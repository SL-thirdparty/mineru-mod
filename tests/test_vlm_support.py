# -*- coding: utf-8 -*-
"""VLM 高精度模型下载支持与拦截测试。

覆盖：
  1) install_flow：_vlm_dir / _set_models_dir（保留其它键）/ vlm_models_ready
  2) install_flow：download_vlm（fastdl 缺失报错 / 全量已就绪跳过并写配置）
  3) install_flow：main --with-vlm 开关（勾选下载 / 不勾选跳过 / --skip-model 一并跳过）
  4) webui.app：vlm_models_ready（未配置 / 配置路径 / 默认缓存目录）
  5) webui.app：create_task 拦截（hybrid-engine 未下载 → 400 拒绝且无副作用；
     hybrid-engine 已就绪 → 放行；pipeline 不受影响）
  6) 安装器 GUI：VLM 复选框默认勾选 + 偏好持久化回读

用法（项目根）:
    runtime\\venv\\Scripts\\python.exe tests\\test_vlm_support.py
"""
import asyncio
import importlib.util
import json
import os
import shutil
import sys
import tempfile
import time
import types
import unittest
from unittest.mock import patch

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for p in (os.path.join(ROOT, "src", "installer"),
          os.path.join(ROOT, "src", "webui"),
          os.path.join(ROOT, "scripts")):
    if os.path.isdir(p) and p not in sys.path:
        sys.path.insert(0, p)

import install_flow as flow  # noqa: E402


def _mapped(win):
    return bool(win.winfo_manager())


# ==================================================================
# install_flow：路径 / 配置 / 就绪判定
# ==================================================================
class TestVlmHelpers(unittest.TestCase):

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="vlm_flow_")

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_vlm_dir_path(self):
        self.assertEqual(
            flow._vlm_dir(self.dir),
            os.path.join(self.dir, "runtime", "models_cache", "models",
                         "OpenDataLab--MinerU2.5-Pro-2605-1.2B",
                         "snapshots", "master"))

    def test_set_models_dir_preserves_other_keys(self):
        cfg_path = os.path.join(self.dir, "mineru.json")
        with open(cfg_path, "w", encoding="utf-8") as f:
            json.dump({"models-dir": {"pipeline": "C:/pipeline"},
                       "download-threads": 16}, f)
        flow._set_models_dir(self.dir, "vlm", "C:/vlm")
        with open(cfg_path, encoding="utf-8") as f:
            cfg = json.load(f)
        self.assertEqual(cfg["models-dir"]["vlm"], "C:/vlm")
        self.assertEqual(cfg["models-dir"]["pipeline"], "C:/pipeline")
        self.assertEqual(cfg["download-threads"], 16)

    def test_set_models_dir_creates_config_when_missing(self):
        flow._set_models_dir(self.dir, "vlm", "C:/vlm")
        with open(os.path.join(self.dir, "mineru.json"), encoding="utf-8") as f:
            cfg = json.load(f)
        self.assertEqual(cfg["models-dir"]["vlm"], "C:/vlm")

    def test_vlm_models_ready_false_without_config(self):
        self.assertFalse(flow.vlm_models_ready(self.dir))

    def test_vlm_models_ready_true_via_config(self):
        vlm_dir = os.path.join(self.dir, "custom_vlm")
        os.makedirs(vlm_dir)
        with open(os.path.join(vlm_dir, "model.safetensors"), "wb") as f:
            f.write(b"weights")
        with open(os.path.join(self.dir, "mineru.json"), "w", encoding="utf-8") as f:
            json.dump({"models-dir": {"vlm": vlm_dir}}, f)
        self.assertTrue(flow.vlm_models_ready(self.dir))

    def test_vlm_models_ready_false_config_points_nowhere(self):
        with open(os.path.join(self.dir, "mineru.json"), "w", encoding="utf-8") as f:
            json.dump({"models-dir": {"vlm": os.path.join(self.dir, "none")}}, f)
        self.assertFalse(flow.vlm_models_ready(self.dir))


