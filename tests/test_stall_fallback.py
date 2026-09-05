# -*- coding: utf-8 -*-
"""依赖下载停滞兜底测试。

背景：uv/pip 连接建立后数据不流动时可能永不超时（实测 sympy 6MiB 卡 11 分钟），
覆盖：
  1) run_visible 停滞兜底：超过 MINERU_STALL_TIMEOUT 无输出 → 强制终止并返回失败码
     （调用方据此换源/回退 pip 重试，绝不无限挂起）
  2) run_visible 正常路径：子进程有输出正常退出 → 返回其退出码
  3) GUI 停滞提示：依赖下载 90s 无进展且 worker 存活 → 活动行追加提示
  4) GUI 有进展 / 非下载阶段 → 不出现停滞提示

用法（项目根）:
    runtime\\venv\\Scripts\\python.exe tests\\test_stall_fallback.py
"""
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for p in (os.path.join(ROOT, "src", "installer"),
          os.path.join(ROOT, "scripts")):
    if os.path.isdir(p) and p not in sys.path:
        sys.path.insert(0, p)

import install_mineru_uv as iuv  # noqa: E402


class TestRunVisibleStall(unittest.TestCase):

    def test_stall_kills_process_and_fails(self):
        """子进程无任何输出超过 1s → 被强制终止，返回失败码（供换源回退）。"""
        old = os.environ.get("MINERU_STALL_TIMEOUT")
        os.environ["MINERU_STALL_TIMEOUT"] = "1"
        try:
            t0 = time.time()
            code = iuv.run_visible(
                [sys.executable, "-c", "import time; time.sleep(30)"], True)
            self.assertNotEqual(code, 0)
            # 未被停滞检测前就结束（30s sleep 被 kill，总耗时远小于 30s）
            self.assertLess(time.time() - t0, 15)
        finally:
            if old is None:
                os.environ.pop("MINERU_STALL_TIMEOUT", None)
            else:
                os.environ["MINERU_STALL_TIMEOUT"] = old

    def test_normal_output_returns_code(self):
        """有输出且正常退出：返回子进程退出码（0），不被误杀。"""
        code = iuv.run_visible(
            [sys.executable, "-c", "import sys; print('ok'); sys.exit(0)"], True)
        self.assertEqual(code, 0)

    def test_slow_but_alive_not_killed(self):
        """停滞窗口内持续有输出 → 不触发终止（正常慢速下载不被误杀）。"""
        old = os.environ.get("MINERU_STALL_TIMEOUT")
        os.environ["MINERU_STALL_TIMEOUT"] = "2"
        try:
            code = iuv.run_visible([sys.executable, "-c",
                                    "import time;"
                                    "[(print('tick', i), time.sleep(0.5))"
                                    " for i in range(3)]"], True)
            self.assertEqual(code, 0)
        finally:
            if old is None:
                os.environ.pop("MINERU_STALL_TIMEOUT", None)
            else:
                os.environ["MINERU_STALL_TIMEOUT"] = old


class TestGuiStallHint(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        try:
            import installer_gui
            cls.gui = installer_gui
        except Exception:
            raise unittest.SkipTest("无法导入 installer_gui")

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.gui.messagebox.showinfo = lambda *a, **k: None
        self._orig_open_guide = self.gui.Installer._open_guide

    def tearDown(self):
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

    def test_stall_hint_shown_when_no_progress(self):
        """下载中 90s 无进展且 worker 存活 → 活动行追加停滞提示。"""
        app = self._app()
        try:
            app._running = True
            app._set_activity("正在下载 sympy（6.0MiB）", stopwatch=True)
            app._pkg_last_ts = time.time() - 120
            app.worker = threading.Thread(target=lambda: time.sleep(30),
                                          daemon=True)
            app.worker.start()
            app._sec_tick()
            self.assertIn("下载疑似停滞", app.act_lbl.cget("text"))
        finally:
            app.destroy()

    def test_stall_hint_hidden_when_progress_recent(self):
        """最近有下载进展 → 不显示停滞提示。"""
        app = self._app()
        try:
            app._running = True
            app._set_activity("正在下载 sympy（6.0MiB）", stopwatch=True)
            app._pkg_last_ts = time.time()
            app.worker = threading.Thread(target=lambda: time.sleep(30),
                                          daemon=True)
            app.worker.start()
            app._sec_tick()
            self.assertNotIn("下载疑似停滞", app.act_lbl.cget("text"))
        finally:
            app.destroy()

    def test_stall_hint_hidden_when_not_downloading(self):
        """非下载阶段（安装依赖中，pip 装大包可长时间无输出）→ 不误报停滞。"""
        app = self._app()
        try:
            app._running = True
            app._set_activity("正在安装依赖到虚拟环境 …", stopwatch=True)
            app._pkg_last_ts = 0.0
            app.worker = threading.Thread(target=lambda: time.sleep(30),
                                          daemon=True)
            app.worker.start()
            app._sec_tick()
            self.assertNotIn("下载疑似停滞", app.act_lbl.cget("text"))
        finally:
            app.destroy()


if __name__ == "__main__":
    unittest.main()
