# -*- coding: utf-8 -*-
"""P2 组件清单可视化事件管线测试。

覆盖：
  1) install_flow.comp() 输出格式
  2) precheck 三场景（空目录 / 半成品 / state 含 deps）
  3) installer_gui._handle_comp 解析（合法 / 非法行容错）
  4) CompPanel 状态流转与计数聚合（tk 可用时）
  5) CompPanel 未知组件 id 容错
  6) [comp] 行在 _run 事件正则下的分发（worker 端 tag 解析）

用法（项目根）:
    runtime\\venv\\Scripts\\python.exe -m pytest tests\\test_comp_events.py -q
    # 或直接运行：
    runtime\\venv\\Scripts\\python.exe tests\\test_comp_events.py
"""
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for p in (os.path.join(ROOT, "src", "installer"),
          os.path.join(ROOT, "scripts")):
    if os.path.isdir(p) and p not in sys.path:
        sys.path.insert(0, p)

import install_flow  # noqa: E402

# 6 个后端组件（python 由 GUI 端产生，不在 precheck 输出中）
BACKEND_COMPS = {"uv", "app", "venv", "cuda", "models", "shortcut"}


def _capture(fn, *a, **kw):
    buf = io.StringIO()
    with redirect_stdout(buf):
        fn(*a, **kw)
    return buf.getvalue()


def _comp_events(out):
    """从 stdout 文本提取 [comp] 事件 → dict id → (status, detail)。"""
    evs = {}
    for ln in out.splitlines():
        m = re.match(r"^\[comp\] (\w+)\|(\w+)\|(.*)$", ln)
        if m:
            evs[m.group(1)] = (m.group(2), m.group(3))
    return evs


class TestCompEmit(unittest.TestCase):

    def test_format(self):
        out = _capture(install_flow.comp, "app", "installing", "正在复制 …")
        self.assertEqual(out.strip(), "[comp] app|installing|正在复制 …")


class TestPrecheck(unittest.TestCase):

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        # 本机桌面可能遗留历史快捷方式，影响 shortcut 组件断言：
        # 将 _desktop_dir 重定向到临时目录，测试后恢复（不触碰真实桌面）
        self._desk = tempfile.mkdtemp()
        self._orig_desktop = install_flow._desktop_dir
        install_flow._desktop_dir = lambda: self._desk

    def tearDown(self):
        install_flow._desktop_dir = self._orig_desktop
        shutil.rmtree(self._desk, ignore_errors=True)
        shutil.rmtree(self.dir, ignore_errors=True)

    def _events(self):
        return _comp_events(_capture(install_flow.precheck, self.dir))

    def test_empty_root(self):
        evs = self._events()
        self.assertEqual(set(evs), BACKEND_COMPS)
        self.assertEqual(evs["app"], ("wait", "待复制"))
        self.assertEqual(evs["venv"][0], "wait")
        self.assertIn("待创建", evs["venv"][1])
        self.assertEqual(evs["models"][0], "wait")
        self.assertEqual(evs["shortcut"], ("wait", "待创建"))
        self.assertEqual(evs["uv"][0], "ok")          # 本机 uv 状态，恒 ok
        # cuda 预检恒 wait（决策在依赖阶段之后）
        self.assertEqual(evs["cuda"][0], "wait")

    def test_partial_root(self):
        # 半成品：venv 骨架 + 2 个尺寸正确的模型文件
        os.makedirs(os.path.join(self.dir, "runtime", "venv", "Scripts"))
        os.makedirs(os.path.join(self.dir, "runtime", "venv", "Lib", "site-packages"))
        open(os.path.join(self.dir, "runtime", "venv", "Scripts", "python.exe"),
             "w").close()
        kit = os.path.join(self.dir, "runtime", "models_cache", "models",
                           "OpenDataLab--PDF-Extract-Kit-1.0", "snapshots", "master")
        for i in (0, 2):
            fp, size, _ = install_flow.MODEL_FILES[i]
            p = os.path.join(kit, fp.replace("/", os.sep))
            os.makedirs(os.path.dirname(p), exist_ok=True)
            with open(p, "wb") as f:
                f.write(b"x" * size)
        evs = self._events()
        self.assertEqual(evs["venv"], ("wait", "已存在，需补装依赖：mineru、pywin32、pystray"))
        self.assertEqual(evs["models"], ("wait", "2/40 文件已就绪，还需下载 38"))

    def test_complete_deps(self):
        # 依赖真实装齐（site-packages 含三个直接依赖 dist-info）→ venv ok；
        # 判据为实际安装状态探测，不再依赖 .install_state.json（安装成功后会被删除）
        os.makedirs(os.path.join(self.dir, "runtime", "venv", "Scripts"))
        sp = os.path.join(self.dir, "runtime", "venv", "Lib", "site-packages")
        os.makedirs(sp)
        open(os.path.join(self.dir, "runtime", "venv", "Scripts", "python.exe"),
             "w").close()
        for pkg in ("mineru-3.4.5.dist-info", "pywin32-306.dist-info",
                    "pystray-0.19.5.dist-info"):
            os.makedirs(os.path.join(sp, pkg))
        kit = os.path.join(self.dir, "runtime", "models_cache", "models",
                           "OpenDataLab--PDF-Extract-Kit-1.0", "snapshots", "master")
        for fp, size, _ in install_flow.MODEL_FILES:
            p = os.path.join(kit, fp.replace("/", os.sep))
            os.makedirs(os.path.dirname(p), exist_ok=True)
            with open(p, "wb") as f:
                f.write(b"x" * size)
        evs = self._events()
        self.assertEqual(evs["venv"], ("ok", "环境与依赖已就绪（检测通过）"))
        self.assertEqual(evs["models"][0], "ok")
        self.assertIn("断点续传", evs["models"][1])


