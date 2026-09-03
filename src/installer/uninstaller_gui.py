# -*- coding: utf-8 -*-
"""MinerU 卸载器（安装时随包分发到安装根目录，双击运行）。

功能：停止托盘/服务/引擎进程 → 删除桌面快捷方式 → 按勾选删除应用/虚拟环境/模型缓存 →
延迟自删除（运行中的 exe 无法直接删除自己，由分离的 cmd 延迟清理兜底）。

命令行：python uninstaller_gui.py [--root <安装根>]（开发调试用）。
"""
import argparse
import ctypes
import os
import queue
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
import tkinter as tk
from tkinter import font as tkfont

BG        = "#f4f6f8"
CARD      = "#ffffff"
INK       = "#182430"
MUTED     = "#6d7885"
FAINT     = "#9aa5b1"
ACCENT    = "#0e7490"
ACCENT_2  = "#14b8a6"
DANGER    = "#c0362c"
DANGER_D  = "#9c2b23"
TRACK     = "#e7ebee"
LINE      = "#dfe4e9"
OK_GREEN  = "#15803d"

_NO_WINDOW = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
APP_EXE_NAMES = ("MinerU文档解析.exe", "mineru_tray.exe")  # 新版 + 旧版兼容
PORT = 7860


def _enable_dpi_awareness():
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass


def _desktop_dirs():
    """用户桌面与公共桌面（桌面可能被 OneDrive 重定向）。"""
    dirs = []
    try:
        import winreg
        with winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                r"Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders") as k:
            val, _ = winreg.QueryValueEx(k, "Desktop")
        if val:
            p = os.path.expandvars(val)
            if os.path.isdir(p):
                dirs.append(p)
    except Exception:
        pass
    home_desk = os.path.join(os.path.expanduser("~"), "Desktop")
    if home_desk not in dirs and os.path.isdir(home_desk):
        dirs.append(home_desk)
    common = os.path.join(os.environ.get("ProgramData", r"C:\ProgramData"),
                          "Microsoft", "Windows", "Start Menu", "Programs")
    return dirs, common


def _du(path):
    """目录总大小（字节），出错按 0。"""
    total = 0
    for dp, _dns, fns in os.walk(path):
        for fn in fns:
            try:
                total += os.path.getsize(os.path.join(dp, fn))
            except OSError:
                pass
    return total


# ---------------- 卸载核心逻辑（GUI 与 CLI 共用） ----------------

def stop_all(on_stage=None):
    """停止托盘/WebUI/引擎进程：先优雅停机释放 GPU，再强杀兜底。"""
    if on_stage:
        on_stage("stop", "run")
    try:
        req = urllib.request.Request(
            f"http://127.0.0.1:{PORT}/api/shutdown", data=b"", method="POST")
        urllib.request.urlopen(req, timeout=3).read()
        time.sleep(2)
    except Exception:
        pass
    for name in APP_EXE_NAMES:
        subprocess.run(["taskkill", "/IM", name, "/T", "/F"],
                      capture_output=True, creationflags=_NO_WINDOW)
    # 端口残留（webui 引擎）兜底
    try:
        out = subprocess.check_output(
            f'netstat -ano | findstr :{PORT}', shell=True, text=True,
            errors="ignore", creationflags=_NO_WINDOW)
        pids = {ln.split()[-1] for ln in out.splitlines() if ln.strip()}
        for pid in pids:
            if pid.isdigit() and pid != "0":
                subprocess.run(["taskkill", "/PID", pid, "/T", "/F"],
                               capture_output=True, creationflags=_NO_WINDOW)
    except Exception:
        pass
    time.sleep(1)
    if on_stage:
        on_stage("stop", "ok")


def remove_shortcuts(on_stage=None):
    if on_stage:
        on_stage("shortcut", "run")
    desks, _common = _desktop_dirs()
    for d in desks:
        p = os.path.join(d, "MinerU 文档解析.lnk")
        try:
            if os.path.isfile(p):
                os.remove(p)
        except OSError:
            pass
    if on_stage:
        on_stage("shortcut", "ok")


