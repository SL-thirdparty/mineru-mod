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
        self.assertEqual(evs["venv"], ("wait", "已存在，需补装依赖"))
        self.assertEqual(evs["models"], ("wait", "2/40 文件已就绪，还需下载 38"))

    def test_complete_deps(self):
        # venv 完整 + state 含 deps → venv ok；模型全在 → ok（断点续传）
        os.makedirs(os.path.join(self.dir, "runtime", "venv", "Scripts"))
        os.makedirs(os.path.join(self.dir, "runtime", "venv", "Lib", "site-packages"))
        open(os.path.join(self.dir, "runtime", "venv", "Scripts", "python.exe"),
             "w").close()
        with open(os.path.join(self.dir, ".install_state.json"), "w",
                  encoding="utf-8") as f:
            json.dump({"steps": ["copy", "deps"]}, f)
        kit = os.path.join(self.dir, "runtime", "models_cache", "models",
                           "OpenDataLab--PDF-Extract-Kit-1.0", "snapshots", "master")
        for fp, size, _ in install_flow.MODEL_FILES:
            p = os.path.join(kit, fp.replace("/", os.sep))
            os.makedirs(os.path.dirname(p), exist_ok=True)
            with open(p, "wb") as f:
                f.write(b"x" * size)
        evs = self._events()
        self.assertEqual(evs["venv"], ("ok", "环境与依赖已就绪（跳过）"))
        self.assertEqual(evs["models"][0], "ok")
        self.assertIn("断点续传", evs["models"][1])


class _FakePanel:
    """替身：记录 set_comp 调用。"""

    def __init__(self):
        self.calls = []

    def set_comp(self, cid, status, detail):
        self.calls.append((cid, status, detail))


class TestGuiHandleComp(unittest.TestCase):
    """installer_gui._handle_comp 解析层（不实例化完整窗口）。"""

    @classmethod
    def setUpClass(cls):
        import installer_gui
        cls.installer_gui = installer_gui
        gui = installer_gui.Installer.__new__(installer_gui.Installer)
        gui.comps = _FakePanel()
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

    def test_worker_tag_dispatch(self):
        """_run 的 [tag] 行正则必须把 comp 归入高频事件（不进日志）。"""
        pat = self.installer_gui.re.compile(r"^\[([a-z]+)\]\s?(.*)$")
        m = pat.match("[comp] models|ok|已就绪（40 文件 · 2.42 GB）")
        self.assertIsNotNone(m)
        self.assertEqual(m.group(1), "comp")
        self.assertEqual(m.group(2), "models|ok|已就绪（40 文件 · 2.42 GB）")


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
        self.assertEqual(panel._state["venv"][0], "installing")
        self.assertIn("torch", panel._state["venv"][1])
        panel.set_model_feed(12, 40, 0.75, 2.42, 8.3, "model.safetensors")
        self.assertEqual(panel._state["models"][0], "installing")
        self.assertIn("12/40", panel._state["models"][1])


if __name__ == "__main__":
    unittest.main(verbosity=2)
