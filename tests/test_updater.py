# -*- coding: utf-8 -*-
"""P4 远程修复/升级器测试。

覆盖：
  1) source_bases / _url：镜像链与中文路径 URL 编码
  2) fetch_manifest：镜像链逐级回退（坏源 → 好源）
  3) check：三种差异场景（全新增 / 损坏 / 一致）+ 版本对比
  4) download：差异文件下载到 .update/ 暂存（fastdl + sha256 校验）
  5) apply_update：暂存替换 + 本地清单重写（进程终止调用被 mock）
  6) read_dl_threads：mineru.json download-threads 读取与钳制

远端用本地 HTTP 服务模拟（MINERU_UPDATE_BASES 覆盖源链）。

用法（项目根）:
    runtime\\venv\\Scripts\\python.exe tests\\test_updater.py
"""
import json
import os
import shutil
import sys
import tempfile
import threading
import unittest
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for p in (os.path.join(ROOT, "src", "installer"),
          os.path.join(ROOT, "scripts")):
    if os.path.isdir(p) and p not in sys.path:
        sys.path.insert(0, p)

import updater  # noqa: E402


def _sha_bytes(b):
    import hashlib
    return hashlib.sha256(b).hexdigest()


class _Server:
    """模拟 dist 分支远端：/manifest.json + 文件（URL 编码路径）。"""

    def __init__(self):
        self.files = {}
        srv = self

        class H(BaseHTTPRequestHandler):
            def _resolve(self):
                path = urllib.parse.unquote(self.path.lstrip("/"))
                if path == "manifest.json":
                    return json.dumps(srv.files.get("__manifest__")).encode()
                return srv.files.get(path)

            def do_GET(self):
                body = self._resolve()
                if body is None:
                    self.send_error(404)
                    return
                self.send_response(200)
                self.send_header("Content-Type", "application/octet-stream")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_HEAD(self):
                body = self._resolve()
                if body is None:
                    self.send_error(404)
                    return
                self.send_response(200)
                self.send_header("Content-Type", "application/octet-stream")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()

            def log_message(self, *a):
                pass

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.base = f"http://127.0.0.1:{self.httpd.server_port}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def stop(self):
        self.httpd.shutdown()
        self.httpd.server_close()