class _FakePanel:
    """替身：记录 set_comp 调用。"""

    def __init__(self):
        self.calls = []

    def set_comp(self, cid, status, detail):
        self.calls.append((cid, status, detail))

    def mark_pkg_all_downloaded(self):
        self.calls.append(("venv", "_mark_downloaded_all", ""))


class TestGuiHandleComp(unittest.TestCase):
    """installer_gui._handle_comp 解析层（不实例化完整窗口）。"""

    @classmethod
    def setUpClass(cls):
        import installer_gui
        cls.installer_gui = installer_gui
        gui = installer_gui.Installer.__new__(installer_gui.Installer)
        gui.comps = _FakePanel()
        # _handle_comp 现会写日志（组件状态全量落盘）；测试不建窗口，直接吞掉
        gui._append_log = lambda *a, **k: None
        cls.gui = gui

    def test_valid_line(self):
        self.gui.comps.calls.clear()
        self.installer_gui.Installer._handle_comp(self.gui, "app|installing|正在复制 …")
        self.assertEqual(self.gui.comps.calls,
                         [("app", "installing", "正在复制 …")])

    def test_malformed_lines_no_raise(self):
        # 破损/未知状态行在解析层被挡；未知 cid 由面板层过滤（见 test_unknown_cid_ignored）
        for bad in ("", "app", "app|ok", "app|ok|d1|d2", "||", "app|badstat|x"):
            self.installer_gui.Installer._handle_comp(self.gui, bad)  # 不抛异常
        self.assertEqual(self.gui.comps.calls, [])  # 全部被忽略

    def test_new_states_accepted(self):
        """checking/downloading 状态被解析层接受并直达组件面板。"""
        self.gui.comps.calls.clear()
        H = self.installer_gui.Installer._handle_comp
        H(self.gui, "venv|checking|检测中…")
        H(self.gui, "venv|downloading|正在下载 torch")
        self.assertEqual(self.gui.comps.calls,
                         [("venv", "checking", "检测中…"),
                          ("venv", "downloading", "正在下载 torch")])

    def test_worker_tag_dispatch(self):
        """_run 的 [tag] 行正则必须把 comp 归入高频事件（不进日志）。"""
        pat = self.installer_gui.re.compile(r"^\[([a-z]+)\]\s?(.*)$")
        m = pat.match("[comp] models|ok|已就绪（40 文件 · 2.42 GB）")
        self.assertIsNotNone(m)
        self.assertEqual(m.group(1), "comp")
        self.assertEqual(m.group(2), "models|ok|已就绪（40 文件 · 2.42 GB）")


