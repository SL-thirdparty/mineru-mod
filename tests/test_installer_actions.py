# -*- coding: utf-8 -*-
"""安装器维护操作与按钮布局测试（启动三选弹窗 → 主界面常驻按钮）。

覆盖：
  1) 未安装启动：仅「开始安装」，维护按钮隐藏
  2) 已安装启动：检测修复/检查更新/卸载 常驻，主按钮变「重新安装」，徽章显示已安装版本
  3) 运行中：切换为 暂停/停止（维护按钮隐藏）
  4) 完成后：恢复维护按钮 + 打开目录
  5) _ver_tuple 版本大小比较
  6) _installed_version 读取清单版本
  7) 修复模式完成文案 = 「检测修复完成」，安装模式 = 「安装完成」

用法（项目根）:
    runtime\\venv\\Scripts\\python.exe tests\\test_installer_actions.py
"""
import json
import os
import shutil
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for p in (os.path.join(ROOT, "src", "installer"),
          os.path.join(ROOT, "scripts")):
    if os.path.isdir(p) and p not in sys.path:
        sys.path.insert(0, p)


def _mapped(win):
    return bool(win.winfo_manager())


class TestInstallerActions(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        try:
            import installer_gui
            cls.gui = installer_gui
        except Exception:
            raise unittest.SkipTest("无法导入 installer_gui")

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self._orig_detect = self.gui._detect_installed_root
        self._orig_showinfo = self.gui.messagebox.showinfo
        self.gui.messagebox.showinfo = lambda *a, **k: None
        # 真实桌面快捷方式/浏览器不参与测试
        self._orig_open_guide = self.gui.Installer._open_guide

    def tearDown(self):
        self.gui._detect_installed_root = self._orig_detect
        self.gui.messagebox.showinfo = self._orig_showinfo
        self.gui.Installer._open_guide = self._orig_open_guide
        shutil.rmtree(self.dir, ignore_errors=True)

    def _make_installed_root(self, version="1.2.3"):
        """构造一个"已安装"根：安装清单 + 卸载程序。"""
        with open(os.path.join(self.dir, ".install_manifest.json"), "w",
                  encoding="utf-8") as f:
            json.dump({"version": version, "files": {}}, f)
        un = os.path.join(self.dir, "卸载MinerU.exe")
        with open(un, "wb") as f:
            f.write(b"MZ")
        return self.dir

    def _app(self, installed=None):
        self.gui.Installer._open_guide = lambda self: None
        app = self.gui.Installer()
        app.withdraw()
        app.update_idletasks()
        self.gui._detect_installed_root = lambda: installed   # 显式控制检测结果
        app._check_update_bg = lambda root: None   # 屏蔽启动后台联网
        return app

    def test_idle_not_installed(self):
        app = self._app(installed=None)
        try:
            app._startup_state()
            self.assertEqual(app.btn_primary._text, "开始安装")
            self.assertFalse(_mapped(app.btn_repair))
            self.assertFalse(_mapped(app.btn_update))
            self.assertFalse(_mapped(app.btn_uninstall))
            self.assertFalse(_mapped(app.btn_open))
        finally:
            app.destroy()

    def test_idle_installed(self):
        root = self._make_installed_root()
        app = self._app(installed=root)
        try:
            app._startup_state()
            self.assertEqual(app.btn_primary._text, "重新安装")
            for b in (app.btn_repair, app.btn_update, app.btn_uninstall,
                      app.btn_open):
                self.assertTrue(_mapped(b), f"{b._text} 未显示")
            self.assertIn("已安装 v1.2.3", app.pill._text)
        finally:
            app.destroy()

    def test_running_switches_to_pause_stop(self):
        root = self._make_installed_root()
        app = self._app(installed=root)
        try:
            app._set_buttons("running")
            self.assertTrue(_mapped(app.btn_pause))
            self.assertTrue(_mapped(app.btn_stop))
            self.assertFalse(_mapped(app.btn_repair))
            self.assertFalse(_mapped(app.btn_update))
            self.assertFalse(_mapped(app.btn_uninstall))
            self.assertEqual(app.btn_primary._text, "安装中…")
        finally:
            app.destroy()

    def test_done_restores_maintenance(self):
        root = self._make_installed_root()
        app = self._app(installed=root)
        try:
            app._set_buttons("done")
            for b in (app.btn_repair, app.btn_update, app.btn_uninstall,
                      app.btn_open):
                self.assertTrue(_mapped(b), f"{b._text} 未显示")
            self.assertEqual(app.btn_primary._text, "重新安装")
        finally:
            app.destroy()

    def test_ver_tuple(self):
        v = self.gui._ver_tuple
        self.assertEqual(v("1.0.0"), (1, 0, 0))
        self.assertEqual(v("1.2"), (1, 2, 0))
        self.assertEqual(v("2"), (2, 0, 0))
        self.assertEqual(v("未知"), (0, 0, 0))
        self.assertTrue(v("1.10.0") > v("1.9.9"))
        self.assertTrue(v("1.1.0") > v("1.0.9"))

    def test_installed_version(self):
        self._make_installed_root("1.2.3")
        self.assertEqual(self.gui._installed_version(self.dir), "1.2.3")
        empty = os.path.join(self.dir, "no-manifest")
        os.makedirs(empty)
        self.assertEqual(self.gui._installed_version(empty), "未知")

    def _run_finish(self, repair, ok=True):
        app = self._app(installed=None)
        app._repair_mode = repair
        app._finish(ok, False, 0)
        text = app.flog.get("1.0", "end")
        app.destroy()
        return text

    def test_repair_finish_wording(self):
        text = self._run_finish(True)
        self.assertIn("检测修复完成", text)
        self.assertNotIn("安装完成，已生成", text)

    def test_install_finish_wording(self):
        text = self._run_finish(False)
        self.assertIn("安装完成", text)

    # ---- 检测修复两阶段（检测 → 用户确认 → 修复）按钮状态 ----
    def test_checking_state(self):
        """检测阶段：主按钮禁用显示「检测中…」，左侧仅保留「停止」，无确认按钮。"""
        root = self._make_installed_root()
        app = self._app(installed=root)
        try:
            app._set_buttons("checking")
            self.assertFalse(app.btn_primary._enabled)
            self.assertEqual(app.btn_primary._text, "检测中…")
            self.assertTrue(_mapped(app.btn_stop))
            self.assertFalse(_mapped(app.btn_repair))
            self.assertFalse(_mapped(app.btn_update))
            self.assertFalse(_mapped(app.btn_uninstall))
            self.assertFalse(_mapped(app.btn_open))
            self.assertFalse(_mapped(app.btn_confirm_fix))
        finally:
            app.destroy()

    def test_check_done_shows_confirm(self):
        """检测完成：展示「开始修复」等待用户确认，主按钮隐藏。"""
        root = self._make_installed_root()
        app = self._app(installed=root)
        try:
            app._set_buttons("check_done")
            self.assertTrue(_mapped(app.btn_confirm_fix))
            self.assertEqual(app.btn_confirm_fix._text, "开始修复")
            self.assertTrue(_mapped(app.btn_open))
            self.assertTrue(_mapped(app.btn_repair))
            self.assertFalse(_mapped(app.btn_primary))
        finally:
            app.destroy()

    def test_repair_running_wording(self):
        """修复运行中：主按钮文案「修复中…」（区别于安装的「安装中…」）。"""
        root = self._make_installed_root()
        app = self._app(installed=root)
        try:
            app._repair_mode = True
            app._set_buttons("running")
            self.assertEqual(app.btn_primary._text, "修复中…")
            self.assertFalse(_mapped(app.btn_confirm_fix))
            self.assertTrue(_mapped(app.btn_pause))
            self.assertTrue(_mapped(app.btn_stop))
        finally:
            app.destroy()

    def test_on_check_done_transitions(self):
        """检测完成后：记录安装目录、退出检测态、展示确认按钮。"""
        root = self._make_installed_root()
        app = self._app(installed=root)
        try:
            app._startup_state()
            app._on_check_done(root)
            self.assertEqual(app._repair_root, root)
            self.assertFalse(app._checking)
            self.assertTrue(_mapped(app.btn_confirm_fix))
            self.assertIn("请确认", app.pill._text)
        finally:
            app.destroy()

    def test_on_check_done_no_repair_needed(self):
        """检测完成但无需修复：不展示「开始修复」，回到完成状态（主按钮恢复）。"""
        root = self._make_installed_root()
        app = self._app(installed=root)
        try:
            app._startup_state()
            app._on_check_done(root, need=False)
            self.assertEqual(app._repair_root, root)
            self.assertFalse(app._checking)
            self.assertFalse(_mapped(app.btn_confirm_fix))
            self.assertEqual(app.btn_primary._text, "重新安装")
            for b in (app.btn_repair, app.btn_update, app.btn_uninstall,
                      app.btn_open):
                self.assertTrue(_mapped(b), f"{b._text} 未显示")
            self.assertIn("无需修复", app.pill._text)
        finally:
            app.destroy()

    def test_start_repair_requires_real_install(self):
        """检测修复入口校验：目录非真实安装（无清单/无 venv）→ 提示且不启动检测。"""
        app = self._app(installed=None)
        app.path_var.set("")                      # 模拟未填写/空目录
        warns = []
        self.gui.messagebox.showwarning = lambda *a, **k: warns.append(a)
        try:
            app._start_repair()
            self.assertEqual(len(warns), 1)
            self.assertFalse(app._checking)
            self.assertFalse(_mapped(app.btn_confirm_fix))
        finally:
            app.destroy()

    def test_start_repair_accepts_manifest_only(self):
        """仅安装清单存在（venv 缺失的损坏安装）也应允许检测修复。"""
        root = self._make_installed_root()
        app = self._app(installed=None)
        app.path_var.set(root)
        warns = []
        self.gui.messagebox.showwarning = lambda *a, **k: warns.append(a)
        app._run_check = lambda root: None         # 打桩：只验证入口放行，不跑真实检测
        app._save_prefs = lambda: None            # 不覆盖用户真实偏好（path 指向临时目录）
        try:
            app._start_repair()                   # 只应触发检测，不弹提示
            self.assertEqual(warns, [])
            self.assertTrue(app._checking)
            self.assertEqual(app._repair_root, root)
        finally:
            app.destroy()

    def test_check_update_bg_prompts_inline_on_file_diff(self):
        """启动后台检查以文件差异为准：有差异 → 主界面内联提示（不弹新窗口）。"""
        root = self._make_installed_root()
        app = self._app(installed=root)
        try:
            import updater
            real_check = updater.check
            updater.check = lambda r: {
                "up_to_date": False,
                "remote_version": "1.0.0.202609051215",
                "local_version": "1.0.0",
                "remote_created": "2026-09-05 12:00:00",
                "local_created": "",
                "added": ["app.py"], "changed": [], "total": 1,
                "manifest": {"version": "1.0.0.202609051215", "files": {}}}
            calls = []
            app._inline_update_hint = lambda d: calls.append(d)
            app._check_update_bg = self.gui.Installer._check_update_bg.__get__(app)
            app._check_update_bg(root)
            app.update()                          # 触发 after 回调
            self.assertEqual(len(calls), 1)
            self.assertFalse(calls[0]["up_to_date"])
            self.assertEqual(calls[0]["remote_version"], "1.0.0.202609051215")
        finally:
            updater.check = real_check
            app.destroy()

    def test_check_update_bg_no_prompt_when_up_to_date(self):
        """本地版本带构建时间戳、文件与远端一致 → 不触发内联提示。"""
        root = self._make_installed_root()
        app = self._app(installed=root)
        try:
            import updater
            real_check = updater.check
            updater.check = lambda r: {
                "up_to_date": True,
                "remote_version": "1.0.0.202609051215",
                "local_version": "1.0.0.202609051215",
                "remote_created": "2026-09-05 12:00:00",
                "local_created": "2026-09-05 12:00:00"}
            calls = []
            app._inline_update_hint = lambda d: calls.append(d)
            app._check_update_bg = self.gui.Installer._check_update_bg.__get__(app)
            app._check_update_bg(root)
            app.update()
            self.assertEqual(calls, [])
        finally:
            updater.check = real_check
            app.destroy()

    # ---- 更新流程内联化（检查更新 → 立即更新，不弹新窗口）----
    def test_update_check_done_new_version(self):
        """检查更新发现新版本：主按钮变「立即更新」，徽章提示发现新版本。"""
        root = self._make_installed_root("1.0.0")
        app = self._app(installed=root)
        try:
            app._update_mode = True
            app._on_update_done({
                "up_to_date": False, "remote_version": "1.1.0",
                "local_version": "1.0.0", "total": 2,
                "added": ["a.py"], "changed": ["b.py"]})
            self.assertEqual(app.btn_primary._text, "立即更新")
            self.assertTrue(app.btn_primary._enabled)
            self.assertTrue(app._update_ready)
            self.assertIn("发现新版本 v1.1.0", app.pill._text)
            self.assertFalse(_mapped(app.btn_confirm_fix))
        finally:
            app.destroy()

    def test_update_check_done_up_to_date(self):
        """检查更新已最新：徽章绿勾「已是最新版本」，主按钮「重新安装」。"""
        root = self._make_installed_root("1.1.0")
        app = self._app(installed=root)
        try:
            app._update_mode = True
            app._on_update_done({
                "up_to_date": True, "remote_version": "1.1.0",
                "local_version": "1.1.0", "total": 1,
                "added": [], "changed": []})
            self.assertEqual(app.btn_primary._text, "重新安装")
            self.assertIn("已是最新版本", app.pill._text)
            self.assertFalse(app._update_ready)
        finally:
            app.destroy()

    def test_update_running_hides_pause_stop(self):
        """更新运行中：无暂停/停止（差异下载无取消接口），维护按钮置灰展示。"""
        root = self._make_installed_root()
        app = self._app(installed=root)
        try:
            app._update_mode = True
            app._set_buttons("running")
            self.assertEqual(app.btn_primary._text, "更新中…")
            self.assertFalse(app.btn_primary._enabled)
            self.assertFalse(_mapped(app.btn_pause))
            self.assertFalse(_mapped(app.btn_stop))
            self.assertTrue(_mapped(app.btn_repair))
            self.assertFalse(app.btn_repair._enabled)
            self.assertFalse(app.btn_update._enabled)
            # 离开更新运行中后维护按钮恢复可用
            app._set_buttons("done")
            self.assertTrue(app.btn_repair._enabled)
            self.assertTrue(app.btn_update._enabled)
        finally:
            app.destroy()

    def test_update_finish_failure_keeps_retry(self):
        """更新执行失败：保留「立即更新」可重试。"""
        root = self._make_installed_root()
        app = self._app(installed=root)
        try:
            app._checking = False
            app._update_mode = True
            app._update_ready = False
            app._on_update_finish(False)
            self.assertTrue(app._update_ready)
            self.assertEqual(app.btn_primary._text, "立即更新")
            self.assertIn("更新失败", app.pill._text)
        finally:
            app.destroy()

    def test_check_update_failure_resets_to_idle(self):
        """检查更新失败：无差异清单不可执行，恢复常规状态而非「立即更新」。"""
        root = self._make_installed_root()
        app = self._app(installed=root)
        try:
            app._checking = True
            app._on_update_finish(False)
            self.assertFalse(app._update_ready)
            self.assertEqual(app.btn_primary._text, "重新安装")
            self.assertIn("检查更新失败", app.pill._text)
        finally:
            app.destroy()

    def test_cancel_wait_update_back_to_ready(self):
        """取消等待更新：徽章回到「发现新版本」，主按钮「立即更新」。"""
        root = self._make_installed_root()
        app = self._app(installed=root)
        try:
            app._update_mode = True
            app._update_diff = {"remote_version": "2.0.0", "up_to_date": False}
            app._waiting_update = True
            app._cancel_wait_update()
            self.assertFalse(app._waiting_update)
            self.assertTrue(app._update_ready)
            self.assertEqual(app.btn_primary._text, "立即更新")
            self.assertIn("发现新版本 v2.0.0", app.pill._text)
        finally:
            app.destroy()

    def test_pill_kind_icons(self):
        """Pill 徽章按 kind 绘制不同图标（success=✓ / error=✗ / accent=旋转 / warn=!）。"""
        app = self._app(installed=None)
        try:
            pill = app.pill
            for kind in ("success", "error", "accent", "warn", "busy", "idle"):
                pill.set(f"状态-{kind}", kind)
                self.assertEqual(pill._kind, kind)
                self.assertTrue(pill.find_all())
            pill.set("进行中", "accent")
            pill.pulse()          # 动画步进不抛错
            # 旧签名 (颜色, 文本) 仍兼容
            app._set_status(self.gui.SUCCESS, "旧签名")
            self.assertEqual(app.pill._kind, "success")
        finally:
            app.destroy()

    def test_needs_repair_core_only(self):
        """只有核心组件（app/venv/models）异常才算需要修复；
        cuda/shortcut/uv 等可选增强项仅展示状态，不触发「开始修复」。"""
        nr = self.gui._needs_repair
        for cid in ("app", "venv", "models"):
            self.assertTrue(nr(cid, "wait"), cid)
            self.assertTrue(nr(cid, "fail"), cid)
            self.assertFalse(nr(cid, "ok"), cid)
            self.assertFalse(nr(cid, "installing"), cid)
        for cid in ("cuda", "shortcut", "uv"):
            self.assertFalse(nr(cid, "wait"), cid)
            self.assertFalse(nr(cid, "fail"), cid)

    def test_cleanup_skipped_during_check(self):
        """检测阶段停止不清理（只读未产生半成品，不能误删已装 venv）。"""
        root = self._make_installed_root()
        os.makedirs(os.path.join(root, "runtime", "venv"))
        app = self._app(installed=root)
        try:
            app._checking = True
            app._cleanup_after_stop(root)
            self.assertTrue(os.path.isdir(os.path.join(root, "runtime", "venv")))
        finally:
            app.destroy()

    # ---- 运行中应用检测 / 关闭（覆盖主程序前释放文件锁）----
    def test_find_app_procs_empty(self):
        app = self._app(installed=None)
        try:
            self.assertEqual(app._find_app_procs(self.dir), [])
        finally:
            app.destroy()

    def test_close_running_app_none(self):
        app = self._app(installed=None)
        try:
            self.assertTrue(app._close_running_app(self.dir))  # 无进程，直接放行
        finally:
            app.destroy()

    def test_close_running_app_cancel(self):
        app = self._app(installed=None)
        app._find_app_procs = lambda root: [(1234, r"C:\MinerU_App\MinerU文档解析.exe")]
        self.gui.messagebox.askyesno = lambda *a, **k: False
        try:
            self.assertFalse(app._close_running_app(r"C:\MinerU_App"))
        finally:
            app.destroy()

    def test_close_running_app_confirm(self):
        app = self._app(installed=None)
        app._find_app_procs = lambda root: [
            (1234, r"C:\MinerU_App\MinerU文档解析\MinerU文档解析.exe"),
            (5678, r"C:\MinerU_App\runtime\venv\Scripts\python.exe")]
        self.gui.messagebox.askyesno = lambda *a, **k: True
        app._append_log = lambda *a, **k: None
        calls = []
        orig_sub = self.gui.subprocess
        self.gui.subprocess = type("S", (), {"run": staticmethod(
            lambda *a, **k: calls.append(a[0]))})
        try:
            self.assertTrue(app._close_running_app(r"C:\MinerU_App"))
            self.assertEqual(len(calls), 2)  # 两个进程各 taskkill 一次
            for c in calls:
                self.assertIn("/T", c)
                self.assertIn("/F", c)
        finally:
            self.gui.subprocess = orig_sub
            app.destroy()


if __name__ == "__main__":
    unittest.main(verbosity=2)