def remove_files(root, keep_venv, keep_model, on_stage=None):
    """删除安装内容；返回未能立即删除的路径列表（已安排延迟清理）。

    采用"已知清单 + sysdiag 通配"而非全目录枚举——若用户把安装根选在
    C:\ 等位置，全量删除会波及无关文件，显式清单可兜住风险。"""
    if on_stage:
        on_stage("files", "run")
    targets = [
        os.path.join(root, "MinerU文档解析"),
        os.path.join(root, "使用说明.html"),
        os.path.join(root, "mineru.json"),
        os.path.join(root, ".install_manifest.json"),
        os.path.join(root, ".install_state.json"),
        os.path.join(root, "install_result.json"),
        os.path.join(root, "runtime", "_data"),
    ]
    try:
        for name in os.listdir(root):
            if name.startswith("sysdiag_") and name.endswith(".log"):
                targets.append(os.path.join(root, name))
    except OSError:
        pass
    if not keep_venv:
        targets.append(os.path.join(root, "runtime", "venv"))
    if not keep_model:
        targets.append(os.path.join(root, "runtime", "models_cache"))

    leftovers = []
    for p in targets:
        if not os.path.exists(p):
            continue
        last_err = None
        for _attempt in range(3):
            try:
                if os.path.isdir(p):
                    import shutil
                    shutil.rmtree(p)
                else:
                    os.remove(p)
                last_err = None
                break
            except OSError as e:
                last_err = e
                time.sleep(0.6)
        if last_err is not None:
            leftovers.append(p)

    # runtime/：空则顺手删除
    try:
        os.rmdir(os.path.join(root, "runtime"))
    except OSError:
        pass
    if on_stage:
        on_stage("files", "ok" if not leftovers else "fail")
    return leftovers


def _ps_encode(script):
    """PowerShell -EncodedCommand：Base64(UTF-16LE)，规避中文路径引号/编码问题。"""
    import base64
    return base64.b64encode(script.encode("utf-16le")).decode("ascii")


def _ps_sq(path):
    """PowerShell 单引号字符串字面量（内部单引号加倍转义）。"""
    return "'" + path.replace("'", "''") + "'"


def schedule_self_cleanup(root):
    """延迟清理：删除自身 exe → 尝试删除根目录（仅当已空——保留模型/venv 时不误删）。

    用 powershell 而非 cmd：部分受限环境（自动化测试沙箱等）会拦截/改写
    cmd /c 调用导致自删除静默失败；PowerShell 全平台可用且未被拦截。
    脚本经 -EncodedCommand 传递，中文路径零转义问题。"""
    exe = sys.executable if getattr(sys, "frozen", False) else None
    lines = ["Start-Sleep 4"]
    if exe:
        q = _ps_sq(exe)
        lines += [
            f"Remove-Item -LiteralPath {q} -Force -ErrorAction SilentlyContinue",
            "Start-Sleep 2",
            f"if (Test-Path -LiteralPath {q}) {{ "
            f"Remove-Item -LiteralPath {q} -Force -ErrorAction SilentlyContinue }}",
        ]
    rq = _ps_sq(root)
    lines += [
        f"if (Test-Path -LiteralPath {rq}) {{",
        f"  if (@(Get-ChildItem -LiteralPath {rq} -Force).Count -eq 0) {{",
        f"    Remove-Item -LiteralPath {rq} -Force -ErrorAction SilentlyContinue }}",
        "}",
    ]
    script = "\n".join(lines)
    subprocess.Popen(
        ["powershell", "-NoProfile", "-WindowStyle", "Hidden",
         "-EncodedCommand", _ps_encode(script)],
        creationflags=_NO_WINDOW, close_fds=True)


def run_uninstall(root, keep_venv, keep_model, on_stage=None):
    """完整卸载流程；返回 leftovers。on_stage(stage, state) 供 UI/CLI 回调。"""
    stop_all(on_stage)
    remove_shortcuts(on_stage)
    leftovers = remove_files(root, keep_venv, keep_model, on_stage)
    schedule_self_cleanup(root)
    return leftovers


def _fmt_size(n):
    if n >= 1024 ** 3:
        return f"{n / 1024 ** 3:.2f} GB"
    if n >= 1024 ** 2:
        return f"{n / 1024 ** 2:.0f} MB"
    return f"{n / 1024:.0f} KB"