class _FakePkgGui:
    """_handle_pkg 测试替身：记录 comps 状态、活动行、日志。"""

    def __init__(self):
        self.comps = _FakePanel()
        self.comps.feed = []
        self.comps.set_pkg_feed = (
            lambda name, size, idx, total: self.comps.feed.append(
                (name, size, idx, total)))
        self.activities = []
        self.logs = []
        self._pkg_total = None
        self._pkg_done = 0
        self._pkg_names = set()

    def _set_activity(self, txt, **_kw):
        self.activities.append(txt)

    def _append_log(self, txt, *_a, **_kw):
        self.logs.append(txt)

    def _advance_progress(self, _v):
        pass


class TestGuiHandlePkgStage(unittest.TestCase):
    """_handle_pkg 阶段映射：解析→下载中→安装中；ok 由 [comp] venv|ok 收尾。"""

    @classmethod
    def setUpClass(cls):
        import installer_gui
        cls.installer_gui = installer_gui

    def _pkg(self, g, rest):
        # 经类访问取未绑定函数（实例访问会把 TestCase 自身绑成 self）
        self.installer_gui.Installer._handle_pkg(g, rest)

    def test_stage_mapping(self):
        g = _FakePkgGui()
        self._pkg(g, "resolved|110|1.2s")
        self.assertEqual(g.comps.calls[-1],
                         ("venv", "downloading", "已解析 110 个依赖包，开始下载 …"))
        self._pkg(g, "down|torch|900.5 MB")
        self.assertEqual(g.comps.calls[-1][1], "downloading")
        self.assertIn("torch", g.comps.calls[-1][2])
        self.assertIn("900.5 MB", g.comps.calls[-1][2])
        self._pkg(g, "down|pymupdf|12.3 MB")
        self.assertEqual(g.comps.calls[-1][1], "downloading")
        self._pkg(g, "prepared|110|40s")
        self.assertEqual(g.comps.calls[-1][1], "installing")
        self._pkg(g, "installing|torch")
        self.assertEqual(g.comps.calls[-1][1], "installing")
        self._pkg(g, "installed|110|1m")
        # installed 只写日志，不把 venv 置 ok（等 [comp] venv|ok 收尾）
        self.assertEqual(g.comps.calls[-1][1], "installing")
        self.assertEqual(len(g.logs), 1)
        self.assertEqual(g.comps.feed[0][0], "torch")

    def test_pip_path_without_resolved(self):
        """pip 回退无 resolved 事件（_pkg_total=0）：不显示「第 N/0 个包」。"""
        g = _FakePkgGui()
        self._pkg(g, "down|torch|900.5 MB")
        self.assertEqual(g.comps.calls[-1][1], "downloading")
        self.assertNotIn("/0", g.comps.calls[-1][2])
        self.assertIn("torch", g.comps.calls[-1][2])

    def test_duplicate_down_no_dup_feed(self):
        """同一包重复 down 事件：明细只记首次，不重复加行、不重复推进计数。"""
        g = _FakePkgGui()
        self._pkg(g, "resolved|110|1.2s")
        self._pkg(g, "down|torch|900.5 MB")
        self._pkg(g, "down|torch|900.5 MB")   # pip 分片/重试导致的重复
        self._pkg(g, "down|pymupdf|12.3 MB")
        self.assertEqual(len(g.comps.feed), 2)
        self.assertEqual(g.comps.feed[0], ("torch", "900.5 MB", 1, 110))
        self.assertEqual(g.comps.feed[1], ("pymupdf", "12.3 MB", 2, 110))


