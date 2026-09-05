# -*- coding: utf-8 -*-
"""P3 多源竞速下载引擎测试（本地 HTTP 服务模拟慢源/停滞源/坏源）。

覆盖：
  1) probe：正常测速排序 / 全部不可达回退
  2) Downloader：基础多文件 / 分段合并 / 停滞换源 / 404 轮转 / 断点续传 /
     sha 不匹配失败 / 重试轮 / 线程数构造

用法（项目根）:
    runtime\\venv\\Scripts\\python.exe tests\\test_fastdl.py
"""
import hashlib
import io
import os
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for p in (os.path.join(ROOT, "scripts"),):
    if os.path.isdir(p) and p not in sys.path:
        sys.path.insert(0, p)

import fastdl  # noqa: E402


def _sha(data):
    return hashlib.sha256(data).hexdigest()


class _State:
    """服务端行为状态（跨 handler 实例共享）。"""
    files = {}          # key → bytes
    flaky_first = {}    # key → 剩余首败次数（/flaky 用）
    lock = threading.Lock()


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def do_GET(self):
        # 路径形如 /<mode>/<key>
        parts = self.path.lstrip("/").split("/", 1)
        mode = parts[0] if parts else "ok"
        key = parts[1] if len(parts) > 1 else ""
        data = _State.files.get(key)

        if mode == "gone" or data is None:
            self.send_error(404)
            return
        if mode == "stall":
            time.sleep(3.0)          # 超过测试用 stall=1s → 读超时换源
        if mode == "slow":
            time.sleep(1.5)          # probe 下慢于 ok 源
        if mode == "flaky":
            with _State.lock:
                n = _State.flaky_first.get(key, 0)
                if n > 0:
                    _State.flaky_first[key] = n - 1
                    self.send_error(500)
                    return
        if mode == "short":           # 谎报长度后短读
            self.send_response(200)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data[: max(1, len(data) // 2)])
            self.wfile.flush()
            self.close_connection = True
            return

        rng = self.headers.get("Range")
        start, end = 0, len(data) - 1
        if rng:
            a, _, b = rng.replace("bytes=", "").partition("-")
            start = int(a) if a else 0
            end = int(b) if b else len(data) - 1
        body = data[start:end + 1]
        self.send_response(206 if rng else 200)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Content-Range", "bytes %d-%d/%d" % (start, end, len(data)))
        self.end_headers()
        self.wfile.write(body)

    def do_HEAD(self):
        parts = self.path.lstrip("/").split("/", 1)
        mode = parts[0] if parts else "ok"
        key = parts[1] if len(parts) > 1 else ""
        data = _State.files.get(key)
        if mode == "gone" or data is None:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()


class _Server:
    """每源一个独立端口：同一 key 在不同源上有不同行为。"""

    def __init__(self):
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self.base = "http://127.0.0.1:%d" % self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def stop(self):
        self.httpd.shutdown()
        self.httpd.server_close()


class FastDLTest(unittest.TestCase):
    stall = 1.0
    seg_size = 32 * 1024

    def setUp(self):
        _State.files.clear()
        _State.flaky_first.clear()
        self.srv = _Server()
        self.dir = tempfile.mkdtemp(prefix="fastdl_")
        self.events = []
        self.sources = [self.srv.base + "/ok", self.srv.base + "/stall",
                        self.srv.base + "/gone"]

    def tearDown(self):
        self.srv.stop()
        import shutil
        shutil.rmtree(self.dir, ignore_errors=True)

    def _url(self, src, key):
        return src + "/" + key

    def _dl(self, sources=None, **kw):
        kw.setdefault("stall", self.stall)
        kw.setdefault("seg_size", self.seg_size)
        return fastdl.Downloader(sources or self.sources, self._url,
                                 on_event=lambda *a: self.events.append(a),
                                 **kw)

    # ---- 1) probe ----

    def test_probe_ranking(self):
        _State.files["p"] = b"x" * 65536
        ranked = fastdl.probe([self.srv.base + "/slow", self.srv.base + "/ok"],
                              lambda s: self._url(s, "p"),
                              probe_bytes=65536, window=1.0,
                              timeout=3.0)
        self.assertEqual(len(ranked), 2)
        self.assertEqual(ranked[0][0], self.srv.base + "/ok")
        self.assertGreater(ranked[0][1], 0.0)
        self.assertEqual(ranked[1][0], self.srv.base + "/slow")

    def test_probe_all_fail(self):
        ranked = fastdl.probe([self.srv.base + "/gone"], lambda s: self._url(s, "x"),
                              probe_bytes=1024, window=0.5, timeout=1.0)
        # 全失败：失败源以 0.0 保留（非空），供调用方回退固定序
        self.assertEqual(ranked, [(self.srv.base + "/gone", 0.0)])

    # ---- 2) Downloader ----

    def test_download_basic(self):
        _State.files["a"] = b"hello fastdl"
        _State.files["b"] = b"second file content"
        dl = self._dl()
        for k in ("a", "b"):
            dl.add(k, os.path.join(self.dir, k), len(_State.files[k]), _sha(_State.files[k]))
        ok, fail = dl.run_with_retry(rounds=1)
        self.assertEqual(ok, ["a", "b"])
        self.assertEqual(fail, [])
        for k in ("a", "b"):
            with open(os.path.join(self.dir, k), "rb") as f:
                self.assertEqual(f.read(), _State.files[k])

    def test_download_segmented(self):
        data = os.urandom(100 * 1024)          # > seg_size → 分段
        _State.files["big"] = data
        dl = self._dl()
        dl.add("big", os.path.join(self.dir, "big"), len(data), _sha(data))
        ok, fail = dl.run_with_retry(rounds=1)
        self.assertEqual(ok, ["big"])
        with open(os.path.join(self.dir, "big"), "rb") as f:
            self.assertEqual(f.read(), data)

    def test_switch_on_stall(self):
        """主源停滞（读超时）→ 自动换到 /ok 源完成。"""
        data = os.urandom(4096)
        _State.files["s"] = data
        dl = self._dl(sources=[self.srv.base + "/stall", self.srv.base + "/ok"])
        dl.add("s", os.path.join(self.dir, "s"), len(data), _sha(data))
        ok, fail = dl.run_with_retry(rounds=1)
        self.assertEqual(ok, ["s"])
        switches = [e for e in self.events if e[0] == "switch"]
        self.assertTrue(switches, "应产生换源事件")

    def test_404_falls_through(self):
        data = b"tiny"
        _State.files["t"] = data
        dl = self._dl(sources=[self.srv.base + "/gone", self.srv.base + "/ok"])
        dl.add("t", os.path.join(self.dir, "t"), len(data), _sha(data))
        ok, fail = dl.run_with_retry(rounds=1)
        self.assertEqual(ok, ["t"])

    def test_short_read_retries_source(self):
        """短读（服务器谎报长度提前断流）→ 换源续传完成。"""
        data = os.urandom(8192)
        _State.files["sr"] = data
        dl = self._dl(sources=[self.srv.base + "/short", self.srv.base + "/ok"])
        dl.add("sr", os.path.join(self.dir, "sr"), len(data), _sha(data))
        ok, fail = dl.run_with_retry(rounds=2)
        self.assertEqual(ok, ["sr"])
        with open(os.path.join(self.dir, "sr"), "rb") as f:
            self.assertEqual(f.read(), data)

    def test_resume_whole_file(self):
        """小文件断点续传：预置半份 .part → 只补后半。"""
        data = os.urandom(4096)
        _State.files["r"] = data
        dest = os.path.join(self.dir, "r")
        with open(dest + ".part", "wb") as f:
            f.write(data[:1000])
        dl = self._dl(sources=[self.srv.base + "/ok"])
        dl.add("r", dest, len(data), _sha(data))
        ok, fail = dl.run_with_retry(rounds=1)
        self.assertEqual(ok, ["r"])
        with open(dest, "rb") as f:
            self.assertEqual(f.read(), data)

    def test_sha_mismatch_fails(self):
        _State.files["bad"] = b"corrupted content"
        dl = self._dl(sources=[self.srv.base + "/ok"])
        dl.add("bad", os.path.join(self.dir, "bad"),
               len(_State.files["bad"]), "0" * 64)   # 错误 sha
        ok, fail = dl.run_with_retry(rounds=1)
        self.assertEqual(fail, ["bad"])
        self.assertFalse(os.path.exists(os.path.join(self.dir, "bad")))

    def test_retry_round_flaky(self):
        """/flaky 源首个请求 500 → 首轮失败，重试轮成功。"""
        data = b"flaky but fine"
        _State.files["f"] = data
        _State.flaky_first["f"] = 1
        dl = self._dl(sources=[self.srv.base + "/flaky"])
        dl.add("f", os.path.join(self.dir, "f"), len(data), _sha(data))
        ok, fail = dl.run_with_retry(rounds=2)
        self.assertEqual(ok, ["f"])

    def test_threads_bounds(self):
        for n in (4, 16, 64):
            self.assertEqual(self._dl(threads=n).threads, n)

    def test_all_sources_fail(self):
        _State.files["z"] = b"z"
        dl = self._dl(sources=[self.srv.base + "/gone", self.srv.base + "/stall"])
        dl.add("z", os.path.join(self.dir, "z"), 1, _sha(b"z"))
        ok, fail = dl.run_with_retry(rounds=1)
        self.assertEqual(fail, ["z"])

    def test_skip_existing(self):
        """已完文件按 sha 跳过（断点续传语义）。"""
        data = b"already there"
        _State.files["e"] = data
        dest = os.path.join(self.dir, "e")
        with open(dest, "wb") as f:
            f.write(data)
        dl = self._dl()
        dl.add("e", dest, len(data), _sha(data))
        ok, fail = dl.run_with_retry(rounds=1)
        self.assertEqual(ok, ["e"])
        self.assertEqual(dl.counter.bytes, 0)      # 未产生下载流量

    def test_small_file_creates_missing_dirs(self):
        """回归：小文件（< seg_size 走整下路径）的 dest 父目录不存在时，
        add() 应自动创建目录链，而非把 FileNotFoundError 误判为源故障
        （线上表现：TabCls/TabRec 下 3 个 onnx 永久失败）。"""
        data = b"onnx-like small model"
        _State.files["m"] = data
        dest = os.path.join(self.dir, "models", "TabRec", "UnetStructure", "unet.onnx")
        dl = self._dl()
        dl.add("m", dest, len(data), _sha(data))
        ok, fail = dl.run_with_retry(rounds=1)
        self.assertEqual(ok, ["m"])
        with open(dest, "rb") as f:
            self.assertEqual(f.read(), data)


if __name__ == "__main__":
    unittest.main(verbosity=2)