class _GradientButton(tk.Canvas):
    """渐变圆角按钮（与安装器主按钮同风格）。"""

    def __init__(self, master, text, top, bottom, fg="#ffffff",
                 width=132, height=38, command=None):
        super().__init__(master, width=width, height=height, bg=master["bg"],
                         highlightthickness=0, cursor="hand2")
        self._text, self._top, self._bottom, self._fg = text, top, bottom, fg
        self._command = command
        self._w, self._h = width, height
        self._hover = False
        self._draw()
        self.bind("<Enter>", lambda e: (self._set_hover(True), self._draw()))
        self.bind("<Leave>", lambda e: (self._set_hover(False), self._draw()))
        self.bind("<Button-1>", self._on_click)

    def _set_hover(self, on):
        self._hover = on

    def _on_click(self, _e):
        if self._command:
            self._command()

    def _draw(self):
        self.delete("all")
        w, h = self._w, self._h
        top = self._mix(self._top, "#ffffff", 0.12 if self._hover else 0.0)
        bot = self._mix(self._bottom, "#000000", 0.10 if self._hover else 0.0)
        for i in range(h - 2):
            t = i / max(h - 3, 1)
            r, g, b = self._lerp(self._hex(top), self._hex(bot), t)
            self.create_rectangle(3, 2 + i, w - 3, 3 + i,
                                  fill=f"#{r:02x}{g:02x}{b:02x}", outline="")
        # 圆角遮罩：用父容器背景色盖住四角
        corner = 9
        bg = self["bg"]
        self.create_polygon(3, 2, 3 + corner, 2, 3, 2 + corner, fill=bg, outline=bg)
        self.create_polygon(w - 3 - corner, 2, w - 3, 2, w - 3, 2 + corner, fill=bg, outline=bg)
        self.create_polygon(3, h - 2, 3 + corner, h - 2, 3, h - 2 - corner, fill=bg, outline=bg)
        self.create_polygon(w - 3 - corner, h - 2, w - 3, h - 2, w - 3, h - 2 - corner, fill=bg, outline=bg)
        self.create_text(w // 2 + 1, h // 2 + 1, text=self._text,
                         fill="#00000044", font=(self._font(), 10, "bold"))
        self.create_text(w // 2, h // 2, text=self._text,
                         fill=self._fg, font=(self._font(), 10, "bold"))

    @staticmethod
    def _font():
        try:
            return tkfont.nametofont("TkDefaultFont").actual("family")
        except Exception:
            return "Microsoft YaHei UI"

    @staticmethod
    def _hex(c):
        return tuple(int(c[i:i + 2], 16) for i in (1, 3, 5))

    @staticmethod
    def _lerp(c1, c2, t):
        return tuple(round(a + (b - a) * t) for a, b in zip(c1, c2))

    @staticmethod
    def _mix(c1, c2, t):
        return "#%02x%02x%02x" % tuple(
            round(a + (b - a) * t) for a, b in zip(_GradientButton._hex(c1),
                                                  _GradientButton._hex(c2)))