class TestPathHintNoMkdir(unittest.TestCase):
    """_refresh_path_hint 只检查不创建目录（回归：逐字符输入不再新建目录）。"""

    @classmethod
    def setUpClass(cls):
        import installer_gui
        cls.installer_gui = installer_gui

    def setUp(self):
        gui = self.installer_gui.Installer.__new__(self.installer_gui.Installer)

        class _Lbl:
            def config(self, **kw):
                self.kw = kw

        gui.path_hint_icon = _Lbl()
        gui.path_hint = _Lbl()
        gui.path_var = type("_V", (), {"get": lambda self: self.v})()
        self.gui = gui
        self.dir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def _hint(self, p):
        self.gui.path_var.v = p
        self.installer_gui.Installer._refresh_path_hint(self.gui)
        return self.gui.path_hint.kw

    def test_typing_intermediate_creates_nothing(self):
        # 逐字符输入路径的中间状态（…App / …App_ / …App_S）不得创建目录
        p = os.path.join(self.dir, "MinerU_App")
        for i in range(4):
            self._hint(p + "_" * i)
        self.assertEqual(os.listdir(self.dir), [])

    def test_deep_path_no_creation_and_safe_hint(self):
        kw = self._hint(os.path.join(self.dir, "NewDir", "Sub"))
        self.assertNotEqual(kw["fg"], self.installer_gui.DANGER)
        self.assertFalse(os.path.isdir(os.path.join(self.dir, "NewDir")))

    def test_file_path_is_error(self):
        fp = os.path.join(self.dir, "a_file.txt")
        open(fp, "w").close()
        kw = self._hint(fp)
        self.assertEqual(kw["fg"], self.installer_gui.DANGER)
        self.assertIn("文件", kw["text"])

    def test_existing_dir_not_error(self):
        kw = self._hint(self.dir)
        self.assertNotEqual(kw["fg"], self.installer_gui.DANGER)