# ==================================================================
# install_flow：download_vlm
# ==================================================================
class TestDownloadVlm(unittest.TestCase):

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="vlm_dl_")

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_fastdl_missing_returns_false(self):
        # sys.modules["fastdl"] = None → `import fastdl` 抛 ImportError
        with patch.dict(sys.modules, {"fastdl": None}):
            self.assertFalse(flow.download_vlm(self.dir))

    def test_all_ready_skips_download_and_writes_config(self):
        fake = types.ModuleType("fastdl")
        with patch.dict(sys.modules, {"fastdl": fake}), \
             patch.object(flow, "_file_ok", lambda *a, **k: True):
            self.assertTrue(flow.download_vlm(self.dir))
        kit = flow._vlm_dir(self.dir)
        with open(os.path.join(self.dir, "mineru.json"), encoding="utf-8") as f:
            cfg = json.load(f)
        self.assertEqual(cfg["models-dir"]["vlm"], kit.replace("\\", "/"))


# ==================================================================
# install_flow：main --with-vlm 开关
# ==================================================================
class TestMainWithVlm(unittest.TestCase):

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="vlm_main_")

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def _argv(self, with_vlm=False, skip_model=False):
        argv = ["install_flow.py", "--root", self.dir, "--src", self.dir,
                "--result", os.path.join(self.dir, "result.json")]
        if with_vlm:
            argv.append("--with-vlm")
        if skip_model:
            argv.append("--skip-model")
        return argv

    def _run_main(self, argv, vlm_ok=True, vlm_calls=None, model_calls=None):
        def _vlm(*a, **k):
            if vlm_calls is not None:
                vlm_calls.append(1)
            return vlm_ok

        def _model(*a, **k):
            if model_calls is not None:
                model_calls.append(1)
            return True

        with patch.object(flow, "precheck", lambda root: None), \
             patch.object(flow, "copy_runtime_files", lambda *a, **k: None), \
             patch.object(flow, "predownload_torch", lambda *a, **k: None), \
             patch.object(flow, "ensure_venv_deps", lambda *a, **k: True), \
             patch.object(flow, "download_models", _model), \
             patch.object(flow, "download_vlm", _vlm), \
             patch.object(flow, "create_shortcut", lambda *a, **k: True), \
             patch.object(flow, "write_install_manifest", lambda *a, **k: None), \
             patch.object(flow, "_write_state", lambda *a, **k: None), \
             patch.object(flow, "_write_config_extra", lambda *a, **k: None):
            with patch.object(sys, "argv", argv):
                return flow.main()

    def test_with_vlm_calls_download_vlm(self):
        vlm_calls, model_calls = [], []
        rc = self._run_main(self._argv(with_vlm=True),
                            vlm_calls=vlm_calls, model_calls=model_calls)
        self.assertEqual(rc, 0)
        self.assertEqual(len(model_calls), 1)
        self.assertEqual(len(vlm_calls), 1)
        with open(os.path.join(self.dir, "result.json"), encoding="utf-8") as f:
            self.assertTrue(json.load(f)["ok"])

    def test_without_vlm_skips_download_vlm(self):
        vlm_calls, model_calls = [], []
        rc = self._run_main(self._argv(with_vlm=False),
                            vlm_calls=vlm_calls, model_calls=model_calls)
        self.assertEqual(rc, 0)
        self.assertEqual(len(model_calls), 1)
        self.assertEqual(len(vlm_calls), 0)

    def test_skip_model_skips_both(self):
        vlm_calls, model_calls = [], []
        rc = self._run_main(self._argv(with_vlm=True, skip_model=True),
                            vlm_calls=vlm_calls, model_calls=model_calls)
        self.assertEqual(rc, 0)
        self.assertEqual(len(model_calls), 0)
        self.assertEqual(len(vlm_calls), 0)

    def test_vlm_failure_fails_install(self):
        rc = self._run_main(self._argv(with_vlm=True), vlm_ok=False)
        self.assertEqual(rc, 1)
        with open(os.path.join(self.dir, "result.json"), encoding="utf-8") as f:
            result = json.load(f)
        self.assertFalse(result["ok"])
        self.assertIn("VLM", result["error"])