class UninstallerApp:
    STAGES = ["停止服务与进程", "删除桌面快捷方式", "删除文件", "完成"]

    def __init__(self, root, root_dir):
        self.root = root
        self._root_dir = root_dir
        self.ui_queue = queue.Queue()
        self.root.title("卸载 MinerU 文档解析")
        self.root.configure(bg=BG)
        self.root.resizable(False, False)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self.root.bind("<Escape>", lambda e: self._on_close())

        # ---------- 卡片 ----------
        card = tk.Frame(self.root, bg=CARD, padx=34, pady=26)
        card.pack(padx=24, pady=24, fill="both", expand=True)

        tk.Label(card, text="卸载 MinerU 文档解析", bg=CARD, fg=INK,
                 font=("Microsoft YaHei UI", 15, "bold")).pack(anchor="w")
        tk.Label(card, text="以下内容将从本机移除，操作不可恢复",
                 bg=CARD, fg=MUTED,
                 font=("Microsoft YaHei UI", 9)).pack(anchor="w", pady=(2, 14))

        # 安装路径
        path_box = tk.Frame(card, bg="#fbfcfd", padx=12, pady=9)
        path_box.pack(fill="x")
        path_box.config(highlightthickness=1, highlightbackground=LINE)
        tk.Label(path_box, text="安装位置", bg="#fbfcfd", fg=FAINT,
                 font=("Microsoft YaHei UI", 8)).pack(anchor="w")
        self.path_var = tk.StringVar(value=self.root_dir)
        tk.Label(path_box, textvariable=self.path_var, bg="#fbfcfd", fg=INK,
                 font=("Microsoft YaHei UI", 9)).pack(anchor="w")

        # 删除项（含体积，后台统计）
        self._sizes = {}
        items = [
            ("app",   "主程序与说明文件", True, "始终删除"),
            ("venv", "Python 虚拟环境", True, ""),
            ("model", "解析模型缓存（重装可跳过下载）", True, ""),
        ]
        self._checks = {}
        for key, label, default, suffix in items:
            var = tk.BooleanVar(value=default)
            row = tk.Frame(card, bg=CARD)
            row.pack(fill="x", pady=3)
            cb = tk.Checkbutton(row, variable=var, bg=CARD, fg=INK, activebackground=CARD,
                                font=("Microsoft YaHei UI", 9), anchor="w")
            cb.pack(side="left")
            txt = tk.Label(row, text=label + (f"（{suffix}）" if suffix else ""),
                           bg=CARD, fg=INK, font=("Microsoft YaHei UI", 9))
            txt.pack(side="left")
            size_lbl = tk.Label(row, text="统计中 ...", bg=CARD, fg=FAINT,
                                font=("Microsoft YaHei UI", 8))
            size_lbl.pack(side="right")
            self._checks[key] = (var, size_lbl)

        tk.Frame(card, bg=LINE, height=1).pack(fill="x", pady=(14, 4))

        # 进度区（卸载时显示）
        self.stage_labels = []
        self.progress_box = tk.Frame(card, bg=CARD)
        for st in self.STAGES:
            row = tk.Frame(self.progress_box, bg=CARD)
            row.pack(anchor="w", pady=3)
            mark = tk.Label(row, text="•", bg=CARD, fg=FAINT,
                            font=("Microsoft YaHei UI", 10, "bold"), width=2)
            mark.pack(side="left")
            lbl = tk.Label(row, text=st, bg=CARD, fg=FAINT,
                           font=("Microsoft YaHei UI", 9))
            lbl.pack(side="left")
            self.stage_labels.append((mark, lbl))
        self.note_lbl = tk.Label(self.progress_box, text="", bg=CARD, fg=MUTED,
                                 font=("Microsoft YaHei UI", 8), wraplength=360, justify="left")

        # 按钮
        btns = tk.Frame(card, bg=CARD)
        btns.pack(fill="x", pady=(10, 0))
        self.cancel_btn = _GradientButton(btns, "取消", "#f5f7f9", "#e9edf0", fg=MUTED,
                                           width=104, height=36, command=self._on_close)
        self.cancel_btn.configure(bg=CARD)
        self.cancel_btn.pack(side="right", padx=(10, 0))
        self.go_btn = _GradientButton(btns, "开始卸载", DANGER, DANGER_D,
                                      width=132, height=36, command=self._start)
        self.go_btn.pack(side="right")

        self._center(520, 430)
        self._working = False
        self.root.after(80, self._poll)
        threading.Thread(target=self._stat_sizes, daemon=True).start()

    # ---------- UI 辅助 ----------
    @property
    def root_dir(self):
        return self._root_dir

    def _center(self, w, h):
        self.root.update_idletasks()
        sw = self.root.winfo_screenwidth()
        sh = self.root.winfo_screenheight()
        self.root.geometry(f"{w}x{h}+{(sw - w) // 2}+{(sh - h) // 2}")

    def _post(self, fn):
        self.ui_queue.put(fn)

    def _poll(self):
        try:
            while True:
                fn = self.ui_queue.get_nowait()
                fn()
        except queue.Empty:
            pass
        self.root.after(80, self._poll)

    def _set_stage(self, idx, state):
        """state: run / ok / fail"""
        mark, lbl = self.stage_labels[idx]
        colors = {"run": (ACCENT_2, INK), "ok": (OK_GREEN, MUTED), "fail": (DANGER, DANGER)}
        m_fg, l_fg = colors[state]
        marks = {"run": "›", "ok": "✓", "fail": "✕"}
        mark.config(fg=m_fg, text=marks[state])
        lbl.config(fg=l_fg)
        if state == "run":
            lbl.config(fg=INK)

    def _on_close(self):
        if not self._working:
            self.root.destroy()

    # ---------- 体积统计 ----------
    def _stat_sizes(self):
        def du_of(*rel):
            p = os.path.join(self.root_dir, *rel)
            return _du(p) if os.path.isdir(p) else 0

        sizes = {
            "venv": du_of("runtime", "venv"),
            "model": du_of("runtime", "models_cache"),
        }
        app = 0
        for dp, _dns, fns in os.walk(os.path.join(self.root_dir, "MinerU文档解析")):
            for fn in fns:
                try:
                    app += os.path.getsize(os.path.join(dp, fn))
                except OSError:
                    pass
        sizes["app"] = app
        self._post(lambda: self._apply_sizes(sizes))

    def _apply_sizes(self, sizes):
        self._sizes = sizes
        for key in ("app", "venv", "model"):
            var, lbl = self._checks[key]
            lbl.config(text=_fmt_size(sizes.get(key, 0)))

    # ---------- 卸载执行 ----------
    def _start(self):
        self._working = True
        keep_venv = not self._checks["venv"][0].get()
        keep_model = not self._checks["model"][0].get()
        self.go_btn.pack_forget()
        self.cancel_btn.configure(state="disabled")
        self.note_lbl.pack(anchor="w", pady=(8, 0))
        self.progress_box.pack(anchor="w", fill="x", pady=(8, 0))
        threading.Thread(target=self._run_uninstall,
                         args=(keep_venv, keep_model), daemon=True).start()

    _STAGE_INDEX = {"stop": 0, "shortcut": 1, "files": 2}

    def _run_uninstall(self, keep_venv, keep_model):
        root = self.root_dir

        def on_stage(stage, state):
            idx = self._STAGE_INDEX.get(stage)
            if idx is not None:
                self._post(lambda i=idx, s=state: self._set_stage(i, s))

        try:
            leftovers = run_uninstall(root, keep_venv, keep_model, on_stage)
            self._post(lambda: self._finish(leftovers))
        except Exception as e:
            try:
                self._post(lambda e=e: self.note_lbl.config(
                    text=f"卸载中断：{e}", fg=DANGER))
            except Exception:
                pass

    def _finish(self, leftovers):
        self._set_stage(3, "ok")
        if leftovers:
            self.note_lbl.config(
                text="部分文件被占用未能立即删除，已安排在程序退出后自动清理。", fg=MUTED)
        else:
            self.note_lbl.config(text="卸载完成，窗口即将自动关闭。", fg=MUTED)
        self._working = False
        self.root.after(2200, self.root.destroy)


def main():
    _enable_dpi_awareness()
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default=None)
    ap.add_argument("--cli", action="store_true",
                    help="命令行模式：无界面直接卸载（自动化/高级用户）")
    ap.add_argument("--keep-venv", action="store_true", help="保留虚拟环境")
    ap.add_argument("--keep-model", action="store_true", help="保留模型缓存")
    args, _unknown = ap.parse_known_args()

    if getattr(sys, "frozen", False):
        root_dir = os.path.dirname(sys.executable)
    else:
        root_dir = os.path.abspath(args.root or os.getcwd())

    if args.cli:
        def _log(stage, state):
            marks = {"run": "…", "ok": "✓", "fail": "✕"}
            print(f"[{stage}] {marks.get(state, state)}", flush=True)
        leftovers = run_uninstall(root_dir, args.keep_venv, args.keep_model,
                                   on_stage=_log)
        if leftovers:
            print(f"部分文件被占用，已安排退出后自动清理：{len(leftovers)} 项", flush=True)
        print("卸载完成", flush=True)
        return

    root = tk.Tk()
    UninstallerApp(root, root_dir)
    root.mainloop()


if __name__ == "__main__":
    main()
