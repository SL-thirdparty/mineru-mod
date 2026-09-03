# -*- coding: utf-8 -*-
"""通用多源竞速下载引擎（纯标准库）。

三层策略（决策 D4：任何时刻至少一条活跃下载流）：
  1. probe()     并发测速出速度榜（全失败返回 []，调用方回退固定优先序兜底）
  2. Downloader  全局线程池统一调度（文件 × 分段共用一个池，无嵌套等待防死锁）
  3. 段级逐源轮转：当前源停滞超过 stall 秒自动换源，段内 .part 断点续传

消费方：
  - install_flow.download_models（模型源链：modelscope → hf-mirror）
  - install_flow 的 torch 大件预下载（pytorch 轮子镜像链）
  - install_mineru_uv.probe_mirrors（pip/uv 镜像测速排序）
  - download_torch_wheels.py（CLI 薄封装）

事件协议（on_event(kind, ...)）：
  probe 不经过本类（独立函数直接返回速度榜）
  race   (key, winner)                 竞速择优结果
  switch (key, old, new)               段级换源
  done   (key, ok)                     文件终态
  retry  (fail_count, round_no)        进入重试轮
"""
import hashlib
import os
import shutil
import threading
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"
_CHUNK = 1 << 20


def _fsize(path):
    try:
        return os.path.getsize(path)
    except OSError:
        return 0


def _file_sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(_CHUNK), b""):
            h.update(chunk)
    return h.hexdigest()


def _verify(path, size, sha):
    """size/sha 任一提供即校验；都缺只查存在。"""
    try:
        if not os.path.isfile(path):
            return False
        if size is not None and os.path.getsize(path) != size:
            return False
        if sha:
            return _file_sha256(path) == sha
        return True
    except OSError:
        return False


# ---------------- 测速 ----------------

