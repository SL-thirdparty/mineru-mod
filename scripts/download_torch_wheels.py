# -*- coding: utf-8 -*-
"""下载 CUDA torch / torchvision wheel 到 torch_wheels/（fastdl 多源竞速引擎）。

用法:
    python download_torch_wheels.py [--cu cu128] [--threads 16] [--dir torch_wheels]

说明:
  - 与 install_mineru_uv.py 的 --local-torch-dir 约定一致：wheel 放入项目根 torch_wheels/，
    安装器会自动探测并离线优先安装，避免联网慢。
  - 开始时并发测速各镜像，选最快源作主源；大 wheel（torch ~2.5GB）逐文件竞速择优；
    下载中停滞 >30s 自动换镜像续传；断点续传（重跑只补未完成部分）。
"""
import argparse
import os
import sys
import threading

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import fastdl                       # noqa: E402
from install_mineru_uv import PYTORCH_INDEXES, TORCH_WHEELS  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cu", default="cu128")
    ap.add_argument("--threads", type=int, default=16)
    ap.add_argument("--dir", default="torch_wheels")
    args = ap.parse_args()

    out_dir = args.dir
    os.makedirs(out_dir, exist_ok=True)
    threads = min(64, max(4, args.threads))
    cu = args.cu
    mirrors = [t.format(cu=cu) for t in PYTORCH_INDEXES]

    def url_of(src, key):
        return src + "/" + TORCH_WHEELS[key].format(cu=cu).replace("+", "%2B")

    def on_event(kind, *a):
        if kind == "race":
            print("  竞速择优 %s → %s" % (a[0], a[1]), flush=True)
        elif kind == "switch":
            print("  换源续传：%s → %s" % (a[1], a[2]), flush=True)
        elif kind == "retry":
            print("  第 %d 轮重试 %d 个 wheel ..." % (a[1], a[0]), flush=True)

    print("并发测速 %d 个镜像（约 5s）..." % len(mirrors), flush=True)
    ranked = fastdl.probe(mirrors, lambda s: url_of(s, "torch"),
                          probe_bytes=1 << 20, window=5.0, timeout=8)
    if ranked:
        for name, mbps in ranked:
            print("  测速 %-46s %s" % (name, "%.1f MB/s" % mbps if mbps > 0 else "不可达"),
                  flush=True)
        best = [n for n, v in ranked if v > 0]
        if best:
            mirrors = best + [s for s in mirrors if s not in best]

    dl = fastdl.Downloader(mirrors, url_of, threads=threads, seg_size=32 << 20,
                           race_min=200 << 20, stall=30.0, on_event=on_event)
    for key in TORCH_WHEELS:
        dl.add(key, os.path.join(out_dir, TORCH_WHEELS[key].format(cu=cu)))

    _hb_stop = threading.Event()

    def _heartbeat():
        while not _hb_stop.wait(3.0):
            got = dl.counter.bytes
            total_b = sum(f.size or 0 for f in dl._files)
            if total_b:
                print("  进度 %.0f%%  %.2f/%.2f GB  %.1f MB/s" % (
                    100 * got / total_b, got / 1024 ** 3, total_b / 1024 ** 3,
                    dl.counter.speed()), flush=True)

    threading.Thread(target=_heartbeat, daemon=True).start()
    print("开始下载（%d 线程 · 源链 %s）" % (threads, " → ".join(mirrors)), flush=True)
    try:
        ok, fail = dl.run_with_retry(rounds=2)
    finally:
        _hb_stop.set()

    print("=" * 50, flush=True)
    if fail:
        print("失败：%s（请检查网络后重试，已下载部分会自动续传）" % ", ".join(fail), flush=True)
        return 1
    print("全部完成（%s）" % ", ".join(sorted(ok)), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