class TestCompPanel(unittest.TestCase):
    """CompPanel 状态流转 + 计数聚合（需 tk 显示环境，无则跳过）。"""

    @classmethod
    def setUpClass(cls):
        try:
            import tkinter as tk
            cls.tk = tk
            cls.root = tk.Tk()
            cls.root.withdraw()
        except Exception:
            raise unittest.SkipTest("无 tk 显示环境")
        from comp_panel import CompPanel, COMPS
        cls.CompPanel = CompPanel
        cls.comps_ids = [c for c, _ in COMPS]

    @classmethod
    def tearDownClass(cls):
        try:
            cls.root.destroy()
        except Exception:
            pass

    def _counts(self, panel):
        return panel._counts.cget("text")

    def test_flow_and_counts(self):
        from comp_panel import CompPanel
        panel = CompPanel(self.root)
        # 初始：7 项全 wait
        self.assertIn("7 项", self._counts(panel))
        self.assertIn("待安装 7", self._counts(panel))
        # 逐个 ok
        for cid in self.comps_ids:
            panel.set_comp(cid, "ok", "x")
        self.assertIn("已就绪 7", self._counts(panel))
        self.assertNotIn("待安装", self._counts(panel))
        # 一个失败 → 计数含 失败 1，字色变红
        panel.set_comp("models", "fail", "校验未通过")
        self.assertIn("失败 1", self._counts(panel))
        self.assertEqual(panel._state["models"], ("fail", "校验未通过"))
        # reset
        panel.reset()
        self.assertIn("待安装 7", self._counts(panel))

    def test_unknown_cid_ignored(self):
        from comp_panel import CompPanel
        panel = CompPanel(self.root)
        panel.set_comp("nonexistent", "ok", "x")  # 不抛异常、不改变计数
        self.assertIn("待安装 7", self._counts(panel))

    def test_feeds(self):
        from comp_panel import CompPanel
        panel = CompPanel(self.root)
        panel.set_pkg_feed("torch", "900.5 MB", 3, 110)
        # 明细只追加列表；venv 行状态由 GUI 按阶段驱动（resolved/down→downloading）
        self.assertEqual(panel._state["venv"][0], "wait")
        panel.set_model_feed(12, 40, 0.75, 2.42, 8.3, "model.safetensors")
        self.assertEqual(panel._state["models"][0], "downloading")
        self.assertIn("12/40", panel._state["models"][1])

    def test_checking_downloading_counts(self):
        from comp_panel import CompPanel
        panel = CompPanel(self.root)
        panel.set_all("checking", "检测中…")
        self.assertIn("检测中 7", self._counts(panel))
        panel.set_comp("venv", "downloading", "正在下载 torch")
        panel.set_comp("models", "downloading", "正在下载模型")
        self.assertIn("进行中 2", self._counts(panel))

    def test_pkg_feed_row_status(self):
        """venv 明细行状态：最新一行「… 下载中」，新行到来后上一行「✓ 已下载」。"""
        from comp_panel import CompPanel
        panel = CompPanel(self.root)
        feed = panel._feeds["venv"]
        panel.set_pkg_feed("torch", "900.5 MB", 1, 2)
        panel.set_pkg_feed("pymupdf", "12.3 MB", 2, 2)
        items = feed._venv_items
        self.assertEqual(len(items), 2)
        self.assertIn("✓ [1/2] torch", items[0][0].cget("text"))
        self.assertIn("… [2/2] pymupdf", items[1][0].cget("text"))
        # 进入安装阶段：全部行收尾为 ✓
        feed.mark_downloaded_all()
        self.assertIn("✓ [2/2] pymupdf", items[1][0].cget("text"))

    def test_venv_feed_finalize(self):
        """安装完成：venv 明细标题变（已完成），全部行 ✓。"""
        from comp_panel import CompPanel
        panel = CompPanel(self.root)
        feed = panel._feeds["venv"]
        panel.set_pkg_feed("torch", "900.5 MB", 1, 1)
        panel.finalize_feeds()
        self.assertIn("（已完成）", feed._title.cget("text"))
        self.assertIn("✓ [1/1] torch", feed._venv_items[0][0].cget("text"))

    def test_model_feed_finalize(self):
        """安装完成：模型明细不再停留「正在下载」，改为完成态文案。"""
        from comp_panel import CompPanel
        panel = CompPanel(self.root)
        feed = panel._feeds["models"]
        panel.set_model_feed(39, 40, 2.42, 2.42, 68.6, "PP-FormulaNet_plus-M.pth")
        self.assertIn("正在下载", feed._cur.cget("text"))
        self.assertEqual(panel._state["models"][0], "downloading")
        panel.finalize_feeds()
        self.assertIn("（已完成）", feed._title.cget("text"))
        self.assertIn("✓ 全部文件已下载", feed._cur.cget("text"))
        # finalize 本身不改行状态（行 ok 由 [comp] models|ok 事件驱动）
        self.assertEqual(panel._state["models"][0], "downloading")

    def test_model_feed_does_not_rollback(self):
        """心跳不把已就绪/失败/校验中的 models 行打回「下载中」。"""
        from comp_panel import CompPanel
        panel = CompPanel(self.root)
        panel.set_comp("models", "ok", "已就绪（40 文件 · 2.42 GB）")
        panel.set_model_feed(40, 40, 2.42, 2.42, 0.0, "")
        self.assertEqual(panel._state["models"][0], "ok")
        panel.set_comp("models", "installing", "下载完成，正在校验完整性 …")
        panel.set_model_feed(40, 40, 2.42, 2.42, 0.0, "")
        self.assertEqual(panel._state["models"][0], "installing")
        panel.set_comp("models", "fail", "校验未通过")
        panel.set_model_feed(39, 40, 2.42, 2.42, 0.0, "")
        self.assertEqual(panel._state["models"][0], "fail")


class TestTorchCudaInstalled(unittest.TestCase):
    """predownload_torch 的 CUDA torch 已装探测（避免修复时重复下载 ~3GB）。"""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.sp = os.path.join(self.dir, "runtime", "venv", "Lib", "site-packages")
        os.makedirs(self.sp, exist_ok=True)

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_cuda_torch_present(self):
        os.makedirs(os.path.join(self.sp, "torch-2.1.0+cu121.dist-info"))
        self.assertTrue(install_flow._torch_cuda_installed(self.dir))

    def test_cpu_torch_only(self):
        os.makedirs(os.path.join(self.sp, "torch-2.1.0+cpu.dist-info"))
        self.assertFalse(install_flow._torch_cuda_installed(self.dir))

    def test_no_torch(self):
        os.makedirs(os.path.join(self.sp, "numpy-1.26.0.dist-info"))
        self.assertFalse(install_flow._torch_cuda_installed(self.dir))

    def test_no_site_packages(self):
        shutil.rmtree(self.sp)
        self.assertFalse(install_flow._torch_cuda_installed(self.dir))