def probe(candidates, url_of, probe_bytes=1 << 20, window=5.0, timeout=8):
    """并发测速：每源一个线程，读 url_of(src) 最多 probe_bytes 或 window 秒。

    返回 [(name, mbps)] 按速度降序；失败源 mbps=0.0 沉底保留（回退候选）；
    全部不可达返回 []（调用方回退固定优先序——绝不因测速失败而中止）。
    无 Range 依赖：普通 GET 读流即停，兼容任意端点（HTML 索引页亦可）。
    """
    results = {}

    def _one(src):
        t0 = time.time()
        got = 0
        try:
            req = urllib.request.Request(url_of(src), headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                while got < probe_bytes and (time.time() - t0) < window:
                    chunk = r.read(min(1 << 18, probe_bytes - got))
                    if not chunk:
                        break
                    got += len(chunk)
            results[src] = got / 1048576 / max(time.time() - t0, 0.01)
        except Exception:
            results[src] = 0.0

    threads = [threading.Thread(target=_one, args=(s,), daemon=True)
               for s in candidates]
    for t in threads:
        t.start()
    for t in threads:
        t.join(window + timeout + 2.0)
    return sorted(results.items(), key=lambda kv: kv[1], reverse=True)


# ---------------- 下载器 ----------------

class Counter:
    """字节级进度（心跳轮询用）；竞速探测流量不计入。"""

    def __init__(self):
        self.bytes = 0
        self._lock = threading.Lock()
        self.t0 = time.time()

    def add(self, n):
        with self._lock:
            self.bytes += n

    def speed(self):
        return self.bytes / 1048576 / max(time.time() - self.t0, 0.01)


class _Stalled(Exception):
    """当前源停滞/中断，应换源。"""


class _File:
    __slots__ = ("key", "dest", "size", "sha", "state", "segs", "seg_ok", "lock")

    def __init__(self, key, dest, size, sha):
        self.key = key
        self.dest = dest
        self.size = size
        self.sha = sha
        self.state = "wait"    # wait / run / ok / fail
        self.segs = []
        self.seg_ok = 0
        self.lock = threading.Lock()


class Downloader:
    """全局线程池（文件×分段统一调度）+ 段级逐源轮转 + 断点续传。

    sources: 有序源名列表（速度榜顺序）；url_of(src, key) -> 文件下载 URL
    threads: 池大小（D3：默认 16，范围 4-64 由调用方保证）
    seg_size: 超过此值切段并发；race_min: 超过此值先竞速择优
    stall: 单源无数据秒数（读超时=停滞判定，默认 30）
    """

    def __init__(self, sources, url_of, threads=16, seg_size=32 << 20,
                 race_min=10 << 20, stall=30.0, racers=3,
                 on_event=None, pause_check=None):
        self.sources = [s for s in sources if s]
        self.url_of = url_of
        self.threads = max(1, int(threads))
        self.seg_size = max(1, int(seg_size))
        self.race_min = int(race_min)
        self.stall = max(1.0, float(stall))
        self.racers = max(2, int(racers))
        self.on_event = on_event or (lambda *a: None)
        self.pause_check = pause_check or (lambda: None)
        self.counter = Counter()
        self._files = []
        self._ok_keys = []       # 跨重试轮累计（心跳计数不回退）
        self._lock = threading.Lock()
        self._remaining = 0
        self._all_done = threading.Event()
        self._pool = None

    # ---- 对外接口 ----

    def add(self, key, dest, size=None, sha=None):
        self._files.append(_File(key, dest, size, sha))

    def run_with_retry(self, rounds=3):
        """失败文件自动重试（轮次=rounds）。返回 (ok_keys, fail_keys)。"""
        rounds = max(1, rounds)
        ok_all, pending = [], list(self._files)
        for rnd in range(rounds):
            if not pending:
                break
            self._files = pending
            ok, fail = self._run_once()
            ok_all += ok
            pending = [f for f in pending if f.state != "ok"]
            for f in pending:
                f.state = "wait"
            if fail and rnd + 1 < rounds:
                self.on_event("retry", len(fail), rnd + 1)
        return ok_all, [f.key for f in pending]

    def active_names(self):
        return [f.key for f in self._files if f.state == "run"]

    def files_ok(self):
        return list(self._ok_keys)

    # ---- 单轮执行 ----

    def _run_once(self):
        pool = ThreadPoolExecutor(self.threads)
        self._pool = pool
        self._remaining = 0
        self._all_done.clear()
        try:
            for f in self._files:
                if f.state == "ok" or _verify(f.dest, f.size, f.sha):
                    f.state = "ok"
                    self._ok_keys.append(f.key)
                    continue
                if f.size is None:
                    f.size = self._head_size(f)
                if not f.size:
                    f.state = "fail"
                    self.on_event("done", f.key, False)
                    continue
                self._remaining += 1
            for f in self._files:
                if f.state == "wait":
                    self._schedule(f)
            if self._remaining:
                self._all_done.wait()
        finally:
            pool.shutdown(wait=True)
            self._pool = None
        return ([f.key for f in self._files if f.state == "ok"],
                [f.key for f in self._files if f.state == "fail"])

    def _schedule(self, f):
        f.state = "run"
        multi = len(self.sources) > 1
        if f.size >= self.seg_size:
            if multi and f.size >= self.race_min:
                self._pool.submit(self._safe, self._race_task, f)
            else:
                self._submit_segments(f, self.sources[0] if self.sources else None)
        else:
            self._pool.submit(self._safe, self._whole_task, f)

    def _safe(self, fn, *a):
        """兜底：任务异常也必须推进终态，否则 _all_done 永不置位。"""
        try:
            fn(*a)
        except Exception:
            f = a[0]
            self._file_finish(f, False)

    # ---- 竞速择优（大文件） ----

    def _race_task(self, f):
        racers = self.sources[:min(self.racers, len(self.sources))]
        if len(racers) < 2:
            self._submit_segments(f, racers[0] if racers else None)
            return
        ranked = probe(racers, lambda s: self.url_of(s, f.key),
                       probe_bytes=1 << 20, window=4.0, timeout=self.stall)
        winner = None
        if ranked and ranked[0][1] > 0:
            winner = ranked[0][0]
        elif self.sources:
            winner = self.sources[0]
        self.on_event("race", f.key, winner)
        self._submit_segments(f, winner)

    # ---- 分段调度 ----

    def _submit_segments(self, f, preferred):
        n = min((f.size + self.seg_size - 1) // self.seg_size, 64) or 1
        seg = (f.size + n - 1) // n
        f.segs = [(i * seg, min((i + 1) * seg - 1, f.size - 1))
                  for i in range(n)]
        f.seg_ok = 0
        if not self.sources:
            self._file_finish(f, False)
            return
        for i in range(len(f.segs)):
            self._pool.submit(self._safe, self._seg_task, f, i)

    def _seg_task(self, f, idx):
        s, e = f.segs[idx]
        parts_dir = f.dest + ".parts"
        part = os.path.join(parts_dir, "p%03d" % idx)
        os.makedirs(parts_dir, exist_ok=True)
        if not self._fetch_rotating(f, part, s, e, None):
            self._file_finish(f, False)
            return
        with f.lock:
            f.seg_ok += 1
            last = f.seg_ok == len(f.segs)
        if last and f.state == "run":
            self._merge_and_finish(f)

    def _merge_and_finish(self, f):
        parts_dir = f.dest + ".parts"
        merged = f.dest + ".part"
        try:
            with open(merged, "wb") as out:
                for i in range(len(f.segs)):
                    with open(os.path.join(parts_dir, "p%03d" % i), "rb") as fh:
                        shutil.copyfileobj(fh, out, _CHUNK)
            if not _verify(merged, f.size, f.sha):
                shutil.rmtree(parts_dir, ignore_errors=True)
                _rm(merged)
                self._file_finish(f, False)
                return
            os.replace(merged, f.dest)
        except OSError:
            self._file_finish(f, False)
            return
        shutil.rmtree(parts_dir, ignore_errors=True)
        self._file_finish(f, True)

    # ---- 小文件整下 ----

    def _whole_task(self, f):
        part = f.dest + ".part"
        if not self._fetch_rotating(f, part, 0, f.size - 1, None):
            self._file_finish(f, False)
            return
        if not _verify(part, f.size, f.sha):
            _rm(part)
            self._file_finish(f, False)
            return
        os.replace(part, f.dest)
        self._file_finish(f, True)

    # ---- 核心：逐源轮转 + 断点续传 ----

    def _fetch_rotating(self, f, part, start, end, preferred):
        """下载 [start, end] 到 part；part 现有字节即断点。
        逐源轮转：当前源停滞/出错 → 下一源从断点续传。"""
        order = self._order(preferred)
        if not order:
            return False
        span = end - start + 1
        for si, src in enumerate(order):
            self.pause_check()
            got = _fsize(part)
            if got > span:          # 残留超长（异常）→ 重下
                _rm(part)
                got = 0
            pos = start + got
            if pos > end:
                return True
            try:
                self._fetch_range(src, f, part, pos, end)
                return True
            except Exception:
                nxt = order[(si + 1) % len(order)]
                self.on_event("switch", f.key, src, nxt)
                continue
        return False

    def _fetch_range(self, src, f, part, pos, end):
        url = self.url_of(src, f.key)
        headers = {"User-Agent": UA, "Range": "bytes=%d-%d" % (pos, end)}
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=self.stall) as r:
            if pos > 0 and r.status != 206:
                raise _Stalled("源不支持续传(%d)" % r.status)
            mode = "ab" if (pos > 0 and _fsize(part) > 0) else "wb"
            with open(part, mode) as out:
                remaining = end - pos + 1
                last = time.time()
                got = 0
                while got < remaining:
                    chunk = r.read(min(_CHUNK, remaining - got))
                    if not chunk:
                        break
                    out.write(chunk)
                    self.counter.add(len(chunk))
                    got += len(chunk)
                    if time.time() - last > self.stall:
                        raise _Stalled("停滞超过 %.0fs" % self.stall)
                    last = time.time()
                if got < remaining:
                    raise _Stalled("短读 %d/%d" % (got, remaining))

    def _order(self, preferred):
        if preferred and preferred in self.sources:
            i = self.sources.index(preferred)
            return self.sources[i:] + self.sources[:i]
        return list(self.sources)

    def _head_size(self, f):
        for src in self.sources:
            try:
                req = urllib.request.Request(
                    self.url_of(src, f.key), method="HEAD",
                    headers={"User-Agent": UA})
                with urllib.request.urlopen(req, timeout=20) as r:
                    n = int(r.headers.get("Content-Length", 0) or 0)
                if n > 0:
                    return n
            except Exception:
                continue
        return None

    def _file_finish(self, f, ok):
        with self._lock:
            if f.state in ("ok", "fail"):
                return          # 已终态（多段并发失败/竞速兜底重复调用去重）
            f.state = "ok" if ok else "fail"
            if ok:
                self._ok_keys.append(f.key)
            self._remaining -= 1
            done = self._remaining <= 0
        if done:
            self._all_done.set()
        self.on_event("done", f.key, ok)


def _rm(path):
    try:
        if os.path.exists(path):
            os.remove(path)
    except OSError:
        pass