class UpdaterTest(unittest.TestCase):

    def setUp(self):
        self.srv = _Server()
        self.root = tempfile.mkdtemp(prefix="upd_root_")
        os.environ["MINERU_UPDATE_BASES"] = self.srv.base
        self._real_run = updater.subprocess.run

        class _FakeCompleted:
            stdout = ""      # _stop_tray 读取 tasklist 结果

        updater.subprocess.run = lambda *a, **k: _FakeCompleted()  # 屏蔽 taskkill/tasklist

    def tearDown(self):
        updater.subprocess.run = self._real_run
        os.environ.pop("MINERU_UPDATE_BASES", None)
        self.srv.stop()
        shutil.rmtree(self.root, ignore_errors=True)

    # ---- 远端与本地安装根的布置 ----

    def _remote(self, files, version="2.0.0"):
        """写远端文件 + manifest。files: {rel: bytes}。"""
        manifest = {"version": version, "created": "t",
                    "files": {r: _sha_bytes(b) for r, b in files.items()}}
        self.srv.files = dict(files)
        self.srv.files["__manifest__"] = manifest
        return manifest

    def _local(self, files, version="1.0.0"):
        """写本地安装根：files {rel: bytes} + .install_manifest.json。"""
        for rel, b in files.items():
            p = os.path.join(self.root, *rel.split("/"))
            os.makedirs(os.path.dirname(p), exist_ok=True)
            with open(p, "wb") as f:
                f.write(b)
        with open(os.path.join(self.root, updater.MANIFEST_LOCAL), "w",
                  encoding="utf-8") as f:
            json.dump({"version": version, "files": {}}, f)

    # ---- 测试 ----

    def test_source_bases_and_url_encode(self):
        os.environ.pop("MINERU_UPDATE_BASES")
        bases = updater.source_bases()
        self.assertEqual(len(bases), 3)
        self.assertIn("raw.githubusercontent.com", bases[0])
        self.assertIn("ghproxy.cn", bases[1])
        self.assertIn("cdn.jsdelivr.net/gh", bases[2])
        url = updater._url("http://x/y", "MinerU文档解析/exe.txt")
        self.assertEqual(url, "http://x/y/MinerU%E6%96%87%E6%A1%A3%E8%A7%A3%E6%9E%90/exe.txt")

    def test_fetch_manifest_fallback(self):
        """第一个源不可达 → 回退第二个源。"""
        bad = "http://127.0.0.1:1"
        os.environ["MINERU_UPDATE_BASES"] = bad + ";" + self.srv.base
        self._remote({"a.txt": b"hello"})
        m = updater.fetch_manifest()
        self.assertEqual(m["version"], "2.0.0")
        self.assertIn("a.txt", m["files"])

    def test_fetch_manifest_all_fail(self):
        os.environ["MINERU_UPDATE_BASES"] = "http://127.0.0.1:1"
        with self.assertRaises(RuntimeError):
            updater.fetch_manifest()

    def test_check_all_added(self):
        files = {"MinerU文档解析/a.exe": b"A" * 100, "使用说明.html": b"<h1>x</h1>"}
        self._remote(files)
        self._local({})
        d = updater.check(self.root)
        self.assertFalse(d["up_to_date"])
        self.assertEqual(sorted(d["added"]), sorted(files))
        self.assertEqual(d["changed"], [])
        self.assertEqual(d["local_version"], "1.0.0")
        self.assertEqual(d["remote_version"], "2.0.0")

    def test_check_changed_and_intact(self):
        files = {"f_ok.bin": b"ok", "f_bad.bin": b"corrupt"}
        self._remote(files)
        self._local({"f_ok.bin": b"ok", "f_bad.bin": b"TAMPERED!"})
        d = updater.check(self.root)
        self.assertEqual(d["added"], [])
        self.assertEqual(d["changed"], ["f_bad.bin"])

    def test_check_up_to_date(self):
        files = {"f.bin": b"same"}
        self._remote(files, version="1.0.0")
        self._local(files, version="1.0.0")
        d = updater.check(self.root)
        self.assertTrue(d["up_to_date"])

    def test_download_and_apply(self):
        files = {"app/x.exe": b"X" * 5000, "使用说明.html": b"doc"}
        remote = self._remote(files)
        self._local({"app/x.exe": b"OLD", "使用说明.html": b"doc"})
        d = updater.check(self.root)
        ok, fail = updater.download(self.root, remote, d["added"] + d["changed"])
        self.assertEqual(ok, ["app/x.exe"])
        self.assertEqual(fail, [])
        stage = os.path.join(self.root, updater.STAGE_DIR, "app", "x.exe")
        with open(stage, "rb") as f:
            self.assertEqual(f.read(), b"X" * 5000)
        # 应用后：文件修复 + 本地清单更新为远端版本与哈希
        self.assertTrue(updater.apply_update(self.root, remote, d["added"] + d["changed"]))
        with open(os.path.join(self.root, "app", "x.exe"), "rb") as f:
            self.assertEqual(f.read(), b"X" * 5000)
        with open(os.path.join(self.root, updater.MANIFEST_LOCAL), encoding="utf-8") as f:
            lm = json.load(f)
        self.assertEqual(lm["version"], "2.0.0")
        self.assertEqual(lm["files"], remote["files"])

    def test_download_rejects_tampered_source(self):
        """远端实际内容与 manifest 哈希不符（模拟源被篡改/损坏）→ 校验失败。"""
        files = {"evil.bin": b"evil"}
        remote = self._remote(files)
        remote["files"]["evil.bin"] = "0" * 64
        self.srv.files["__manifest__"] = remote
        ok, fail = updater.download(self.root, remote, ["evil.bin"])
        self.assertEqual(ok, [])
        self.assertEqual(fail, ["evil.bin"])

    def test_read_dl_threads(self):
        with open(os.path.join(self.root, "mineru.json"), "w", encoding="utf-8") as f:
            json.dump({"download-threads": 32}, f)
        self.assertEqual(updater.read_dl_threads(self.root), 32)
        with open(os.path.join(self.root, "mineru.json"), "w", encoding="utf-8") as f:
            json.dump({"download-threads": 999}, f)
        self.assertEqual(updater.read_dl_threads(self.root), 64)
        self.assertEqual(updater.read_dl_threads(os.path.join(self.root, "none")), 16)


if __name__ == "__main__":
    unittest.main(verbosity=2)
