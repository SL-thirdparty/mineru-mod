# -*- coding: utf-8 -*-
"""P3.3 WebUI 下载线程数配置测试。

覆盖：
  1) _init_download_threads：读 mineru.json 的 download-threads
     （合法值 / 越界钳制 4-64 / 缺文件 / 损坏 JSON 回退 16）
  2) ConfigStore.update(download_threads)：持久化到 config.json + 同步写回 mineru.json
  3) /api/config POST 值域校验：越界钳制、非法输入 400

用法（项目根）:
    runtime\\venv\\Scripts\\python.exe -m pytest tests\\test_dl_threads_config.py -q
    # 或直接运行：
    runtime\\venv\\Scripts\\python.exe tests\\test_dl_threads_config.py
"""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src", "webui"))

# app.py import 时按 MINERU_ROOT 定位 ROOT/RUNTIME/DATA/CONFIG_JSON，
# 指向临时目录避免污染项目根
_TEST_TMP = tempfile.mkdtemp(prefix="dlthreads_")
os.environ["MINERU_ROOT"] = _TEST_TMP

app = None


def _load_app():
    global app
    if app is None:
        import app as _app   # noqa: E402
        app = _app
    return app


class TestInitDownloadThreads(unittest.TestCase):

    def setUp(self):
        self.a = _load_app()
        self.cfg_json = Path(self.a.CONFIG_JSON)

    def _write(self, d):
        self.cfg_json.parent.mkdir(parents=True, exist_ok=True)
        self.cfg_json.write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")

    def test_read_from_installer_json(self):
        self._write({"download-threads": 24, "models-dir": "x"})
        self.assertEqual(self.a._init_download_threads(), 24)

    def test_clamp_high(self):
        self._write({"download-threads": 999})
        self.assertEqual(self.a._init_download_threads(), 64)

    def test_clamp_low(self):
        self._write({"download-threads": 1})
        self.assertEqual(self.a._init_download_threads(), 4)

    def test_missing_file(self):
        if self.cfg_json.exists():
            self.cfg_json.unlink()
        self.assertEqual(self.a._init_download_threads(), 16)

    def test_corrupt_json(self):
        self.cfg_json.parent.mkdir(parents=True, exist_ok=True)
        self.cfg_json.write_text("{not json", encoding="utf-8")
        self.assertEqual(self.a._init_download_threads(), 16)


class TestConfigStoreSync(unittest.TestCase):

    def setUp(self):
        self.a = _load_app()
        self.cfg_json = Path(self.a.CONFIG_JSON)
        self.cfg_json.parent.mkdir(parents=True, exist_ok=True)
        self.cfg_json.write_text(
            json.dumps({"download-threads": 8, "models-dir": "keep"}, ensure_ascii=False),
            encoding="utf-8")

    def test_update_persists_and_syncs(self):
        self.a.CONFIG.update({"download_threads": 32})
        cfg = self.a.CONFIG.get()
        self.assertEqual(cfg["download_threads"], 32)
        # config.json 持久化
        saved = json.loads(self.a.CONFIG_PATH.read_text(encoding="utf-8"))
        self.assertEqual(saved["download_threads"], 32)
        # mineru.json 同步写回，且保留原有其他键
        m = json.loads(self.cfg_json.read_text(encoding="utf-8"))
        self.assertEqual(m["download-threads"], 32)
        self.assertEqual(m["models-dir"], "keep")

    def test_update_without_threads_keeps_synced_value(self):
        self.a.CONFIG.update({"download_threads": 40})
        self.a.CONFIG.update({"idle_release_seconds": 45})
        m = json.loads(self.cfg_json.read_text(encoding="utf-8"))
        self.assertEqual(m["download-threads"], 40)
        cfg = self.a.CONFIG.get()
        self.assertEqual(cfg["download_threads"], 40)


class TestApiValidation(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.a = _load_app()
        from fastapi.testclient import TestClient
        cls.client = TestClient(cls.a.app)

    def test_clamp_to_range(self):
        r = self.client.post("/api/config", json={"download_threads": 999})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["download_threads"], 64)
        r = self.client.post("/api/config", json={"download_threads": 2})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["download_threads"], 4)

    def test_invalid_rejected(self):
        r = self.client.post("/api/config", json={"download_threads": "abc"})
        self.assertEqual(r.status_code, 400)

    def test_get_config_contains_threads(self):
        r = self.client.get("/api/config")
        self.assertEqual(r.status_code, 200)
        self.assertIn("download_threads", r.json())


if __name__ == "__main__":
    unittest.main(verbosity=2)
