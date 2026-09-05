# -*- coding: utf-8 -*-
"""安装目录结构化布局测试（修复"乱七八糟的位置"与组件清单压盖）。

覆盖：
  1) comp_panel._clip 文本截断边界（空/短/超长/过窄）
  2) _Row 绘制无压盖：名称与详情的实际渲染 bbox 不重叠（用户截图回归用例）
  3) Installer 缓存重定向：UV_CACHE_DIR/PIP_CACHE_DIR → <root>/runtime/pkg_cache/
  4) Diagnostics 诊断日志落安装根 logs/（与托盘/WebUI 日志目录一致，不在 runtime/_data）
  5) 卸载器已知清单含 pkg_cache/wheel_cache（新版缓存目录可被卸载清理）

用法（项目根）:
    runtime\\venv\\Scripts\\python.exe tests\\test_install_layout.py
"""
import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for p in (os.path.join(ROOT, "src", "installer"),
          os.path.join(ROOT, "scripts")):
    if os.path.isdir(p) and p not in sys.path:
        sys.path.insert(0, p)


class TestClip(unittest.TestCase):
    def test_clip_boundaries(self):
        try:
            import tkinter as tk
            root = tk.Tk()
            root.withdraw()
        except Exception:
            raise unittest.SkipTest("无 tk 显示环境")
        try:
            from comp_panel import _clip
            import tkinter.font as tkfont
            fm = tkfont.Font(font=("Microsoft YaHei UI", 9))
            self.assertEqual(_clip("", fm, 100), "")
            self.assertEqual(_clip("abc", fm, 400), "abc")
            long_txt = "NVIDIA GeForce RTX 4070 Laptop GPU（将装 CUDA 12.8）"
            clipped = _clip(long_txt, fm, 120)
            self.assertTrue(clipped.endswith("…"))
            self.assertLessEqual(fm.measure(clipped), 120)
            self.assertLess(len(clipped), len(long_txt))
            # 可用宽度过小 → 宁缺勿盖
            self.assertEqual(_clip(long_txt, fm, 8), "")
        finally:
            root.destroy()


class TestRowNoOverlap(unittest.TestCase):
    """用户截图回归：长详情文本不得压盖名称（"GPU加速VIDIA…"混排）。"""

    @classmethod
    def setUpClass(cls):
        try:
            import tkinter as tk
            cls.root = tk.Tk()
            cls.root.withdraw()
        except Exception:
            raise unittest.SkipTest("无 tk 显示环境")

    @classmethod
    def tearDownClass(cls):
        try:
            cls.root.destroy()
        except Exception:
            pass

    def _render(self, width, detail, needle):
        from comp_panel import _Row
        row = _Row(self.root, "cuda", "GPU 加速", lambda c: None)
        row.configure(width=width, height=34)
        row.pack()
        self.root.update()
        row.set("installing", detail)
        name_box = detail_box = None
        for item in row.find_all():
            if row.type(item) != "text":
                continue
            txt = row.itemcget(item, "text")
            box = row.bbox(item)
            if not box:
                continue
            if txt.startswith("GPU"):
                name_box = box
            elif needle in txt:
                detail_box = box
        return name_box, detail_box

    def test_long_detail_does_not_cover_name(self):
        for width in (560, 480, 420, 360):
            detail = "NVIDIA GeForce RTX 4070 Laptop GPU（将装 CUDA 12.8）"
            name_box, detail_box = self._render(width, detail, "NVIDIA")
            self.assertIsNotNone(name_box, f"名称未渲染（w={width}）")
            self.assertIsNotNone(detail_box, f"详情未渲染（w={width}）")
            # 名称右端必须在详情左端之前（留出间距）
            self.assertLessEqual(name_box[2] + 6, detail_box[0],
                                 f"名称与详情压盖（w={width}）："
                                 f"name_x2={name_box[2]} detail_x1={detail_box[0]}")

    def test_short_detail_fits(self):
        _, detail_box = self._render(560, "探测显卡与 CUDA 决策", "探测")
        self.assertIsNotNone(detail_box)


class TestCacheRedirect(unittest.TestCase):
    def setUp(self):
        self._saved = {k: os.environ.get(k) for k in ("UV_CACHE_DIR", "PIP_CACHE_DIR")}

    def tearDown(self):
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def test_installer_redirects_pkg_cache(self):
        import install_mineru_uv as imu
        with tempfile.TemporaryDirectory() as root:
            imu.Installer(root, None)
            self.assertEqual(
                os.environ["UV_CACHE_DIR"],
                os.path.join(root, "runtime", "pkg_cache", "uv"))
            self.assertEqual(
                os.environ["PIP_CACHE_DIR"],
                os.path.join(root, "runtime", "pkg_cache", "pip"))
            self.assertTrue(os.path.isdir(os.environ["UV_CACHE_DIR"]))
            self.assertTrue(os.path.isdir(os.environ["PIP_CACHE_DIR"]))

    def test_diagnostics_log_in_structured_dir(self):
        import install_mineru_uv as imu
        with tempfile.TemporaryDirectory() as root:
            diag = imu.Diagnostics(root)
            self.assertEqual(
                os.path.dirname(diag.path),
                os.path.join(root, "logs"))
            self.assertTrue(os.path.basename(diag.path).startswith("sysdiag_"))


class TestUninstallerTargets(unittest.TestCase):
    def test_removes_pkg_and_wheel_cache(self):
        try:
            import uninstaller_gui
        except Exception:
            raise unittest.SkipTest("tkinter 不可用")
        with tempfile.TemporaryDirectory() as root:
            for rel in (os.path.join("runtime", "pkg_cache", "uv", "x.bin"),
                        os.path.join("runtime", "wheel_cache", "torch.whl"),
                        os.path.join("runtime", "_data", "logs", "sysdiag_t.log"),
                        os.path.join("logs", "MinerU_2026-09-05.log")):
                p = os.path.join(root, rel)
                os.makedirs(os.path.dirname(p), exist_ok=True)
                with open(p, "wb") as f:
                    f.write(b"x")
            uninstaller_gui.remove_files(root, keep_venv=True, keep_model=True)
            for rel in ("runtime/pkg_cache", "runtime/wheel_cache",
                        "runtime/_data", "logs"):
                self.assertFalse(os.path.exists(os.path.join(root, rel)),
                                f"卸载未清理 {rel}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