class _FakeBase:
    """predownload_torch 跳过测试用假 base（有 GPU，但不触发任何下载）。"""

    PYTORCH_INDEXES = []
    TORCH_WHEELS = {}

    def detect_gpu(self):
        return {"name": "RTX 4090", "driver_cuda": "12.1"}

    def pick_cuda(self, _driver_cuda):
        return "cu121"


class TestPredownloadSkip(unittest.TestCase):
    """修复模式：venv 已装 CUDA torch → predownload_torch 直接跳过（不下载 ~3GB）。"""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        sp = os.path.join(self.dir, "runtime", "venv", "Lib", "site-packages")
        os.makedirs(sp, exist_ok=True)
        os.makedirs(os.path.join(sp, "torch-2.1.0+cu121.dist-info"))
        self._orig_base = install_flow.base
        install_flow.base = _FakeBase()

    def tearDown(self):
        install_flow.base = self._orig_base
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_skips_predownload(self):
        out = _capture(install_flow.predownload_torch, self.dir)
        self.assertIn("跳过预下载", out)
        self.assertIn("[comp] cuda|ok|", out)
        self.assertIsNone(install_flow.predownload_torch(self.dir))
        # 未创建 wheel_cache（未发生任何下载）
        self.assertFalse(os.path.isdir(os.path.join(self.dir, "runtime", "wheel_cache")))

    def test_downloads_when_cpu_torch_only(self):
        """仅 CPU torch（+cpu）→ 不满足跳过条件（GPU 机器仍需 CUDA torch 预下载）。"""
        sp = os.path.join(self.dir, "runtime", "venv", "Lib", "site-packages")
        shutil.rmtree(os.path.join(sp, "torch-2.1.0+cu121.dist-info"), ignore_errors=True)
        os.makedirs(os.path.join(sp, "torch-2.1.0+cpu.dist-info"), exist_ok=True)
        # 无 fastdl → 回退 None（联网安装路径），但不会走到跳过分支
        out = _capture(install_flow.predownload_torch, self.dir)
        self.assertNotIn("跳过预下载", out)


class TestCheckOnlyMode(unittest.TestCase):
    """install_flow.py --check-only：仅输出组件状态，不修改安装目录任何内容。"""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        # 预置半成品（模拟已装环境），验证 --check-only 不触碰已有文件
        self.vpy = os.path.join(self.dir, "runtime", "venv", "Scripts", "python.exe")
        os.makedirs(os.path.dirname(self.vpy), exist_ok=True)
        open(self.vpy, "w").close()
        self.marker = os.path.join(self.dir, "已存在.txt")
        open(self.marker, "w").close()
        self._desk = tempfile.mkdtemp()
        self._orig_desktop = install_flow._desktop_dir
        install_flow._desktop_dir = lambda: self._desk

    def tearDown(self):
        install_flow._desktop_dir = self._orig_desktop
        shutil.rmtree(self._desk, ignore_errors=True)
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_check_only_readonly(self):
        flow = os.path.join(ROOT, "src", "installer", "install_flow.py")
        src = os.path.join(ROOT, "src")
        r = subprocess.run(
            [sys.executable, flow, "--root", self.dir, "--src", src,
             "--check-only", "--no-shortcut"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=120)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        out = r.stdout
        # 输出组件状态事件
        self.assertIn("[comp] venv|", out)
        self.assertIn("[comp] models|", out)
        # 输出「检测完成」结束行
        self.assertIn("[check]", out)
        self.assertIn("检测完成", out)
        # 只读：预置文件未被动过，且未产生任何新文件
        self.assertTrue(os.path.isfile(self.marker))
        self.assertTrue(os.path.isfile(self.vpy))
        leftover = []
        for dp, dns, fns in os.walk(self.dir):
            for fn in fns:
                p = os.path.join(dp, fn)
                if p != self.marker and p != self.vpy:
                    leftover.append(os.path.relpath(p, self.dir))
        self.assertEqual(leftover, [],
                         f"--check-only 不应产生任何文件: {leftover}")
        # 未写入安装状态/结果文件
        self.assertFalse(os.path.exists(os.path.join(self.dir, ".install_state.json")))
        self.assertFalse(os.path.exists(os.path.join(self.dir, "install_result.json")))


if __name__ == "__main__":
    unittest.main(verbosity=2)