# ==================================================================
# webui.app：vlm_models_ready + create_task 拦截
# ==================================================================
def _load_webui_module(tmp_root):
    """在隔离 MINERU_ROOT 下加载 webui/app.py（importlib 避免模块名冲突）。"""
    spec = importlib.util.spec_from_file_location(
        "webui_app_under_test",
        os.path.join(ROOT, "src", "webui", "app.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _FakeUpload:
    def __init__(self, name):
        self.filename = name

    async def read(self):
        return b"%PDF-1.4 fake pdf"


class TestWebuiVlm(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.dir = tempfile.mkdtemp(prefix="vlm_webui_")
        cls.out = os.path.join(cls.dir, "out")
        os.environ["MINERU_ROOT"] = cls.dir
        os.environ["MINERU_WEBUI_OUTPUT"] = cls.out
        try:
            cls.app = _load_webui_module(cls.dir)
        except Exception as e:  # noqa: BLE001
            raise unittest.SkipTest(f"无法导入 webui.app: {e}")

    @classmethod
    def tearDownClass(cls):
        os.environ.pop("MINERU_ROOT", None)
        os.environ.pop("MINERU_WEBUI_OUTPUT", None)
        shutil.rmtree(cls.dir, ignore_errors=True)

    def setUp(self):
        """每个用例独立状态：清配置、清模型目录、清任务存储与上传文件。"""
        a = self.app
        for rel in ("mineru.json",
                    os.path.join("runtime", "models_cache"),
                    "custom_vlm"):
            p = os.path.join(self.dir, rel)
            if os.path.isfile(p):
                os.remove(p)
            elif os.path.isdir(p):
                shutil.rmtree(p, ignore_errors=True)
        a.STORE.tasks.clear()
        a.STORE.order.clear()
        a.STORE.total = 0
        for p in a.UPLOADS.glob("*"):
            if p.is_file():
                p.unlink()

    def _write_vlm_snapshot(self, base):
        """在给定根下构造 models-dir.vlm 指向的目录（含 model.safetensors）。"""
        os.makedirs(base, exist_ok=True)
        with open(os.path.join(base, "model.safetensors"), "wb") as f:
            f.write(b"weights")

    def _write_config_vlm(self, path):
        with open(os.path.join(self.dir, "mineru.json"), "w", encoding="utf-8") as f:
            json.dump({"models-dir": {"vlm": path}}, f)

    def _submit(self, backend, files, name=None):
        # 批次名唯一，避免同一秒内重复建同名批次触发 _make 重试后报错
        name = name or f"t{int(time.time() * 1000)}{os.getpid()}"

        async def _run():
            return await self.app.create_task(
                files=files, lang="ch", backend=backend, formula=True,
                table=True, image_analysis=True, is_ocr=False, effort="medium",
                max_pages=1000, formats="", batch="new", batch_name=name)

        return asyncio.run(_run())

    def test_ready_false_without_any_model(self):
        self.assertFalse(self.app.vlm_models_ready())

    def test_ready_true_via_default_cache_dir(self):
        snapshot = os.path.join(self.dir, "runtime", "models_cache", "models",
                                "OpenDataLab--MinerU2.5-Pro-2605-1.2B",
                                "snapshots", "master")
        self._write_vlm_snapshot(snapshot)
        self.assertTrue(self.app.vlm_models_ready())
        # 默认缓存就绪但不写配置，二次调用应仍为 True（幂等）
        self.assertTrue(self.app.vlm_models_ready())

    def test_ready_true_via_config(self):
        vlm = os.path.join(self.dir, "custom_vlm")
        self._write_vlm_snapshot(vlm)
        self._write_config_vlm(vlm)
        self.assertTrue(self.app.vlm_models_ready())

    def test_intercept_hybrid_without_vlm_rejects_400(self):
        from fastapi import HTTPException
        with self.assertRaises(HTTPException) as cm:
            self._submit("hybrid-engine", [_FakeUpload("a.pdf")])
        self.assertEqual(cm.exception.status_code, 400)
        self.assertIn("VLM", cm.exception.detail)
        # 拦截发生在任何副作用之前：没有落盘文件、没有任务入队
        self.assertEqual(self.app.STORE.total, 0)
        self.assertEqual(list(self.app.UPLOADS.glob("*")), [])

    def test_hybrid_with_vlm_passes(self):
        vlm = os.path.join(self.dir, "custom_vlm")
        self._write_vlm_snapshot(vlm)
        self._write_config_vlm(vlm)
        res = self._submit("hybrid-engine", [_FakeUpload("a.pdf")])
        self.assertEqual(len(res["tasks"]), 1)
        self.assertEqual(res["tasks"][0]["filename"], "a.pdf")

    def test_pipeline_without_vlm_passes(self):
        res = self._submit("pipeline", [_FakeUpload("a.pdf")])
        self.assertEqual(len(res["tasks"]), 1)
        self.assertEqual(res["tasks"][0]["filename"], "a.pdf")


# ==================================================================
# 安装器 GUI：VLM 复选框默认勾选 + 偏好持久化
# ==================================================================
class TestGuiVlmOption(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        try:
            import installer_gui
            cls.gui = installer_gui
        except Exception:
            raise unittest.SkipTest("无法导入 installer_gui")

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="vlm_gui_")
        self.gui.messagebox.showinfo = lambda *a, **k: None
        self._orig_prefs = self.gui._prefs_path
        self._orig_open_guide = self.gui.Installer._open_guide
        # 偏好文件指向测试目录，避免污染真实 LOCALAPPDATA
        self.gui._prefs_path = lambda: os.path.join(self.dir, "prefs.json")

    def tearDown(self):
        self.gui._prefs_path = self._orig_prefs
        self.gui.messagebox.showinfo = lambda *a, **k: None
        self.gui.Installer._open_guide = self._orig_open_guide
        shutil.rmtree(self.dir, ignore_errors=True)

    def _app(self):
        self.gui.Installer._open_guide = lambda self: None
        app = self.gui.Installer()
        app.withdraw()
        app.update_idletasks()
        app._check_update_bg = lambda root: None
        return app

    def test_vlm_default_checked(self):
        app = self._app()
        try:
            self.assertTrue(app.vlm_var.get())
        finally:
            app.destroy()

    def test_vlm_prefs_roundtrip(self):
        app = self._app()
        try:
            self.assertTrue(app.vlm_var.get())
            app.vlm_var.set(False)
            app._save_prefs()
        finally:
            app.destroy()
        # 重新打开：勾选状态应回读为上次保存的 False
        app2 = self._app()
        try:
            self.assertFalse(app2.vlm_var.get())
        finally:
            app2.destroy()

    # ---- 检测修复：VLM 纳入核心修复 + 「同时下载」勾选框 ----
    # TickBox 的 grid() 是自定义实现（内部 row Frame 承载），显隐判定查内部行
    @staticmethod
    def _tick_mapped(tick):
        return bool(tick._row.winfo_manager())

    def test_repair_vlm_default_checked(self):
        app = self._app()
        try:
            self.assertTrue(app.repair_vlm_var.get())
            # 初始隐藏，不干扰安装界面
            self.assertFalse(self._tick_mapped(app.repair_vlm_tick))
        finally:
            app.destroy()

    def test_repair_vlm_tick_shown_when_vlm_wait(self):
        app = self._app()
        try:
            app.comps.set_comp("vlm", "wait", "待下载（约 2.3GB）")
            app._set_buttons("check_done")
            self.assertTrue(self._tick_mapped(app.repair_vlm_tick))
        finally:
            app.destroy()

    def test_repair_vlm_tick_hidden_when_vlm_ok(self):
        app = self._app()
        try:
            app.comps.set_comp("vlm", "ok", "已就绪")
            app._set_buttons("check_done")
            self.assertFalse(self._tick_mapped(app.repair_vlm_tick))
        finally:
            app.destroy()

    def test_repair_vlm_tick_hidden_after_repair_starts(self):
        app = self._app()
        try:
            app.comps.set_comp("vlm", "wait", "待下载（约 2.3GB）")
            app._set_buttons("check_done")
            self.assertTrue(self._tick_mapped(app.repair_vlm_tick))
            app._set_buttons("running")
            self.assertFalse(self._tick_mapped(app.repair_vlm_tick))
        finally:
            app.destroy()


if __name__ == "__main__":
    unittest.main()
