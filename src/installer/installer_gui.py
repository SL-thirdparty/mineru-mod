# -*- coding: utf-8 -*-
"""MinerU 一键安装器（图形界面）。

流程：
  1. 用户选择安装目录（默认 C:\\MinerU_App，支持「浏览」选目录 + 目录可写/空间提示）
  2. 校验 Python 3.11（缺则从国内镜像自动下载安装）
  3. 后台复制文件、装依赖、下载模型、建快捷方式
  4. 成功 → 展示终态操作（打开目录 / 完成），并打开说明文档

打包: runtime\\venv\\Scripts\\python.exe -m PyInstaller --clean --noconfirm \
      --distpath release --workpath .tmp\\pyinstaller\\installer-build \
      scripts\\installer_spec.pyinstaller.spec
"""
import ctypes
import json
import math
import os
import queue
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import tkinter.font as tkfont
import urllib.request
import webbrowser
from pathlib import Path

import tkinter as tk
from tkinter import filedialog, messagebox

from comp_panel import CompPanel

try:
    import psutil  # noqa: E402  # 枚举运行中进程（锁定主程序文件）
except Exception:
    psutil = None

_HERE = os.path.dirname(os.path.abspath(__file__))

# 子进程不弹控制台窗口（打包后 exe 是窗口程序，否则 python/pip 会闪黑框）
_NO_WINDOW = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0


def _resource_dir():
    """打包后资源解压到 _MEIPASS；源码运行则用脚本所在目录。"""
    if getattr(sys, "frozen", False):
        return getattr(sys, "_MEIPASS", _HERE)
    return _HERE


def _src_root():
    """复制源根（含 src/webui、release/mineru_tray）：
    exe 用 _MEIPASS；源码运行用项目根（src/installer 向上两层）。"""
    if getattr(sys, "frozen", False):
        return getattr(sys, "_MEIPASS", _HERE)
    return os.path.dirname(os.path.dirname(_HERE))


# 检测时判定「需要修复」的核心组件：主程序 / 运行环境 / 模型 异常才算需要修复。
# cuda（GPU 加速，CPU 机正常报 wait）、shortcut（桌面快捷方式，可选）、uv（加速引擎，
# 缺失回退 pip）为可选/增强项，仅展示状态，不诱导用户执行修复。
CORE_REPAIR = {"app", "venv", "models"}


def _needs_repair(cid, status):
    """组件是否触发「开始修复」：仅核心组件出现 wait/fail 才算。"""
    return cid in CORE_REPAIR and status in ("wait", "fail")


APP_VERSION = "1.0.0"   # 与 install_flow.py 保持一致（安装/更新后清单版本）


_PY_URLS = [
    "https://mirrors.huaweicloud.com/python/3.11.9/python-3.11.9-amd64.exe",
    "https://www.python.org/ftp/python/3.11.9/python-3.11.9-amd64.exe",
]


# ---------------- 现代化基础 ----------------
def _enable_dpi_awareness():
    """高分屏下控件与字体清晰渲染。"""
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass


def _dark_titlebar(win):
    """Win10/11：标题栏暗色化，与深色渐变头部一体。"""
    try:
        hwnd = win.winfo_id()
        # 顶层句柄需要 GetParent 链；tk 直接给 toplevel id 即可
        val = ctypes.c_int(1)
        ctypes.windll.dwmapi.DwmSetWindowAttribute(
            hwnd, 20, ctypes.byref(val), ctypes.sizeof(val))  # DWMWA_USE_IMMERSIVE_DARK_MODE
    except Exception:
        pass


def _hex_rgb(c):
    c = c.lstrip("#")
    return tuple(int(c[i:i + 2], 16) for i in (0, 2, 4))


def _mix(c1, c2, t):
    r1, g1, b1 = _hex_rgb(c1)
    r2, g2, b2 = _hex_rgb(c2)
    return "#%02x%02x%02x" % (int(r1 + (r2 - r1) * t),
                              int(g1 + (g2 - g1) * t),
                              int(b1 + (b2 - b1) * t))


def _rr(cv, x1, y1, x2, y2, r, **kw):
    """圆角矩形（polygon 平滑）。"""
    pts = [x1 + r, y1, x2 - r, y1, x2, y1, x2, y1 + r, x2, y2 - r, x2, y2,
           x2 - r, y2, x1 + r, y2, x1, y2, x1, y2 - r, x1, y1 + r, x1, y1]
    return cv.create_polygon(pts, smooth=True, **kw)


def _vgrad(cv, x1, y1, x2, y2, c1, c2, r=8, **kw):
    """垂直渐变圆角矩形（逐行模拟）。"""
    h = max(int(y2 - y1), 2)
    n = h
    step = max(h / n, 1)
    y = y1
    i = 0
    while y < y2:
        t = i / max(n - 1, 1)
        cv.create_rectangle(x1 + r * 0.3, y, x2 - r * 0.3, min(y + step + 1, y2),
                            fill=_mix(c1, c2, t), outline="", width=0)
        y += step
        i += 1
    # 两端渐变色盖边角
    _rr(cv, x1, y1, x2, y1 + r, r, fill=_mix(c1, c2, 0.0), outline="")
    _rr(cv, x1, y2 - r, x2, y2, r, fill=_mix(c1, c2, 1.0), outline="")


def _hgrad_rect(cv, x1, y1, x2, y2, c1, c2):
    """水平渐变条（用于进度填充 / hero）。"""
    w = max(int(x2 - x1), 2)
    n = min(w, 160)
    for i in range(n):
        t = i / max(n - 1, 1)
        xa = x1 + (x2 - x1) * i / n
        xb = x1 + (x2 - x1) * (i + 1) / n + 1
        cv.create_rectangle(xa, y1, xb, y2, fill=_mix(c1, c2, t), outline="", width=0)


# ---- 青墨渐变配色 ----
BG       = "#f4f6f8"   # 画布
CARD     = "#ffffff"
SHADOW1  = "#e8ecf0"   # 阴影外层
SHADOW2  = "#f0f3f5"   # 阴影内层
INK      = "#182430"
MUTED    = "#6d7885"
FAINT    = "#9aa5b1"
ACCENT   = "#0e7490"   # 主色深青
ACCENT_2 = "#14b8a6"   # 渐变亮端（青绿）
ACCENT_D = "#0b5c73"
ACCENT_L = "#e2f2f6"   # 主色浅底
SUCCESS  = "#15803d"
SUCCESS_L= "#e4f4ea"
DANGER   = "#c0362c"
DANGER_L = "#fbe9e7"
WARN     = "#b45309"
TRACK    = "#e7ebee"
FIELD_BG = "#fbfcfd"

FONT      = "Microsoft YaHei UI"
FONT_MONO = "Consolas"
ICON_FONT = "Segoe MDL2 Assets"

# MDL2 图标码点
I_FOLDER  = "\uE8B7"
I_SETTING = "\uE713"
I_DOWN    = "\uE896"
I_CHECK   = "\uE73E"
I_PLAY    = "\uE768"
I_CLOSE   = "\uE711"
I_DISK    = "\uEDA2"
I_ERROR   = "\uE783"
I_REFRESH = "\uE72C"   # 刷新（检查更新）
I_TRASH   = "\uE74D"   # 删除（卸载）

STAGES = [
    ("准备", "复制文件与创建环境", I_FOLDER),
    ("组件", "安装解析依赖", I_SETTING),
    ("模型", "下载解析模型", I_DOWN),
    ("完成", "创建桌面快捷方式", I_CHECK),
]
# 各阶段进度条起始水位（0-100）；安装过程中进度只前进不回退
_STAGE_FLOOR = (3, 10, 50, 75)
_TAG_STAGE = {
    "copy": 0, "venv": 0,
    "cfg": 1, "gpu": 1, "deps": 1,
    "model": 2,
    "shortcut": 3,
}


def _prefs_path():
    """安装器偏好持久化文件（安装路径 + 下载线程数）。"""
    return os.path.join(os.environ.get("LOCALAPPDATA",
                                       os.path.expanduser("~")),
                        "MinerU", "installer.json")


_TAG_HINT = {
    "copy": "正在复制运行文件到安装目录",
    "venv": "正在创建虚拟环境",
    "deps": "正在安装解析依赖（首次较慢，请耐心等待）",
    "gpu": "正在探测显卡与 CUDA 推理加速",
    "cfg": "正在生成引擎配置",
    "model": "正在下载解析模型（约 2.4GB，依网速而定）",
    "shortcut": "正在生成桌面快捷方式",
}


def _fmt_dur(sec):
    """秒 → 'mm:ss' / 'h:mm:ss'。"""
    sec = max(int(sec), 0)
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


class GradButton(tk.Canvas):
    """渐变圆角按钮：primary 垂直渐变 / secondary 白底描边 / ghost 透明。"""

    _STYLE = {
        "primary":   dict(grad=(ACCENT_2, ACCENT), fg="#ffffff", border=None),
        "secondary": dict(grad=None, bg=CARD, fg=INK, border="#dfe4e9"),
        "ghost":     dict(grad=None, bg=None, fg=MUTED, border=None),
    }

    def __init__(self, master, text, command=None, kind="primary",
                 height=38, padx=28, fontsize=10, icon=None):
        self._kind = kind
        self._command = command
        self._enabled = True
        self._padx = padx
        self._height = height
        self._icon = icon
        self._font = (FONT, fontsize, "bold")
        self._ifont = (ICON_FONT, fontsize + 2)
        self._canvas_bg = master.cget("bg") if isinstance(master, (tk.Frame, tk.Canvas)) else BG
        super().__init__(master, width=10, height=height, bg=self._canvas_bg,
                         highlightthickness=0, cursor="hand2")
        self._measure = tkfont.Font(font=self._font)
        self._imeasure = tkfont.Font(font=self._ifont)
        self.bind("<Button-1>", self._on_click)
        self.bind("<Enter>", lambda e: self._hover(True))
        self.bind("<Leave>", lambda e: self._hover(False))
        self._hovering = False
        self.set_text(text)

    def set_text(self, text):
        w = self._measure.measure(text) + 2 * self._padx
        if self._icon:
            w += self._imeasure.measure(self._icon) + 8
        self.configure(width=int(w))
        self._text = text
        self._draw()

    def set_enabled(self, on):
        self._enabled = on
        self.configure(cursor="hand2" if on else "arrow")
        self._draw()

    def _on_click(self, _e):
        if self._enabled and self._command:
            self.after(10, self._command)

    def _hover(self, on):
        self._hovering = on
        self._draw()

    def _draw(self):
        cv = self
        cv.delete("all")
        st = self._STYLE[self._kind]
        w, h = int(cv.cget("width")), self._height
        disabled = not self._enabled
        if self._kind == "primary":
            top, bot = st["grad"]
            if disabled:
                cv.create_rectangle(2, 2, w - 2, h - 2, fill="#c6ccd3", outline="")
            elif self._hovering:
                _vgrad(cv, 1, 1, w - 1, h - 1, _mix(top, "#ffffff", 0.18),
                       _mix(bot, "#ffffff", 0.12), r=9)
            else:
                _vgrad(cv, 1, 1, w - 1, h - 1, top, bot, r=9)
            fg = "#ffffff"
        elif self._kind == "secondary":
            fill = "#f5f7f9" if self._hovering else (st["bg"] if not disabled else "#eceff2")
            _rr(cv, 1, 1, w - 1, h - 1, 9, fill=fill, outline=st["border"] or "", width=1)
            fg = MUTED if disabled else st["fg"]
        else:  # ghost
            if self._hovering and self._enabled:
                _rr(cv, 1, 1, w - 1, h - 1, 9, fill="#e9edf0", outline="")
            fg = FAINT if disabled else st["fg"]
        tx = w // 2
        if self._icon:
            iw = self._imeasure.measure(self._icon)
            total = self._measure.measure(self._text) + iw + 8
            ix = int((w - total) / 2)
            cv.create_text(ix + iw / 2, h // 2, text=self._icon, font=self._ifont, fill=fg)
            cv.create_text(ix + iw + 4 + self._measure.measure(self._text) / 2, h // 2,
                           text=self._text, font=self._font, fill=fg)
        else:
            cv.create_text(tx, h // 2, text=self._text, font=self._font, fill=fg)


class Field(tk.Frame):
    """圆角输入框：Canvas 画圆角底与聚焦描边，内嵌无边框 Entry。"""

    def __init__(self, master, textvariable, height=42):
        super().__init__(master, bg=CARD)
        self._h = height
        self._focus = False
        self.cv = tk.Canvas(self, height=height, width=0, bg=CARD, highlightthickness=0)
        self.cv.pack(fill="both", expand=True)
        self.entry = tk.Entry(self.cv, textvariable=textvariable, font=(FONT_MONO, 10),
                              relief="flat", bg=FIELD_BG, fg=INK,
                              insertbackground=INK, borderwidth=0,
                              highlightthickness=0)
        self.entry.place(relx=0.5, rely=0.5, anchor="center", relwidth=0.965)
        self.entry.bind("<FocusIn>", lambda e: self._set_focus(True))
        self.entry.bind("<FocusOut>", lambda e: self._set_focus(False))
        self.cv.bind("<Configure>", lambda e: self._draw())
        self._draw()

    def _set_focus(self, on):
        self._focus = on
        self._draw()

    def _draw(self):
        cv = self.cv
        cv.delete("all")
        w = max(cv.winfo_width(), 40)
        h = self._h
        _rr(cv, 1, 1, w - 1, h - 1, 10,
            fill=FIELD_BG, outline=ACCENT if self._focus else "#dde3e8", width=2)

    def focus_set(self):
        self.entry.focus_set()


class TickBox(tk.Canvas):
    """自定义圆角复选框（渐变主色勾选）。"""

    def __init__(self, master, text, variable, size=18):
        self._var = variable
        self._size = size
        super().__init__(master, width=size, height=size, bg=master.cget("bg"),
                         highlightthickness=0, cursor="hand2")
        self._label = tk.Label(master, text=text, font=(FONT, 10),
                               bg=master.cget("bg"), fg=INK, cursor="hand2")
        self._label.bind("<Button-1>", lambda e: self._toggle())
        self.bind("<Button-1>", lambda e: self._toggle())
        self._hover = False
        self.bind("<Enter>", lambda e: (setattr(self, "_hover", True), self._draw()))
        self.bind("<Leave>", lambda e: (setattr(self, "_hover", False), self._draw()))
        self._draw()

    def grid(self, **kw):
        row = tk.Frame(self.master, bg=self.master.cget("bg"))
        row.grid(**kw)
        super().pack(side="left", in_=row)
        self._label.pack(side="left", padx=(9, 0), in_=row)

    def _toggle(self):
        self._var.set(not self._var.get())
        self._draw()

    def _draw(self):
        self.delete("all")
        s = self._size
        if self._var.get():
            _vgrad(self, 0, 0, s, s, ACCENT_2, ACCENT, r=5)
            self.create_line(s * 0.27, s * 0.53, s * 0.44, s * 0.70,
                             s * 0.74, s * 0.30, fill="#ffffff", width=2.2,
                             capstyle="round", joinstyle="round")
        else:
            _rr(self, 1, 1, s - 1, s - 1, 5,
                fill="#ffffff" if not self._hover else "#f3f7f9",
                outline="#c9d2da" if not self._hover else ACCENT, width=1.5)


class Pill(tk.Canvas):
    """圆角胶囊状态徽章：浅底深字，前置图形随 kind 变化（✓/✗/⟳/!/圆点）。"""

    _KIND = {
        "idle":    (TRACK, MUTED),
        "accent":  (ACCENT_L, ACCENT_D),
        "success": (SUCCESS_L, SUCCESS),
        "error":   (DANGER_L, DANGER),
        "busy":    (ACCENT_L, ACCENT_D),
        "warn":    ("#fef3c7", WARN),
    }

    def __init__(self, master, text="", kind="idle", height=24, padx=12):
        self._h = height
        self._padx = padx
        self._kind = kind
        self._phase = 0.0           # 旋转图形相位（accent/busy 动态旋转）
        self._font = (FONT, 9, "bold")
        super().__init__(master, height=height, bg=master.cget("bg"),
                         highlightthickness=0)
        self._measure = tkfont.Font(font=self._font)
        self.set(text, kind)

    def set(self, text, kind):
        self._text = text
        self._kind = kind
        w = self._measure.measure(text) + 2 * self._padx + 20  # 前置图形占位
        self.configure(width=int(w))
        self._draw()

    def pulse(self):
        """accent/busy 旋转图形动画步进（主窗口动画循环调用）。"""
        if self._kind in ("accent", "busy"):
            self._phase += 0.35
            self._draw()

    def _draw(self):
        self.delete("all")
        bg, fg = self._KIND.get(self._kind, self._KIND["idle"])
        w, h = int(self.cget("width")), self._h
        _rr(self, 1, 1, w - 1, h - 1, (h - 2) / 2, fill=bg, outline="")
        cx, cy = self._padx, h / 2
        if self._kind == "success":
            # 对勾 ✓（结果正常）
            self.create_line(cx - 3.5, cy, cx - 1, cy + 3.5, cx + 5, cy - 3.5,
                             fill=fg, width=2, capstyle="round",
                             joinstyle="round")
        elif self._kind == "error":
            # 叉 ✗（结果失败/异常）
            self.create_line(cx - 4, cy - 4, cx + 4, cy + 4, fill=fg, width=2,
                             capstyle="round")
            self.create_line(cx - 4, cy + 4, cx + 4, cy - 4, fill=fg, width=2,
                             capstyle="round")
        elif self._kind == "warn":
            # 感叹号（需要用户注意）
            self.create_line(cx, cy - 4, cx, cy + 1, fill=fg, width=2.2,
                             capstyle="round")
            self.create_oval(cx - 1.6, cy + 3.4, cx + 1.6, cy + 6.6,
                             fill=fg, outline="")
        elif self._kind in ("accent", "busy"):
            # 旋转刷新箭头 ⟳（进行中，动态旋转；Tk 角度 0°=右、90°=上）
            r = 5.0
            start = self._phase + 120          # 缺口朝上（60°~120°）
            end = start + 300
            self.create_arc(cx - r, cy - r, cx + r, cy + r, start=start,
                            extent=300, style="arc", outline=fg, width=2)
            th = math.radians(end)
            tip = (cx + r * math.cos(th), cy - r * math.sin(th))
            vx, vy = -math.sin(th), -math.cos(th)   # 弧线前进方向（逆时针）
            L, wd = 4.5, 3.4
            bx, by = tip[0] - vx * L, tip[1] - vy * L
            px, py = math.cos(th), -math.sin(th)    # 垂直方向
            self.create_polygon(tip[0], tip[1],
                                bx + px * wd / 2, by + py * wd / 2,
                                bx - px * wd / 2, by - py * wd / 2,
                                fill=fg, outline="")
        else:
            # idle：静态圆点
            self.create_oval(cx - 3, cy - 3, cx + 3, cy + 3, fill=fg, outline="")
        self.create_text(self._padx + 12, h / 2, text=self._text,
                         font=self._font, fill=fg, anchor="w")


class Installer(tk.Tk):
    def __init__(self):
        _enable_dpi_awareness()
        # 任务栏图标独立分组（避免被 Python 默认图标覆盖）
        try:
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
                "MinerU.Installer.1.0")
        except Exception:
            pass
        super().__init__()
        self.title("MinerU 文档解析 · 一键安装")
        self.configure(bg=BG)
        self._set_window_icon()
        self.q = queue.Queue()
        self.worker = None
        self._proc = None
        self._pysetup = None
        self._cancel = threading.Event()
        self._paused = False
        self._stage_now = -1
        self._progress = 0
        self._running = False
        self._t0 = None              # 本次安装起始时间（总耗时秒表）
        self._act_base = ""          # 当前活动文案（不含秒表后缀）
        self._pkg_last_ts = 0.0      # 依赖下载最后进展时间戳（停滞提示用）
        self._act_t0 = None          # 当前活动秒表起点（None=不显示耗时）
        self._pkg_total = None       # 本轮 uv 依赖解析总数
        self._pkg_done = 0
        self._pkg_names = set()
        self._spin = 0               # 加载圈相位
        self._mq_x = 0.0             # 进度条流光偏移
        self._pulse = 0.0            # 阶段图标呼吸相位
        self._log_fh = None          # 安装日志文件句柄
        self._log_path = None        # 日志文件路径
        self._repair_mode = False    # 当前是否为「检测修复」流程（决定完成文案）
        self._checking = False       # 当前是否处于「仅检测」阶段（阶段一，未开始修复）
        self._repair_root = None     # 检测到的安装目录（阶段二确认修复时复用）
        self._update_prompted = False  # 已内联提示过更新（避免后台重复打扰）
        self._update_mode = False      # 当前是否处于更新流程（决定按钮文案/状态）
        self._update_ready = False     # 已发现新版本，等待用户点击「立即更新」
        self._waiting_update = False   # 等待 MinerU 退出（更新三选一「否」模式）
        self._update_diff = None       # 最近一次更新检查结果（供执行阶段复用）
        self._update_tray_pid = None   # 运行中托盘 PID（更新应用阶段精确终止）

        self._build_ui()
        self.after(50, lambda: _dark_titlebar(self))
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.bind("<Escape>", lambda e: self._on_close())
        self._fill_presets()
        self._apply_adaptive_geometry()
        self.after(400, self._startup_state)

    def _set_window_icon(self):
        """设置窗口图标（与 exe 图标一致）。"""
        for p in (
            os.path.join(_resource_dir(), "icon.ico"),
            os.path.join(_resource_dir(), "src", "tray", "icon.ico"),
            os.path.join(_HERE, "..", "tray", "icon.ico"),
            os.path.join(_HERE, "icon.ico"),
        ):
            if os.path.isfile(p):
                try:
                    self.iconbitmap(p)
                except Exception:
                    pass
                break

    def _init_log_file(self):
        """创建安装日志文件（exe 同目录，命名 MinerU安装_日期_时间.log）。"""
        if getattr(sys, "frozen", False):
            base = os.path.dirname(sys.executable)
        else:
            base = _HERE
        ts = time.strftime("%Y%m%d_%H%M%S")
        path = os.path.join(base, f"MinerU安装_{ts}.log")
        try:
            self._log_fh = open(path, "w", encoding="utf-8")
            self._log_path = path
        except OSError:
            self._log_fh = None
            self._log_path = None

    def _close_log_file(self):
        if self._log_fh:
            try:
                self._log_fh.close()
            except OSError:
                pass
            self._log_fh = None

    def _startup_state(self):
        """启动后检测已安装状态：已装则常驻「检测修复/检查更新/卸载」按钮，
        并在后台对比远端版本，发现新版本时弹窗提醒（不阻断主界面）。"""
        self._set_buttons("idle")
        self._refit_width()   # 窗口映射后校正宽度，防维护按钮溢出压盖
        root = _detect_installed_root()
        if not root or self._running:
            return
        self._show_installed(root)
        threading.Thread(target=self._check_update_bg, args=(root,),
                         daemon=True).start()

    def _show_installed(self, root):
        """展示已安装状态：胶囊徽章 + 日志提示。"""
        ver = _installed_version(root)
        self.pill.set(f"已安装 v{ver}", "success")
        self._append_log(f"· 检测到已安装 MinerU v{ver}：{root}", "muted")
        self._append_log("· 可点击「检测修复」检查组件完整性，"
                         "或「检查更新」升级到最新版本", "muted")

    def _start_check_update(self):
        """检查更新（主界面内联）：对比本地与远端清单，结果显示在状态徽章与日志。
        发现新版本时主按钮变为「立即更新」，不打开任何新窗口。"""
        if self.worker and self.worker.is_alive():
            return
        root = _detect_installed_root()
        if not root:
            self._set_status("error", "未检测到已安装的 MinerU")
            self._append_log("· 检查更新：未检测到已安装的 MinerU，请先完成安装", "err")
            return
        self._cancel.clear()
        self._paused = False
        self._proc = None
        self._update_mode = True
        self._checking = True
        self._update_ready = False
        self._waiting_update = False
        self._update_diff = None
        self._repair_root = root
        self._reset_progress()
        self._set_buttons("checking")
        self._init_log_file()
        self._flog_placeholder(clear=True)
        self._append_log("· 检查更新：对比本地与远端文件清单（只读，不修改）")
        self._append_log(f"· 安装目录：{root}")
        if self._log_path:
            self._append_log(f"· 日志文件：{self._log_path}", "muted")
        self._spin = 0
        self._mq_x = 0.0
        self._pulse = 0.0
        self._running = True
        self._t0 = time.time()
        self._set_activity("正在检查更新…", stopwatch=True, fg=INK)
        self._set_status("accent", "正在检查更新…")
        self._anim_tick()
        self._sec_tick()
        self.worker = threading.Thread(target=self._run_check_update,
                                       args=(root,), daemon=True)
        self.worker.start()
        self.after(80, self._poll)

    def _run_check_update(self, root):
        """检查更新 worker：updater.check 只读对比，结果经队列回 UI。"""
        try:
            import updater
            self.q.put(("status", "accent", "正在对比远端清单…"))
            d = updater.check(root)
            self.q.put(("update_done", d))
        except Exception as e:  # noqa: BLE001
            self.q.put(("log", "错误：" + str(e)))
            self.q.put(("update_finish", False, None))

    def _on_update_done(self, d):
        """检查更新完成：已最新 → 恢复；有新版本 → 主按钮变「立即更新」。"""
        self._checking = False
        self._stop_flow_anim()
        self._update_diff = d
        self._advance_progress(100)
        if d.get("up_to_date"):
            ver = d.get("local_version", "未知")
            self._set_activity("已是最新版本，无需更新", fg=SUCCESS)
            self._set_status("success", f"已是最新版本 v{ver}")
            self._set_buttons("done")
            self._append_log(f"✓ 已是最新版本 v{ver}"
                             "（文件与远端一致，或本地版本不低于远端）", "ok")
            if d.get("local_newer") and (d.get("added") or d.get("changed")):
                self._append_log("· 本地版本高于远端，不执行降级更新", "muted")
        else:
            rv = d.get("remote_version", "未知")
            lv = d.get("local_version", "未知")
            self._update_ready = True
            self._set_activity(f"发现新版本 v{rv}（当前 v{lv}），"
                               f"可点击「立即更新」", fg=ACCENT_D)
            self._set_status("accent", f"发现新版本 v{rv}")
            self._set_buttons("check_done")
            self._append_log(f">>> 发现新版本 v{rv}（当前 v{lv}）", "stage")
            if d.get("local_created") or d.get("remote_created"):
                self._append_log(f"· 本地构建：{d.get('local_created') or '未知'}",
                                 "muted")
                self._append_log(f"· 远端构建：{d.get('remote_created') or '未知'}",
                                 "muted")
            n_add = len(d.get("added", []))
            n_chg = len(d.get("changed", []))
            self._append_log(f"· 远端共 {d.get('total', 0)} 个文件 · "
                             f"新增 {n_add} · 更新 {n_chg}", "muted")
            for rel in (d.get("added", []) + d.get("changed", []))[:10]:
                self._append_log("  " + rel
                                 + ("（缺失）" if rel in d.get("added", []) else ""),
                                 "muted")
            more = n_add + n_chg - 10
            if more > 0:
                self._append_log(f"· …等共 {more} 个文件未列出", "muted")
            self._append_log("· 点击「立即更新」只下载差异文件，不影响已有环境",
                             "muted")
        self._refit_width()

    def _check_update_bg(self, root):
        """启动后台轻量对比远端清单：存在可更新差异才在主线程内联提示（静默失败）。
        以文件差异为准（updater.check），而非版本号对比——本地清单版本为
        APP_VERSION、远端带构建时间戳时，纯版本对比会让新装永远显示有更新。"""
        try:
            import updater
            d = updater.check(root)
            if not d["up_to_date"]:
                self.after(0, lambda: self._inline_update_hint(d))
        except Exception:
            pass

    def _inline_update_hint(self, d):
        """后台发现新版本：主界面内联提示（状态徽章 + 主按钮「立即更新」），不弹窗。"""
        if self._update_prompted or self._running or (self.worker
                                                     and self.worker.is_alive()):
            return
        self._update_prompted = True
        self._update_mode = True
        self._update_ready = True
        self._update_diff = d
        rv = d.get("remote_version", "未知")
        lv = d.get("local_version", "未知")
        self._set_status("accent", f"发现新版本 v{rv}（当前 v{lv}）")
        self._set_activity(f"发现新版本 v{rv}，可点击「立即更新」升级", fg=ACCENT_D)
        self._set_buttons("check_done")
        self._append_log(f"· 后台检查发现新版本 v{rv}（当前 v{lv}），"
                         f"点击「立即更新」只下载差异文件", "warn")
        self._refit_width()

    def _start_update(self):
        """阶段二·更新：确认运行中程序处理方式后，下载差异文件并应用。"""
        if self.worker and self.worker.is_alive():
            return
        d = getattr(self, "_update_diff", None)
        if not d or d.get("up_to_date"):
            return
        root = d.get("root")
        if not root or not os.path.isdir(root):
            messagebox.showwarning("提示", "安装目录不存在，请重新检查更新。")
            self._set_buttons("idle")
            return
        self._update_ready = False
        if not self._ask_close_app_for_update(root):
            return
        self._cancel.clear()
        self._paused = False
        self._proc = None
        self._reset_progress()
        self._set_buttons("running")
        if self._log_fh is None:
            self._init_log_file()
            if self._log_path:
                self._append_log(f"· 日志文件：{self._log_path}", "muted")
        self._append_log(">>> 开始更新：下载差异文件并应用", "stage")
        self._set_activity("正在下载差异文件…", stopwatch=True, fg=INK)
        self._set_status("accent", "正在更新…")
        self._spin = 0
        self._mq_x = 0.0
        self._pulse = 0.0
        self._running = True
        self._t0 = time.time()
        self._anim_tick()
        self._sec_tick()
        self.worker = threading.Thread(target=self._run_update,
                                       args=(root, d), daemon=True)
        self.worker.start()
        self.after(80, self._poll)

    def _ask_close_app_for_update(self, root):
        """更新前处理运行中的 MinerU：立即关闭 / 等待退出（可取消）/ 取消。
        返回 True 表示可继续下载（无进程或已关闭）。"""
        try:
            import updater
            n = updater._running_tray_procs()
        except Exception:
            n = 0
        self._update_tray_pid = None
        if n == 0:
            return True
        r = messagebox.askyesnocancel(
            "MinerU 正在运行",
            f"检测到 {n} 个 MinerU 进程正在运行。\n\n"
            "更新需要覆盖主程序文件，请先退出 MinerU。\n\n"
            "「是」立即关闭进程并继续更新\n"
            "「否」等待任务完成后再更新（等待期间可点击「取消等待」）\n"
            "「取消」中止本次更新")
        if r is None:
            self._append_log("· 已取消更新（MinerU 仍在运行）", "muted")
            return False
        if r is False:
            self._waiting_update = True
            self._set_buttons("upd_waiting")
            self._set_activity("等待 MinerU 退出后自动继续更新…", fg=WARN)
            self._set_status("accent", "等待 MinerU 退出…")
            self._append_log(f"· 正在等待 {n} 个 MinerU 进程退出"
                             f"（可点击「取消等待」）", "muted")
            self._wait_update_tick()
            return False
        self._update_tray_pid = self._find_tray_pid(root)
        try:
            import updater
            updater._stop_tray(self._update_tray_pid)
            self._append_log("· 已关闭运行中的 MinerU 进程", "warn")
        except Exception as e:  # noqa: BLE001
            self._append_log("· 关闭运行中进程失败：" + str(e), "warn")
        return True

    def _find_tray_pid(self, root):
        """定位安装根下运行的托盘主程序 PID（应用阶段精确终止，避免误伤）。"""
        if psutil is None:
            return None
        root_abs = os.path.normcase(os.path.abspath(root))
        try:
            for p in psutil.process_iter(["pid", "exe"]):
                try:
                    exe = p.info.get("exe")
                except Exception:
                    exe = None
                if exe and os.path.normcase(
                        os.path.abspath(exe)).startswith(root_abs):
                    return p.info["pid"]
        except Exception:
            pass
        return None

    def _wait_update_tick(self):
        """等待模式轮询：MinerU 退出后自动进入更新。"""
        if not getattr(self, "_waiting_update", False):
            return
        try:
            import updater
            n = updater._running_tray_procs()
        except Exception:
            n = 0
        if n == 0:
            self._waiting_update = False
            self._append_log("· MinerU 已退出，自动开始更新", "ok")
            self._start_update()
            return
        self.after(1000, self._wait_update_tick)

    def _cancel_wait_update(self):
        """取消等待模式，回到「立即更新」待确认状态。"""
        if not getattr(self, "_waiting_update", False):
            return
        self._waiting_update = False
        self._update_ready = True
        d = getattr(self, "_update_diff", None)
        rv = d.get("remote_version", "未知") if d else "未知"
        self._set_buttons("check_done")
        self._set_status("accent", f"发现新版本 v{rv}")
        self._set_activity(f"发现新版本 v{rv}，可点击「立即更新」", fg=ACCENT_D)
        self._append_log("· 已取消等待更新，可稍后点击「立即更新」重试", "muted")

    def _run_update(self, root, diff):
        """更新 worker：下载差异 → 应用（停托盘/替换文件/改写清单）→ 重启托盘。"""
        try:
            import updater
            remote = diff["manifest"]
            rels = diff.get("added", []) + diff.get("changed", [])
            if not rels:
                self.q.put(("update_finish", True, diff.get("remote_version", "未知")))
                return
            total = len(rels)
            done = [0]

            def on_event(*a):
                ev = a[0] if a else ""
                if ev == "done" and len(a) >= 2 and a[1]:
                    done[0] += 1
                    self.q.put(("progress", 5 + 85 * done[0] // total))
                    self.q.put(("activity",
                                f"正在下载差异文件 {done[0]}/{total}…", True))
                elif ev == "switch" and len(a) >= 4:
                    self.q.put(("log", f"· 下载源切换：{a[2]} → {a[3]}", "muted"))
                elif ev == "retry" and len(a) >= 3:
                    self.q.put(("log", f"· 第 {a[2]} 轮重试 {a[1]} 个文件", "warn"))

            n_add = len(diff.get("added", []))
            n_chg = len(diff.get("changed", []))
            self.q.put(("activity", f"正在下载差异文件（共 {total} 个）…", True))
            self.q.put(("log", f"· 需下载 {total} 个差异文件"
                               f"（新增 {n_add} · 更新 {n_chg}）", "muted"))
            ok, fail = updater.download(root, remote, rels,
                                        threads=updater.read_dl_threads(root),
                                        on_event=on_event)
            if fail:
                raise RuntimeError(f"{len(fail)} 个文件下载失败：{fail[:3]}")
            if not ok:
                raise RuntimeError("下载未完成，请重试")
            self.q.put(("activity", "正在应用更新（需短暂关闭 MinerU）…", True))
            self.q.put(("log", "· 差异文件下载完成，正在应用更新…", "ok"))
            self.q.put(("progress", 93))
            if not updater.apply_update(root, remote, rels,
                                        tray_pid=getattr(self, "_update_tray_pid",
                                                         None)):
                raise RuntimeError("应用更新失败：暂存文件缺失")
            updater.clean_stage(root)
            self.q.put(("progress", 98))
            updater.restart_tray(root)
            self.q.put(("update_finish", True, remote.get("version", "未知")))
        except Exception as e:  # noqa: BLE001
            self.q.put(("log", "错误：" + str(e)))
            self.q.put(("update_finish", False, None))

    def _on_update_finish(self, ok, ver=None):
        """更新执行结束：成功 → 完成态；失败 → 恢复「立即更新」可重试。"""
        self._running = False
        self._act_t0 = None
        self._waiting_update = False
        self.spin.delete("all")
        self.title("MinerU 文档解析 · 一键安装")
        if ok:
            self._advance_progress(100)
            self._update_prompted = True
            self._set_activity(f"更新完成，已升级到 v{ver}", fg=SUCCESS)
            self._set_status("success", f"更新完成 v{ver}")
            self._set_buttons("done")
            self._append_log(f"✔ 更新完成，已升级到 v{ver}", "ok")
            self._append_log("· 已自动重启桌面托盘程序", "muted")
            if self._log_path:
                self._append_log(f"· 日志已保存：{self._log_path}", "muted")
            self._close_log_file()
            messagebox.showinfo(
                "更新完成", f"MinerU 已更新到 v{ver}，托盘程序已自动重启。")
        else:
            if getattr(self, "_checking", False):
                # 检查阶段失败：无差异清单可执行，恢复常规状态而非「立即更新」
                self._checking = False
                self._update_mode = False
                self._set_activity("检查更新失败，请稍后重试", fg=DANGER)
                self._set_status("error", "检查更新失败")
                self._set_buttons("idle")
                self._append_log("■ 检查更新失败，详见上方日志", "err")
            else:
                self._update_ready = True
                self._set_activity("更新失败，可点击「立即更新」重试", fg=DANGER)
                self._set_status("error", "更新失败")
                self._set_buttons("check_done")
                self._append_log("■ 更新失败，详见上方日志", "err")
        self._refit_width()

    def _start_uninstall(self):
        """卸载：确认后拉起独立卸载程序。"""
        root = _detect_installed_root()
        if not root:
            messagebox.showinfo("卸载", "尚未检测到已安装的 MinerU。")
            return
        un = os.path.join(root, "卸载MinerU.exe")
        if not os.path.isfile(un):
            messagebox.showerror("无法卸载", f"未找到卸载程序：\n{un}")
            return
        if messagebox.askyesno(
                "卸载 MinerU",
                f"确定要卸载 MinerU 吗？\n\n安装位置：{root}\n\n"
                "卸载将停止服务并清理全部文件，此操作不可撤销。"):
            subprocess.Popen([un], creationflags=_NO_WINDOW)

    def _apply_adaptive_geometry(self):
        """按实际内容需求计算初始窗口与最小尺寸（适配 DPI 缩放，避免控件溢出）。
        先按「已安装」状态布置维护按钮，确保初始宽度容纳全部按钮（防压盖）。"""
        self._set_buttons("idle")
        self.update_idletasks()
        req_w = self.winfo_reqwidth()
        req_h = self.winfo_reqheight()
        init_w = max(req_w, 780)
        init_h = req_h + 170          # 为日志区预留可视高度
        sw, sh = self.winfo_screenwidth(), self.winfo_screenheight()
        init_w = min(init_w, int(sw * 0.96))
        init_h = min(init_h, int(sh * 0.92))
        self.geometry(f"{init_w}x{init_h}")
        self.minsize(max(req_w + 20, 640), req_h)

    def _refit_width(self):
        """按钮布局变化后重算窗口宽度，避免新增按钮溢出压盖（如完成/修复后）。"""
        try:
            self.update_idletasks()
            req = self.winfo_reqwidth()
            cur = self.winfo_width()
            if req > cur:
                sw = self.winfo_screenwidth()
                new_w = min(req + 24, int(sw * 0.96))
                if new_w > cur:
                    x = max(self.winfo_rootx() - (new_w - cur) // 2, 0)
                    self.geometry(f"{new_w}x{self.winfo_height()}"
                                  f"+{x}+{self.winfo_rooty()}")
                    self.update_idletasks()
            try:
                self.minsize(max(self.winfo_reqwidth() + 20, 640),
                             self.minsize()[1])
            except Exception:
                pass
        except Exception:
            pass

    # ================= UI =================
    def _build_ui(self):
        root = self
        root.grid_columnconfigure(0, weight=1)
        root.grid_rowconfigure(2, weight=1)

        # ---- 渐变 Hero 头部 ----
        hero = tk.Canvas(root, height=122, width=0, bg=BG, highlightthickness=0)
        hero.grid(row=0, column=0, sticky="ew")
        hero.bind("<Configure>", lambda e: self._draw_hero())
        self._hero = hero

        # ---- 主内容区 ----
        body = tk.Frame(root, bg=BG)
        body.grid(row=1, column=0, sticky="ew")
        body.grid_columnconfigure(0, weight=1)

        # 安装位置卡片（带阴影）
        card = self._shadow_card(body, row=0, padx=36, pady=(18, 6))
        card.grid_columnconfigure(0, weight=1)
        tk.Label(card, text="安装位置", font=(FONT, 11, "bold"),
                 bg=CARD, fg=INK).grid(row=0, column=0, sticky="w", padx=22, pady=(16, 0))
        self.pill = Pill(card, "准备就绪", "idle")
        self.pill.grid(row=0, column=1, sticky="e", padx=22, pady=(16, 0))
        frow = tk.Frame(card, bg=CARD)
        frow.grid(row=1, column=0, columnspan=2, sticky="ew", padx=22, pady=(10, 2))
        frow.grid_columnconfigure(0, weight=1)
        self.path_var = tk.StringVar()
        self.field = Field(frow, self.path_var)
        self.field.grid(row=0, column=0, sticky="ew")
        self.path_entry = self.field.entry
        self.path_entry.bind("<Return>", lambda e: self.start())
        self.btn_browse = GradButton(frow, "浏览", self._browse, kind="secondary",
                                     height=42, padx=22, icon=I_FOLDER)
        self.btn_browse.grid(row=0, column=1, padx=(12, 0))
        hrow = tk.Frame(card, bg=CARD)
        hrow.grid(row=2, column=0, columnspan=2, sticky="w", padx=24, pady=(0, 14))
        self.path_hint_icon = tk.Label(hrow, text="", font=(ICON_FONT, 10),
                                       bg=CARD, fg=MUTED)
        self.path_hint_icon.pack(side="left")
        self.path_hint = tk.Label(hrow, text="", font=(FONT, 9), bg=CARD, fg=MUTED,
                                  anchor="w")
        self.path_hint.pack(side="left", padx=(5, 0))

        # 快捷方式选项
        self.shortcut_var = tk.BooleanVar(value=True)
        self.tick = TickBox(body, "在桌面创建「MinerU 文档解析」快捷方式", self.shortcut_var)
        self.tick.grid(row=1, column=0, sticky="w", padx=40, pady=(10, 2))

        # 下载线程数（多源竞速引擎池大小；与安装路径一起持久化）
        trow = tk.Frame(body, bg=BG)
        trow.grid(row=2, column=0, sticky="w", padx=40, pady=(4, 2))
        tk.Label(trow, text="下载线程数", font=(FONT, 9), bg=BG, fg=MUTED).pack(side="left")
        self.dl_threads_var = tk.IntVar(value=16)
        self.dl_threads_spin = tk.Spinbox(
            trow, from_=4, to=64, increment=1, width=4, textvariable=self.dl_threads_var,
            font=(FONT_MONO, 9), relief="flat", bd=1, bg="#f4f6f8", fg=INK,
            buttonbackground="#e8edf1", buttoncursor="hand2", justify="center",
            highlightthickness=1, highlightbackground="#e5eaee",
            highlightcolor=ACCENT_L)
        self.dl_threads_spin.pack(side="left", padx=(10, 6))
        tk.Label(trow, text="越大下载越快、占用越高（4-64，默认 16）",
                 font=(FONT, 9), bg=BG, fg=FAINT).pack(side="left", padx=(0, 6))

        # 进度卡片（步骤条 + 组件清单 + 活动 + 进度 + 日志）
        pcard = self._shadow_card(root, row=2, sticky="nsew", padx=36, pady=(6, 6))
        pcard.grid_columnconfigure(0, weight=1)
        pcard.grid_rowconfigure(5, weight=1)

        tk.Label(pcard, text="安装进度", font=(FONT, 11, "bold"),
                 bg=CARD, fg=INK).grid(row=0, column=0, sticky="w", padx=22, pady=(16, 0))
        # 步骤条（图标）
        self._build_stepper(pcard)
        # 组件清单面板（实时状态卡片，可展开明细）
        self.comps = CompPanel(pcard)
        self.comps.grid(row=2, column=0, sticky="ew", padx=10, pady=(4, 0))
        # 当前活动行：加载圈 + 实时文案（正在下载哪个包/模型、速度等）
        actrow = tk.Frame(pcard, bg=CARD)
        actrow.grid(row=3, column=0, sticky="ew", padx=22, pady=(12, 0))
        self.spin = tk.Canvas(actrow, width=18, height=18, bg=CARD,
                              highlightthickness=0)
        self.spin.pack(side="left")
        self.act_lbl = tk.Label(actrow, text="等待开始", font=(FONT, 9), bg=CARD,
                                fg=MUTED, anchor="w")
        self.act_lbl.pack(side="left", padx=(8, 0), fill="x", expand=True)
        # 进度条
        prow = tk.Frame(pcard, bg=CARD)
        prow.grid(row=4, column=0, sticky="ew", padx=22, pady=(4, 0))
        prow.grid_columnconfigure(0, weight=1)
        self.step_lbl = tk.Label(prow, text="", font=(FONT, 9), bg=CARD, fg=MUTED,
                                 anchor="w")
        self.step_lbl.grid(row=0, column=0, sticky="w")
        self.elapsed_lbl = tk.Label(prow, text="", font=(FONT_MONO, 9), bg=CARD,
                                    fg=FAINT)
        self.elapsed_lbl.grid(row=0, column=1, sticky="e", padx=(0, 10))
        self.pct = tk.Label(prow, text="0%", font=(FONT_MONO, 9, "bold"), bg=CARD,
                            fg=ACCENT_D)
        self.pct.grid(row=0, column=2, sticky="e")
        self.pbar = tk.Canvas(prow, height=10, width=0, bg=CARD, highlightthickness=0)
        self.pbar.grid(row=1, column=0, columnspan=3, sticky="ew", pady=(6, 0))
        self.pbar.bind("<Configure>", lambda e: self._draw_progress())
        # 日志（圆角浅底终端）
        logwrap = tk.Frame(pcard, bg=CARD)
        logwrap.grid(row=5, column=0, sticky="nsew", padx=22, pady=(10, 16))
        logwrap.grid_columnconfigure(0, weight=1)
        logwrap.grid_rowconfigure(0, weight=1)
        logbg = tk.Canvas(logwrap, bg=BG, width=0, height=0, highlightthickness=0)
        logbg.grid(row=0, column=0, sticky="nsew")
        self._logbg = logbg
        self.flog = tk.Text(logbg, state="disabled", wrap="word", width=10, height=3,
                            font=(FONT_MONO, 9), bg=BG, fg=INK,
                            relief="flat", padx=16, pady=10, cursor="arrow")
        self.flog.place(relx=0.5, rely=0.5, anchor="center", relwidth=0.985, relheight=0.97)
        self.flog.tag_configure("muted", foreground=FAINT)
        self.flog.tag_configure("stage", foreground=ACCENT_D, font=(FONT_MONO, 9, "bold"))
        self.flog.tag_configure("err", foreground=DANGER)
        self.flog.tag_configure("ok", foreground=SUCCESS)
        self.flog.tag_configure("warn", foreground=WARN)
        logbg.bind("<Configure>", lambda e: self._draw_logbg())

        # ---- 底部操作 ----
        foot = tk.Frame(root, bg=BG)
        foot.grid(row=3, column=0, sticky="ew", padx=36, pady=(8, 20))
        foot.grid_columnconfigure(0, weight=1)
        lefts = tk.Frame(foot, bg=BG)
        lefts.grid(row=0, column=0, sticky="w")
        self.btn_repair = GradButton(lefts, "检测修复", self._start_repair,
                                     kind="secondary", height=40, fontsize=9,
                                     icon=I_SETTING)
        self.btn_update = GradButton(lefts, "检查更新", self._start_check_update,
                                     kind="secondary", height=40, fontsize=9,
                                     icon=I_REFRESH)
        self.btn_uninstall = GradButton(lefts, "卸载", self._start_uninstall,
                                        kind="ghost", height=40, fontsize=9,
                                        icon=I_TRASH)
        self.btn_pause = GradButton(lefts, "暂停", self._toggle_pause, kind="secondary",
                                    height=40, fontsize=9)
        self.btn_stop = GradButton(lefts, "停止", self._stop_run, kind="ghost",
                                   height=40, fontsize=9)
        acts = tk.Frame(foot, bg=BG)
        acts.grid(row=0, column=1, sticky="e")
        self.btn_confirm_fix = GradButton(acts, "开始修复", self._confirm_repair,
                                          kind="primary", height=40, icon=I_SETTING)
        self.btn_confirm_fix.grid(row=0, column=0, padx=(0, 10))
        self.btn_confirm_fix.grid_remove()
        self.btn_open = GradButton(acts, "打开目录", self._open_dir, kind="secondary",
                                   height=40, icon=I_FOLDER)
        self.btn_open.grid(row=0, column=1, padx=(0, 10))
        self.btn_open.grid_remove()
        self.btn_primary = GradButton(acts, "开始安装", self.start, kind="primary",
                                      height=40, icon=I_PLAY)
        self.btn_primary.grid(row=0, column=2)
        self._reflow_left()

    def _draw_hero(self):
        """渐变头部：深青→深蓝斜向渐变 + logo + 标题 + 版本胶囊。"""
        cv = self._hero
        cv.delete("all")
        w, h = cv.winfo_width() or 820, 122
        # 两段水平渐变叠底（深青 → 更深蓝青）
        _hgrad_rect(cv, 0, 0, w, h, "#0a4a5c", "#0e6f83")
        # 底部亮色 accent 线
        cv.create_rectangle(0, h - 3, w, h, fill=ACCENT_2, outline="")
        # logo：白色圆角块 + 主色 M
        lx, ly, ls = 36, 30, 60
        _rr(cv, lx, ly, lx + ls, ly + ls, 16, fill="#ffffff", outline="")
        cv.create_text(lx + ls / 2, ly + ls / 2, text="M",
                       font=(FONT, 26, "bold"), fill=ACCENT_D)
        # 标题 / 副标题
        cv.create_text(lx + ls + 22, ly + 14, anchor="w", text="MinerU 文档解析",
                       font=(FONT, 19, "bold"), fill="#ffffff")
        cv.create_text(lx + ls + 22, ly + 42, anchor="w",
                       text="PDF / 图片 → Markdown · 本地运行 · 无需命令行",
                       font=(FONT, 10), fill="#a7d3de")
        # 右上版本胶囊（窄窗口下隐藏，避免与标题/副标题重叠）
        if w >= 600:
            ver = "v" + APP_VERSION
            tw = tkfont.Font(font=(FONT, 9, "bold")).measure(ver)
            _rr(cv, w - tw - 52, 24, w - tw - 12, 46, 11, fill="#12586b", outline="")
            cv.create_text(w - 32 - tw / 2, 35, text=ver, font=(FONT, 9, "bold"),
                           fill="#8fd0dd")

    def _shadow_card(self, parent, row=None, **gkw):
        """白色卡片 + 双层柔阴影（统一 grid 布局）。"""
        sh1 = tk.Frame(parent, bg=SHADOW1)
        if row is None:
            raise ValueError("shadow card 必须指定 grid row")
        sh1.grid(row=row, column=0, **{"sticky": "nsew", **gkw})
        sh1.grid_columnconfigure(0, weight=1)
        sh1.grid_rowconfigure(0, weight=1)
        sh2 = tk.Frame(sh1, bg=SHADOW2)
        sh2.grid(row=0, column=0, sticky="nsew", padx=2, pady=2)
        sh2.grid_columnconfigure(0, weight=1)
        sh2.grid_rowconfigure(0, weight=1)
        card = tk.Frame(sh2, bg=CARD)
        card.grid(row=0, column=0, sticky="nsew", padx=4, pady=4)
        return card

    def _build_stepper(self, parent):
        """图标式步骤条：圆角图标块 + 连接线。"""
        st = tk.Frame(parent, bg=CARD)
        st.grid(row=1, column=0, sticky="ew", padx=22, pady=(12, 0))
        n = len(STAGES)
        # 连接线列为弹性列：窄窗口收缩、宽窗口展开；图标列保持自然宽度
        for col in range(1, n * 2, 2):
            st.grid_columnconfigure(col, weight=1, minsize=0)
        self._steps, self._links = [], []
        for i, (name, _, _ic) in enumerate(STAGES):
            if i > 0:
                link = tk.Canvas(st, height=2, width=0, bg=CARD, highlightthickness=0)
                link.grid(row=0, column=i * 2 - 1, sticky="ew", padx=10,
                          pady=(0, 20))
                self._links.append(link)
                link.bind("<Configure>",
                          lambda e, idx=i - 1: self._draw_link(idx))
            cell = tk.Frame(st, bg=CARD)
            cell.grid(row=0, column=i * 2)
            c = tk.Canvas(cell, width=44, height=44, bg=CARD, highlightthickness=0)
            c.pack()
            label = tk.Label(cell, text=name, font=(FONT, 9), bg=CARD, fg=FAINT)
            label.pack(pady=(6, 0))
            self._steps.append((c, label))
            self._draw_step(i, "pending")

    def _draw_step(self, i, state):
        """state: pending / current / done"""
        cv, label = self._steps[i]
        cv.delete("all")
        _, _, ic = STAGES[i]
        if state == "pending":
            _rr(cv, 2, 2, 42, 42, 12, fill="#f0f3f5", outline="")
            cv.create_text(22, 22, text=ic, font=(ICON_FONT, 15), fill="#b3bcc5")
            label.config(fg=FAINT)
        elif state == "current":
            _vgrad(cv, 2, 2, 42, 42, ACCENT_2, ACCENT, r=12)
            cv.create_text(22, 22, text=ic, font=(ICON_FONT, 15), fill="#ffffff")
            _rr(cv, 0, 0, 44, 44, 13, fill="", outline=ACCENT_L, width=3, tags="ring")
            label.config(fg=INK, font=(FONT, 9, "bold"))
        else:
            _rr(cv, 2, 2, 42, 42, 12, fill=ACCENT_L, outline="")
            cv.create_text(22, 22, text=ic, font=(ICON_FONT, 15), fill=ACCENT_D)
            # 右下角完成徽章
            cv.create_oval(30, 30, 44, 44, fill=SUCCESS, outline="#ffffff", width=2)
            cv.create_line(35, 37.5, 38, 41, 42, 34, fill="#ffffff", width=2,
                          capstyle="round", joinstyle="round")
            label.config(fg=ACCENT_D, font=(FONT, 9))

    def _draw_link(self, idx):
        cv = self._links[idx]
        cv.delete("all")
        w = max(cv.winfo_width(), 4)
        color = ACCENT if idx < self._stage_now else TRACK
        cv.create_rectangle(0, 0, w, 2, fill=color, outline="")

    def _draw_progress(self):
        """圆角轨道 + 水平渐变填充 + 运行中流光扫动（证明未卡死）。"""
        cv = self.pbar
        cv.delete("all")
        w = max(cv.winfo_width(), 20)
        h = 10
        _rr(cv, 1, 0, w - 1, h, 5, fill=TRACK, outline="")
        if self._progress > 0:
            x = max(int(w * self._progress / 100), 12)
            # 渐变填充（裁成圆角：画渐变后叠轨道色圆角端盖）
            _hgrad_rect(cv, 2, 2, x - 2, h - 2, ACCENT_2, ACCENT)
            _rr(cv, max(x - 10, 6), 2, x - 2, h - 2, 4, fill=ACCENT, outline="")
            cv.create_rectangle(6, 2, x - 10, h - 2, fill=ACCENT, outline="")
            # 流光：在已填充区间内来回扫动的亮色条（仅运行中）
            if self._running and not self._paused and x > 36:
                span = x - 24
                sx = 6 + int(self._mq_x % (span * 2))          # 往返扫动
                if sx > x - 18:
                    sx = x - 18 - (sx - (x - 18))               # 折返
                sx = max(6, sx)
                cv.create_rectangle(sx, 2, min(sx + 16, x - 2), h - 2,
                                    fill=_mix(ACCENT, "#ffffff", 0.62), outline="")

    def _draw_logbg(self):
        cv = self._logbg
        cv.delete("all")
        w = max(cv.winfo_width(), 40)
        h = max(cv.winfo_height(), 40)
        _rr(cv, 0, 0, w - 1, h - 1, 10, fill=BG, outline="#e5eaee", width=1)

    def _set_status(self, kind, text):
        """kind: idle/accent/success/error（兼容旧 (color,text) 调用）。"""
        if isinstance(kind, str) and kind in Pill._KIND:
            self.pill.set(text, kind)
        else:  # 旧签名 (颜色, 文本) 映射
            m = {"#77808c": "idle", ACCENT: "accent", SUCCESS: "success",
                 DANGER: "error"}
            self.pill.set(text, m.get(kind, "accent"))

    def _append_log(self, s, tag=None):
        self.flog.config(state="normal")
        if tag is not None:
            self.flog.insert("end", s + "\n", tag)
        else:
            self.flog.insert("end", s + "\n")
        self.flog.see("end")
        self.flog.config(state="disabled")
        # 同步写入日志文件（带时间戳）
        if self._log_fh:
            try:
                ts = time.strftime("%H:%M:%S")
                self._log_fh.write(f"[{ts}] {s}\n")
                self._log_fh.flush()
            except OSError:
                pass

    def _reset_progress(self):
        """新流程开始前进度归零（安装/检测/更新，避免上一轮 100% 残留）。"""
        self._progress = 0
        self._draw_progress()
        self.pct.config(text="0%")

    def _stop_flow_anim(self):
        """流程（检测/检查更新）结束：停止加载圈、秒表与标题进度刷新。"""
        self._running = False
        self._act_t0 = None
        self.spin.delete("all")
        self.title("MinerU 文档解析 · 一键安装")

    def _advance_progress(self, value, force=False):
        """进度只前进不回退（阶段间重复事件不会把进度拉回去）。"""
        if not force:
            value = max(self._progress, value)
        self._progress = value
        self._draw_progress()
        self.pct.config(text=f"{int(value)}%")

    def _set_activity(self, text, stopwatch=False, fg=None):
        """更新「当前活动」行；stopwatch=True 时显示该活动已持续的时间。"""
        self._act_base = text
        self._act_t0 = time.time() if stopwatch else None
        self.act_lbl.config(text=text, fg=fg or MUTED)

    # ---- 界面动画（运行中证明「活着」，防误判卡死） ----
    def _anim_tick(self):
        if not self._running:
            return
        if not self._paused:
            self._spin = (self._spin + 28) % 360
            self._draw_spin()
            self._mq_x += 5
            self._draw_progress()
            self._pulse += 0.13
            self._draw_pulse()
            self.comps.pulse()
            self.pill.pulse()
        self.after(70, self._anim_tick)

    def _sec_tick(self):
        if not self._running:
            return
        if self._t0 is not None:
            self.elapsed_lbl.config(text="已用时 " + _fmt_dur(time.time() - self._t0))
            self.title("MinerU 文档解析 · 一键安装 · %d%%" % int(self._progress))
        if self._act_t0 is not None:
            txt = "%s · 已 %s" % (
                self._act_base, _fmt_dur(time.time() - self._act_t0))
            if (self._pkg_last_ts and self.worker and self.worker.is_alive()
                    and time.time() - self._pkg_last_ts > 90):
                # 依赖下载 90s 无任何进展（后端 180s 自动终止换源重试），提前提示
                txt += "（下载疑似停滞，系统将自动切换源重试，请稍候…）"
            self.act_lbl.config(text=txt)
        self.after(1000, self._sec_tick)

    def _draw_spin(self):
        cv = self.spin
        cv.delete("all")
        if self._paused:
            cv.create_rectangle(4, 3, 7, 15, fill=FAINT, outline="")
            cv.create_rectangle(11, 3, 14, 15, fill=FAINT, outline="")
            return
        cv.create_arc(2.5, 2.5, 15.5, 15.5, start=self._spin, extent=110,
                      style="arc", width=2.6, outline=ACCENT_2)

    def _draw_pulse(self):
        """当前阶段图标外环呼吸（颜色/粗细缓慢脉动）。"""
        if 0 <= self._stage_now < len(self._steps):
            cv = self._steps[self._stage_now][0]
            t = (math.sin(self._pulse) + 1) / 2
            cv.delete("ring")
            _rr(cv, 0, 0, 44, 44, 13, fill="",
                outline=_mix(ACCENT_L, "#ffffff", t * 0.75),
                width=2.5 + t * 2.0, tags="ring")

    # ---- 后端结构化事件 → 界面反馈 ----
    def _handle_comp(self, rest):
        """[comp] 组件状态事件：id|status|detail（wait/checking/downloading/installing/ok/fail）。"""
        parts = rest.split("|")
        if len(parts) != 3:
            return
        cid, status, detail = parts
        if not cid or status not in ("wait", "checking", "downloading",
                                     "installing", "ok", "fail"):
            return
        self.comps.set_comp(cid, status, detail)
        # 组件状态变化全部写入日志（检测/安装过程可完整回溯：缺什么、装了什么）
        if status == "fail":
            self._append_log("✗ 组件[" + cid + "] " + detail, "err")
        elif status == "wait":
            self._append_log("· 组件[" + cid + "] 待安装：" + detail, "muted")
        elif status == "ok":
            self._append_log("✓ 组件[" + cid + "] " + detail, "ok")
        elif status == "checking":
            self._append_log("… 组件[" + cid + "] 检测中", "muted")
        elif status == "downloading":
            self._append_log("… 组件[" + cid + "] 下载中：" + detail, "muted")
        elif status == "installing":
            self._append_log("… 组件[" + cid + "] " + detail, "muted")

    def _handle_pkg(self, rest):
        """[pkg] 事件：resolved|N|t / down|包|大小 / prepared|N|t / installing|包 / installed|N|t
        venv 行状态随阶段流转：解析→下载中→安装中→已就绪（由 [comp] venv|ok 收尾）。"""
        parts = rest.split("|")
        act = parts[0] if parts else ""
        try:
            if act == "resolved" and len(parts) >= 2:
                self._pkg_total = int(parts[1])
                self._pkg_done = 0
                self._pkg_names.clear()
                self.comps.set_comp("venv", "downloading",
                                    f"已解析 {self._pkg_total} 个依赖包，开始下载 …")
                self._set_activity("依赖解析完成（共 %d 个包），开始下载 …" % self._pkg_total)
            elif act == "down" and len(parts) >= 2:
                name = parts[1]
                size = parts[2] if len(parts) > 2 else ""
                # 同一包可能重复出现（pip 分片/重试），明细只记首次，避免重复行与误标
                is_new = bool(name) and name not in self._pkg_names
                if is_new:
                    self._pkg_names.add(name)
                    self._pkg_done += 1
                    self.comps.set_pkg_feed(name, size,
                                            self._pkg_done, self._pkg_total or 0)
                # pip 回退路径无 resolved 事件（_pkg_total 为 0），此时不显示「第 N/0 个包」
                if self._pkg_total:
                    d = f"正在下载 {name} · 第 {self._pkg_done}/{self._pkg_total} 个包"
                    if size:
                        d = (f"正在下载 {name}（{size}）"
                             f"· 第 {self._pkg_done}/{self._pkg_total} 个包")
                else:
                    d = f"正在下载 {name}" + (f"（{size}）" if size else "")
                self.comps.set_comp("venv", "downloading", d)
                label = "正在下载 %s%s" % (name, f"（{size}）" if size else "")
                if self._pkg_total:
                    label += " · 第 %d/%d 个包" % (self._pkg_done, self._pkg_total)
                    self._advance_progress(10 + 40 * self._pkg_done / self._pkg_total)
                self._set_activity(label, stopwatch=True, fg=INK)
                self._pkg_last_ts = time.time()   # 有进展：刷新停滞时间戳
            elif act == "prepared" and len(parts) >= 2:
                # 下载阶段结束，进入安装：明细行统一收尾为「✓ 已下载」
                self._pkg_last_ts = 0.0   # 退出下载停滞监控（安装阶段无逐行输出）
                self.comps.mark_pkg_all_downloaded()
                self.comps.set_comp("venv", "installing",
                                    "依赖下载完成，正在安装到虚拟环境 …")
                self._set_activity("依赖下载完成，正在安装到虚拟环境 …")
            elif act == "installing":
                # pip 下载完成后进入安装阶段（Installing collected packages: ...）
                # （pip 无 prepared 事件，此处同样收尾明细行）
                self._pkg_last_ts = 0.0
                self.comps.mark_pkg_all_downloaded()
                self._set_activity("正在安装依赖到虚拟环境 …", stopwatch=True)
                if self._pkg_total:
                    self.comps.set_comp("venv", "installing",
                                        f"正在安装依赖包（共 {self._pkg_total} 个）…")
            elif act == "installed" and len(parts) >= 2:
                n, t = parts[1], (parts[2] if len(parts) > 2 else "")
                txt = f"已安装 {n} 个包" + (f"（{t}）" if t else "")
                self._append_log("✓ " + txt, "ok")
                self._set_activity("依赖就绪：" + txt)
        except (ValueError, IndexError):
            pass

    def _comps_checking(self):
        """进入检测/安装/修复：全部组件先置「检测中」，由后续事件逐个更新真实状态。"""
        self.comps.set_all("checking", "检测中…")

    def _handle_mbeat(self, rest):
        """[mbeat] 模型下载心跳（后端字段：d/t|比率|已下载GB|总GB|速度MB/s|文件名）"""
        parts = rest.split("|")
        if len(parts) < 6:
            return
        try:
            d, t = (int(x) for x in parts[0].split("/"))
            ratio = float(parts[1])
            got, total = float(parts[2]), float(parts[3])
            speed = float(parts[4])
            names = parts[5]
        except ValueError:
            return
        self.comps.set_model_feed(d, t, got, total, speed, names)
        txt = f"已下载 {got:.2f}/{total:.2f} GB · {speed:.1f} MB/s · {d}/{t} 个文件"
        if speed > 0.5 and ratio < 0.999:
            remain = (total - got) / speed
            if remain > 90:
                txt += f" · 预计剩余约 {int(remain // 60)} 分钟"
            elif remain > 5:
                txt += f" · 预计剩余 {int(remain)} 秒"
        if names:
            shown = names if len(names) <= 42 else names[:39] + "…"
            txt += f"（{shown}）"
        self._set_activity(txt, fg=INK)
        self._advance_progress(50 + 25 * min(max(ratio, 0.0), 1.0))

    def _handle_theat(self, rest):
        """[theat] torch 预下载心跳（字段同 mbeat：d/t|比率|已下载GB|总GB|速度MB/s|文件名）"""
        parts = rest.split("|")
        if len(parts) < 6:
            return
        try:
            ratio = float(parts[1])
            got, total = float(parts[2]), float(parts[3])
            speed = float(parts[4])
            names = parts[5]
        except ValueError:
            return
        if total <= 0:
            return
        txt = f"预下载 CUDA torch {got:.2f}/{total:.2f} GB · {speed:.1f} MB/s"
        if speed > 0.5 and ratio < 0.999:
            remain = (total - got) / speed
            if remain > 90:
                txt += f" · 预计剩余约 {int(remain // 60)} 分钟"
            elif remain > 5:
                txt += f" · 预计剩余 {int(remain)} 秒"
        if names:
            shown = names if len(names) <= 42 else names[:39] + "…"
            txt += f"（{shown}）"
        self._set_activity(txt, fg=INK)
        self._advance_progress(10 + 15 * min(max(ratio, 0.0), 1.0))

    # ================= 交互 =================
    def _fill_presets(self):
        prefs = self._load_prefs()
        self.path_var.set(prefs.get("path") or r"C:\MinerU_App")
        try:
            self.dl_threads_var.set(min(64, max(4, int(prefs.get("dl_threads", 16)))))
        except (TypeError, ValueError):
            self.dl_threads_var.set(16)
        self.path_var.trace_add("write", lambda *_: self._refresh_path_hint())
        self._refresh_path_hint()
        self._flog_placeholder()
        self.path_entry.focus_set()

    def _load_prefs(self):
        """读取上次安装偏好（安装路径 + 下载线程数）。"""
        try:
            with open(_prefs_path(), encoding="utf-8") as f:
                d = json.load(f)
            return d if isinstance(d, dict) else {}
        except Exception:
            return {}

    def _save_prefs(self):
        try:
            os.makedirs(os.path.dirname(_prefs_path()), exist_ok=True)
            with open(_prefs_path(), "w", encoding="utf-8") as f:
                json.dump({"path": self.path_var.get().strip(),
                           "dl_threads": self._dl_threads()},
                          f, ensure_ascii=False)
        except OSError:
            pass

    def _dl_threads(self):
        try:
            return min(64, max(4, int(self.dl_threads_var.get())))
        except Exception:
            return 16

    def _refresh_path_hint(self):
        p = self.path_var.get().strip()
        icon, text, fg = I_DISK, "", MUTED
        if not p:
            icon, text, fg = I_ERROR, "请选择或输入一个安装位置", DANGER
        else:
            try:
                if os.path.isfile(p):
                    icon, text, fg = I_ERROR, "该路径是一个文件，请更换位置", DANGER
                else:
                    # 仅检查、不创建目录：向上回溯到最近存在的父目录做可写性/空间探测。
                    # 此前这里直接 Path(p).mkdir(...)，用户每输入一个字符就新建一个目录。
                    base = p
                    while base and not os.path.isdir(base):
                        parent = os.path.dirname(base)
                        if parent == base:
                            break
                        base = parent
                    if not base or not os.path.isdir(base):
                        icon, text, fg = I_ERROR, "无法读取该位置，请检查路径", DANGER
                    elif not os.access(base, os.W_OK):
                        icon, text, fg = I_ERROR, "该目录不可写，请更换位置", DANGER
                    else:
                        gig = shutil.disk_usage(base).free / (1024 ** 3)
                        if gig < 5:
                            text, fg = f"剩余空间 {gig:.1f}GB，建议 ≥ 5GB", WARN
                        elif os.path.isdir(p):
                            text, fg = f"磁盘可用 {gig:.1f}GB · 可写入", SUCCESS
                        else:
                            text, fg = (f"目录不存在，安装时自动创建 · "
                                        f"磁盘可用 {gig:.1f}GB", SUCCESS)
            except Exception:
                icon, text, fg = I_ERROR, "无法读取该位置，请检查路径", DANGER
        self.path_hint_icon.config(text=icon, fg=fg)
        self.path_hint.config(text=text, fg=fg)

    def _browse(self):
        d = filedialog.askdirectory(initialdir=self.path_var.get() or os.path.expanduser("~"),
                                    title="选择安装目录")
        if d:
            self.path_var.set(os.path.normpath(d))

    def _flog_placeholder(self, clear=False):
        self.flog.config(state="normal")
        self.flog.delete("1.0", "end")
        if not clear:
            self.flog.insert("end", "\n")
            self.flog.insert("end", "一切就绪\n", "stage")
            self.flog.insert("end", "确认安装目录后，点击「开始安装」自动完成环境、依赖与模型安装\n",
                            "muted")
        self.flog.config(state="disabled")

    def _update_stage(self, tag):
        idx = _TAG_STAGE.get(tag, -1)
        if idx < 0:
            return
        self._stage_now = idx
        for i in range(len(STAGES)):
            self._draw_step(i, "done" if i < idx else "current" if i == idx else "pending")
        for j in range(len(self._links)):
            self._draw_link(j)
        self._advance_progress(_STAGE_FLOOR[idx])
        self.step_lbl.config(text=f"第 {idx + 1}/{len(STAGES)} 步 · {STAGES[idx][1]}")
        self._append_log(f"—— 第 {idx + 1}/{len(STAGES)} 步 · {STAGES[idx][1]} ——", "stage")
        self._set_status("accent", _TAG_HINT.get(tag, STAGES[idx][1]))
        self._set_activity(_TAG_HINT.get(tag, STAGES[idx][1]), stopwatch=True, fg=INK)

    def _reflow_left(self, *btns):
        """按需重排底部左侧按钮（连续列，避免隐藏后留空列）。"""
        for b in (self.btn_repair, self.btn_update, self.btn_uninstall,
                  self.btn_pause, self.btn_stop):
            b.grid_forget()
        for i, b in enumerate(btns):
            b.grid(row=0, column=i, padx=(0, 0) if i == 0 else (10, 0))

    def _set_buttons(self, state):
        # 离开「更新运行中」后恢复维护按钮可用（更新中置灰，其余状态常态可用）
        for b in (self.btn_repair, self.btn_update, self.btn_uninstall):
            b.set_enabled(True)
        if state == "idle":
            installed = bool(_detect_installed_root())
            self._update_mode = False
            self._update_ready = False
            self._waiting_update = False
            self.btn_primary.grid()
            self.btn_primary.set_enabled(True)
            self.btn_primary.set_text("重新安装" if installed else "开始安装")
            self.btn_confirm_fix.grid_remove()
            if installed:
                self.btn_open.grid()
                self._reflow_left(self.btn_repair, self.btn_update,
                                  self.btn_uninstall)
            else:
                self.btn_open.grid_remove()
                self._reflow_left()
        elif state == "checking":
            # 检测/检查更新阶段：只保留「停止」可点（检测很快，无需暂停）；
            # 更新检查为只读网络对比，无取消接口，展示维护按钮但不显示「停止」
            self.btn_primary.grid()
            self.btn_primary.set_enabled(False)
            self.btn_primary.set_text("检查更新中…" if self._update_mode else "检测中…")
            self.btn_confirm_fix.grid_remove()
            self.btn_open.grid_remove()
            if self._update_mode:
                self._reflow_left(self.btn_repair, self.btn_update,
                                  self.btn_uninstall)
            else:
                self._reflow_left(self.btn_stop)
        elif state == "check_done":
            if self._update_mode:
                # 更新检查完成（发现新版本）：主按钮变「立即更新」，无修复确认按钮
                self.btn_primary.grid()
                self.btn_primary.set_enabled(True)
                self.btn_primary.set_text("立即更新")
                self.btn_confirm_fix.grid_remove()
                self.btn_open.grid()
                self._reflow_left(self.btn_repair, self.btn_update,
                                  self.btn_uninstall)
            else:
                # 检测完成：展示「开始修复」等待用户确认，是否修复由用户点击决定
                self.btn_confirm_fix.grid()
                self.btn_open.grid()
                self.btn_primary.grid_remove()
                self._reflow_left(self.btn_repair, self.btn_update,
                                  self.btn_uninstall)
        elif state == "upd_waiting":
            # 更新等待退出：主按钮变「取消等待」，可随时中止
            self.btn_primary.grid()
            self.btn_primary.set_enabled(True)
            self.btn_primary.set_text("取消等待")
            self.btn_confirm_fix.grid_remove()
            self.btn_open.grid_remove()
            self._reflow_left(self.btn_repair, self.btn_update,
                              self.btn_uninstall)
        elif state == "running":
            self.btn_primary.grid()
            self.btn_primary.set_enabled(False)
            self.btn_primary.set_text(
                "更新中…" if self._update_mode else
                ("修复中…" if self._repair_mode else "安装中…"))
            self.btn_confirm_fix.grid_remove()
            self.btn_open.grid_remove()
            if self._update_mode:
                # 更新为「下载差异 + 应用」，无暂停/停止接口：维护按钮置灰展示
                self._reflow_left(self.btn_repair, self.btn_update,
                                  self.btn_uninstall)
                for b in (self.btn_repair, self.btn_update, self.btn_uninstall):
                    b.set_enabled(False)
            else:
                self.btn_pause.set_text("暂停")
                self.btn_pause.set_enabled(True)
                self._reflow_left(self.btn_pause, self.btn_stop)
        elif state == "paused":
            self.btn_pause.set_text("继续")
        elif state == "done":
            self._update_mode = False
            self._update_ready = False
            self._waiting_update = False
            self.btn_primary.grid()
            self.btn_primary.set_enabled(True)
            self.btn_primary.set_text("重新安装")
            self.btn_confirm_fix.grid_remove()
            self.btn_open.grid()
            self._reflow_left(self.btn_repair, self.btn_update,
                              self.btn_uninstall)

    # ================= 生命周期 =================
    def start(self):
        # 更新流程调度：等待中 → 取消等待；已发现新版本 → 立即更新
        if getattr(self, "_waiting_update", False):
            self._cancel_wait_update()
            return
        if getattr(self, "_update_ready", False):
            self._start_update()
            return
        if self.worker and self.worker.is_alive():
            return
        root = self.path_var.get().strip()
        root = os.path.normpath(root) if root else ""
        if not root:
            messagebox.showwarning("提示", "请先填写安装目录")
            return
        if not os.path.isdir(root):
            try:
                Path(root).mkdir(parents=True, exist_ok=True)
            except Exception as e:
                messagebox.showerror("目录无效", f"无法创建安装目录：\n{e}")
                return
        if not os.access(root, os.W_OK):
            messagebox.showerror("目录无效", f"安装目录不可写，请更换位置：\n{root}")
            return
        if not self._close_running_app(root):
            return
        self._cancel.clear()
        self._paused = False
        self._proc = None
        self._pysetup = None
        self._repair_mode = False
        self._checking = False
        self._update_mode = False
        self._repair_root = None
        self._safe_unlink(os.path.join(root, ".install_pause"))
        self._stage_now = -1
        self._dl_threads_val = self._dl_threads()   # 主线程读取，worker 线程安全使用
        self._save_prefs()
        for i in range(len(STAGES)):
            self._draw_step(i, "pending")
        for j in range(len(self._links)):
            self._draw_link(j)
        self.step_lbl.config(text="")
        self._reset_progress()
        self._set_buttons("running")
        self._init_log_file()
        self._flog_placeholder(clear=True)
        self._append_log("· 选择目录：" + root)
        if self._log_path:
            self._append_log(f"· 日志文件：{self._log_path}", "muted")
        self.comps.reset()
        self._comps_checking()
        # 动画与计时：加载圈 / 进度流光 / 阶段脉动 / 秒表
        self._pkg_total = None
        self._pkg_done = 0
        self._pkg_names.clear()
        self._spin = 0
        self._mq_x = 0.0
        self._pulse = 0.0
        self._running = True
        self._t0 = time.time()
        self._set_activity("正在准备…", stopwatch=True, fg=INK)
        self._anim_tick()
        self._sec_tick()
        self.worker = threading.Thread(target=self._run, args=(root,), daemon=True)
        self.worker.start()
        self.after(80, self._poll)

    # ---- 检测修复（两阶段：先检测展示结果 → 用户确认 → 执行修复）----
    def _start_repair(self):
        """阶段一·检测：仅扫描已安装组件并展示状态，不修改任何内容。
        用户核对结果后点击「开始修复」才进入阶段二（_confirm_repair）。"""
        if self.worker and self.worker.is_alive():
            return
        root = _detect_installed_root() or self.path_var.get().strip()
        # 必须是真实安装目录（安装清单或 venv 存在），防止对空目录/新路径误检并诱导修复
        real_install = (root and os.path.isdir(root) and (
            os.path.isfile(os.path.join(root, ".install_manifest.json"))
            or os.path.isfile(os.path.join(root, "runtime", "venv",
                                           "Scripts", "python.exe"))))
        if not real_install:
            messagebox.showwarning("提示", "未检测到已安装的 MinerU，请先完成安装。")
            return
        # 检测为只读操作，无需关闭运行中的应用；关闭动作推迟到确认修复时
        self._cancel.clear()
        self._paused = False
        self._proc = None
        self._pysetup = None
        self._repair_mode = True
        self._checking = True
        self._update_mode = False
        self._repair_root = root
        self._safe_unlink(os.path.join(root, ".install_pause"))
        self._stage_now = -1
        self._dl_threads_val = self._dl_threads()
        self._save_prefs()
        for i in range(len(STAGES)):
            self._draw_step(i, "pending")
        for j in range(len(self._links)):
            self._draw_link(j)
        self.step_lbl.config(text="")
        self._reset_progress()
        self._set_buttons("checking")
        self._init_log_file()
        self._flog_placeholder(clear=True)
        self._append_log("· 检测修复：先扫描已安装组件（仅检测，不修改）")
        self._append_log(f"· 安装目录：{root}")
        if self._log_path:
            self._append_log(f"· 日志文件：{self._log_path}", "muted")
        self.comps.reset()
        self._comps_checking()
        self._pkg_total = None
        self._pkg_done = 0
        self._pkg_names.clear()
        self._spin = 0
        self._mq_x = 0.0
        self._pulse = 0.0
        self._running = True
        self._t0 = time.time()
        self._set_activity("正在检测组件状态…", stopwatch=True, fg=INK)
        self._anim_tick()
        self._sec_tick()
        self.worker = threading.Thread(target=self._run_check, args=(root,), daemon=True)
        self.worker.start()
        self.after(80, self._poll)

    def _run_check(self, root):
        """检测阶段 worker：运行 install_flow.py --check-only，只输出组件状态不修改。
        结束时统计是否存在待修复（wait/fail）组件，need=True 才进入确认修复阶段。"""
        try:
            self.q.put(("progress", 3))
            self.q.put(("status", "accent", "正在检测组件状态…"))
            # 优先用系统 Python，其次用已装 venv Python
            py = self._find_python()
            if not py:
                venv_py = os.path.join(root, "runtime", "venv", "Scripts", "python.exe")
                if os.path.isfile(venv_py):
                    py = [venv_py]
            if not py:
                raise RuntimeError("未找到 Python 3.11，无法执行检测")
            self.q.put(("comp", "python|ok|Python 3.11 已就绪"))
            self.q.put(("log", "Python: " + " ".join(py)))

            need = False
            res = _src_root()
            flow = os.path.join(_resource_dir(), "install_flow.py")
            cmd = [*py, flow, "--root", root, "--src", res, "--check-only",
                   "--dl-threads", str(getattr(self, "_dl_threads_val", 16))]
            self._proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                          stderr=subprocess.STDOUT,
                                          encoding="utf-8", errors="replace",
                                          creationflags=_NO_WINDOW)
            for line in self._proc.stdout:
                if self._cancel.is_set():
                    break
                line = line.rstrip()
                if not line:
                    continue
                m = re.match(r"^\[([a-z]+)\]\s?(.*)$", line)
                if m:
                    tag, rest = m.group(1), m.group(2)
                    if tag == "comp":
                        self.q.put(("comp", rest))
                        parts = rest.split("|")
                        if len(parts) == 3 and _needs_repair(parts[0], parts[1]):
                            need = True
                        continue
                    if tag == "pkg":
                        self.q.put(("pkg", rest))
                        continue
                    self.q.put(("tag", f"[{tag}] {rest}",
                                "stage" if tag in _TAG_STAGE else None))
                else:
                    self.q.put(("log", line))
            self._proc.wait()
            if self._cancel.is_set():
                raise _Cancelled("用户停止检测")
            if self._proc.returncode != 0:
                raise RuntimeError("组件检测失败")
            self.q.put(("check_done", root, need))
        except _Cancelled:
            # 检测阶段未产生任何修改，无需清理半成品
            self.q.put(("done", False, True))
        except Exception as e:  # noqa: BLE001
            self.q.put(("log", "错误：" + str(e)))
            self.q.put(("done", False, False))

    def _on_check_done(self, root, need=True):
        """检测完成：need=True 存在待修复组件 → 展示「开始修复」等待确认；
        need=False 所有组件正常 → 直接回到完成状态，不引导执行修复。"""
        self._repair_root = root
        self._checking = False
        self._stop_flow_anim()
        self._advance_progress(100)
        self._append_log(">>> 检测完成（仅检测，未修改任何内容）", "ok")
        if need:
            self._set_activity("检测完成，请确认是否开始修复", fg="#2e7d32")
            self._set_status("accent", "检测完成，请确认是否开始修复")
            self._set_buttons("check_done")
            self._append_log("· 请核对上方组件状态，确认无误后点击「开始修复」", "muted")
        else:
            self._set_activity("检测完成，所有组件正常，无需修复", fg="#2e7d32")
            self._set_status("success", "检测完成，所有组件正常，无需修复")
            self._set_buttons("done")
            self._append_log("✓ 核心组件（主程序/环境/模型）均正常，无需修复", "ok")
            # 可选增强项未启用时给出说明，避免用户看到 wait 状态却无修复入口而困惑
            states = self.comps.get_states()
            if states.get("cuda", ("", ""))[0] in ("wait", "fail"):
                self._append_log("· GPU 加速未启用（当前为 CPU 模式，"
                                 "如需加速可重新安装）", "muted")
            if states.get("shortcut", ("", ""))[0] in ("wait", "fail"):
                self._append_log("· 桌面快捷方式未创建（可打开安装目录手动启动）",
                                 "muted")
        self._refit_width()

    def _confirm_repair(self):
        """阶段二·修复：用户确认后执行实际修复（install_flow.py --repair）。"""
        if self.worker and self.worker.is_alive():
            return
        root = getattr(self, "_repair_root", None) or (
            _detect_installed_root() or self.path_var.get().strip())
        if not root or not os.path.isdir(root):
            messagebox.showwarning("提示", "安装目录不存在，请重新检测。")
            self._set_buttons("idle")
            return
        if not self._close_running_app(root):
            return
        self._cancel.clear()
        self._paused = False
        self._proc = None
        self._pysetup = None
        self._checking = False
        self._safe_unlink(os.path.join(root, ".install_pause"))
        self._stage_now = -1
        self._progress = 0
        self._advance_progress(0, force=True)
        self.comps.reset()
        self._comps_checking()
        self._running = True
        self._t0 = time.time()
        self._set_buttons("running")
        self._set_activity("正在修复…", stopwatch=True, fg=INK)
        self.worker = threading.Thread(target=self._run_repair, args=(root,), daemon=True)
        self.worker.start()
        self.after(80, self._poll)

    def _run_repair(self, root):
        """修复模式 worker：运行 install_flow.py --repair。"""
        try:
            self.q.put(("progress", 3))
            self.q.put(("status", "accent", "正在修复…"))
            self.q.put(("log", "· 用户已确认，开始执行修复"))
            # 优先用系统 Python，其次用已装 venv Python
            py = self._find_python()
            if not py:
                venv_py = os.path.join(root, "runtime", "venv", "Scripts", "python.exe")
                if os.path.isfile(venv_py):
                    py = [venv_py]
            if not py:
                raise RuntimeError("未找到 Python 3.11，无法执行修复")
            self.q.put(("comp", "python|ok|Python 3.11 已就绪"))
            self.q.put(("log", "Python: " + " ".join(py)))

            res = _src_root()
            flow = os.path.join(_resource_dir(), "install_flow.py")
            result = os.path.join(root, "install_result.json")
            pause_file = os.path.join(root, ".install_pause")
            cmd = [*py, flow, "--root", root, "--src", res, "--result", result,
                   "--pause-file", pause_file, "--repair",
                   "--dl-threads", str(getattr(self, "_dl_threads_val", 16)),
                   *([] if self.shortcut_var.get() else ["--no-shortcut"])]
            self.q.put(("stage", "copy"))
            self.q.put(("progress", 10))
            self._proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                          stderr=subprocess.STDOUT,
                                          encoding="utf-8", errors="replace",
                                          creationflags=_NO_WINDOW)
            stage_now = 0
            for line in self._proc.stdout:
                if self._cancel.is_set():
                    break
                line = line.rstrip()
                if not line:
                    continue
                m = re.match(r"^\[([a-z]+)\]\s?(.*)$", line)
                if m:
                    tag, rest = m.group(1), m.group(2)
                    if tag == "pkg":
                        self.q.put(("pkg", rest))
                        continue
                    if tag == "mbeat":
                        self.q.put(("mbeat", rest))
                        continue
                    if tag == "theat":
                        self.q.put(("theat", rest))
                        continue
                    if tag == "comp":
                        self.q.put(("comp", rest))
                        continue
                    self.q.put(("tag", f"[{tag}] {rest}",
                                "stage" if tag in _TAG_STAGE else None))
                    if tag in _TAG_STAGE:
                        stage_now = _TAG_STAGE[tag]
                        self.q.put(("stage", tag))
                    if tag == "pause":
                        if "已暂停" in rest:
                            self.q.put(("status", "accent", "已暂停（点击「继续」恢复）"))
                        elif "已恢复" in rest:
                            self.q.put(("status", "accent", "修复中…"))
                    if tag == "model" and stage_now == 2:
                        mm = re.search(r"\((\d+)/(\d+)\)", rest)
                        if mm and "失败" not in rest:
                            d, t = int(mm.group(1)), int(mm.group(2))
                            if t:
                                self.q.put(("progress", 50 + 25 * d // t))
                else:
                    self.q.put(("log", line))
            self._proc.wait()
            if self._cancel.is_set():
                raise _Cancelled("用户停止修复")
            if self._proc.returncode != 0:
                raise RuntimeError("修复过程出错")
            self.q.put(("progress", 100))
            self.q.put(("log", ">>> 检测修复完成"))
            sc = 0
            try:
                with open(result, encoding="utf-8") as fh:
                    rj = json.load(fh)
                if "shortcut_ok" in rj:
                    sc = 1 if rj["shortcut_ok"] else 2
            except Exception:
                pass
            for tmp in ("install_result.json", ".install_state.json",
                        ".install_pause", "python-setup.exe"):
                self._safe_unlink(os.path.join(root, tmp))
            self.q.put(("done", True, False, sc))
        except _Cancelled:
            self._cleanup_after_stop(root)
            self.q.put(("done", False, True))
        except Exception as e:  # noqa: BLE001
            self.q.put(("log", "错误：" + str(e)))
            self.q.put(("done", False, False))

    # ---- 暂停 / 停止 ----
    def _toggle_pause(self):
        if not (self.worker and self.worker.is_alive()):
            return
        pause = os.path.join(self.path_var.get().strip(), ".install_pause")
        if self._paused:
            self._safe_unlink(pause)
            self._paused = False
            self._set_buttons("running")
            self._set_status("accent", "继续安装…")
            self._append_log("· 继续安装")
        else:
            Path(pause).touch()
            self._paused = True
            self._set_buttons("paused")
            self._set_status("accent", "已请求暂停（当前任务块完成后生效）")
            self._append_log("· 已请求暂停，将在当前任务块完成后停下")

    def _stop_run(self):
        if not (self.worker and self.worker.is_alive()):
            return
        if not messagebox.askyesno(
                "停止安装",
                "确定要停止安装吗？\n\n"
                "未完成的下载与半成品环境将被清理以释放空间；\n"
                "已校验完成的模型文件会保留，下次安装自动续传。"):
            return
        self._cancel.set()
        if self._paused:  # 先解除暂停阻塞，便于终止
            self._paused = False
            self._safe_unlink(os.path.join(self.path_var.get().strip(), ".install_pause"))
        self._kill_proc_tree(self._proc)
        self._kill_proc_tree(self._pysetup)
        self._set_buttons("running")
        self.btn_pause.set_enabled(False)
        self._set_status("error", "正在停止并清理…")

    def _kill_proc_tree(self, proc):
        """终止进程及其全部子进程（pip 等），避免残留孤儿进程。"""
        if not proc or proc.poll() is not None:
            return
        try:
            subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                           capture_output=True, timeout=30, creationflags=_NO_WINDOW)
        except Exception:
            try:
                proc.terminate()
            except Exception:
                pass

    def _find_app_procs(self, root):
        """返回运行中且可执行文件位于安装目录（root）下的 (pid, exe) 列表。
        这些进程会锁定主程序 exe/DLL，安装或修复覆盖前必须先关闭。"""
        root = os.path.normcase(os.path.abspath(root))
        found = []
        if psutil is not None:
            try:
                for p in psutil.process_iter(["pid", "exe"]):
                    try:
                        exe = p.info.get("exe")
                    except Exception:
                        exe = None
                    if exe and os.path.normcase(
                            os.path.abspath(exe)).startswith(root):
                        found.append((p.info["pid"], exe))
            except Exception:
                pass
        else:
            # 回退：PowerShell 按 ExecutablePath 前缀枚举（无 psutil 环境）
            try:
                ps = subprocess.run(
                    ["powershell", "-NoProfile", "-Command",
                     "$ps=Get-CimInstance Win32_Process | Where-Object { "
                     "$_.ExecutablePath -and $_.ExecutablePath.ToLower()"
                     ".StartsWith('" + root.lower() + "') }; "
                     "$ps | ForEach-Object { $_.ProcessId.ToString()+'|'+"
                     "$_.ExecutablePath }"],
                    capture_output=True, text=True, timeout=30,
                    creationflags=_NO_WINDOW)
                for line in ps.stdout.splitlines():
                    if "|" in line:
                        pid, exe = line.split("|", 1)
                        found.append((int(pid), exe))
            except Exception:
                pass
        return found

    def _close_running_app(self, root):
        """检测并关闭运行中的 MinerU（锁定主程序文件）。返回 False 表示用户取消。"""
        procs = self._find_app_procs(root)
        if not procs:
            return True
        names = sorted({os.path.basename(e) for _, e in procs})
        detail = "、".join(names[:5]) + (" 等" if len(names) > 5 else "")
        if not messagebox.askyesno(
                "MinerU 正在运行",
                f"检测到正在运行的 MinerU（{detail}，共 {len(procs)} 个进程）。\n\n"
                "安装/修复需要覆盖主程序文件，请先关闭它。\n"
                "是否立即关闭这些进程并继续？"):
            return False
        self._append_log(f"· 已关闭 {len(procs)} 个正在运行的 MinerU 进程"
                         f"（{detail}）", "warn")
        for pid, _ in procs:
            try:
                subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                               capture_output=True, timeout=30,
                               creationflags=_NO_WINDOW)
            except Exception:
                pass
        return True

    def _safe_unlink(self, path):
        try:
            if os.path.exists(path):
                os.remove(path)
        except OSError:
            pass

    def _cleanup_after_stop(self, root):
        """停止安装后清理半成品：暂停标志、结果文件、未完成下载、半成品环境。
        检测阶段（_checking）只读未产生任何内容，直接返回避免误删已装环境。"""
        if getattr(self, "_checking", False):
            return
        steps = set()
        try:
            with open(os.path.join(root, ".install_state.json"), encoding="utf-8") as f:
                steps = set(json.load(f).get("steps", []))
        except Exception:
            pass
        freed = [0]

        def _rm(path):
            try:
                if os.path.isfile(path) or os.path.islink(path):
                    try:
                        freed[0] += os.path.getsize(path)
                    except OSError:
                        pass
                    os.remove(path)
                elif os.path.isdir(path):
                    for dp, _, fns in os.walk(path):
                        for fn in fns:
                            try:
                                freed[0] += os.path.getsize(os.path.join(dp, fn))
                            except OSError:
                                pass
                    shutil.rmtree(path, ignore_errors=True)
            except OSError:
                pass

        _rm(os.path.join(root, ".install_pause"))
        _rm(os.path.join(root, "install_result.json"))
        _rm(os.path.join(root, ".install_state.json"))
        _rm(os.path.join(root, "python-setup.exe"))

        # 未完成的模型下载（保留已校验完成的文件，下次续传）
        cache = os.path.join(root, "runtime", "models_cache")
        kept = 0
        if os.path.isdir(cache):
            for dp, dns, fns in os.walk(cache):
                for fn in fns:
                    fp = os.path.join(dp, fn)
                    if fn.endswith(".part"):
                        _rm(fp)
                    else:
                        try:
                            kept += os.path.getsize(fp)
                        except OSError:
                            pass
                for dn in list(dns):
                    if dn.endswith(".parts"):
                        _rm(os.path.join(dp, dn))
        # 依赖未装完 → 半成品 venv 无用，整体删除
        if "deps" not in steps:
            _rm(os.path.join(root, "runtime", "venv"))
        # 复制未完成 → 已复制的主程序文件删除
        if "copy" not in steps:
            for d in ("MinerU文档解析", "src", "scripts", "release"):
                _rm(os.path.join(root, d))
            _rm(os.path.join(root, "卸载MinerU.exe"))
            _rm(os.path.join(root, "使用说明.html"))
        msg = f"清理完成：释放 {freed[0] / 1048576:.0f} MB"
        if kept:
            msg += f"；保留已校验模型 {kept / 1073741824:.2f} GB（下次安装自动续传）"
        self.q.put(("log", "· " + msg))

    def _poll(self):
        try:
            while True:
                item = self.q.get_nowait()
                kind = item[0]
                if kind == "log":
                    self._append_log(item[1])
                elif kind == "tag":
                    self._append_log(item[1], item[2])
                elif kind == "pkg":
                    self._handle_pkg(item[1])
                elif kind == "mbeat":
                    self._handle_mbeat(item[1])
                elif kind == "theat":
                    self._handle_theat(item[1])
                elif kind == "comp":
                    self._handle_comp(item[1])
                elif kind == "status":
                    self._set_status(item[1], item[2])
                elif kind == "stage":
                    self._update_stage(item[1])
                elif kind == "activity":
                    self._set_activity(item[1], stopwatch=True, fg=INK)
                elif kind == "progress":
                    self._advance_progress(item[1])
                elif kind == "check_done":
                    self._on_check_done(item[1],
                                        item[2] if len(item) > 2 else True)
                    return
                elif kind == "update_done":
                    self._on_update_done(item[1])
                    return
                elif kind == "update_finish":
                    self._on_update_finish(item[1],
                                           item[2] if len(item) > 2 else None)
                    return
                elif kind == "done":
                    self._finish(item[1], item[2],
                                 item[3] if len(item) > 3 else 0)
                    return
        except queue.Empty:
            pass
        if (self.worker and self.worker.is_alive()) or self._cancel.is_set():
            self.after(80, self._poll)

    def _finish(self, ok, cancelled=False, sc=0):
        self._running = False
        self._act_t0 = None
        self.spin.delete("all")
        was_check = getattr(self, "_checking", False)
        done_txt = "检测修复完成" if self._repair_mode else "安装完成"
        # 确保所有待处理的 UI 事件已刷新（组件面板等）
        self.update_idletasks()
        if ok:
            # 成功完成：将仍处于进行中/等待状态的组件标记为 "ok"
            for cid, (status, detail) in self.comps.get_states().items():
                if status in ("installing", "downloading", "checking", "wait"):
                    self.comps.set_comp(cid, "ok", detail or "已就绪")
            self.comps.finalize_feeds()
            self._set_activity(done_txt, fg="#2e7d32")
        elif cancelled:
            self._set_activity(("检测" if was_check else "安装") + "已停止", fg=DANGER)
        else:
            self._set_activity(("检测" if was_check else "安装") + "失败，详见日志", fg=DANGER)
        for i in range(len(STAGES)):
            self._draw_step(i, "done" if ok else "pending")
        for j in range(len(self._links)):
            self._draw_link(j)
        if ok:
            self._advance_progress(100)
            self._set_status("success", done_txt)
        elif cancelled:
            self._set_status("error", ("检测" if was_check else "安装") + "已停止")
            self._append_log("■ " + ("检测" if was_check else "安装") + "已停止", "err")
        else:
            self._set_status("error", ("检测" if was_check else "安装") + "失败")
        self._set_buttons("done" if ok or not cancelled else "idle")
        self._refit_width()   # 完成/恢复维护按钮后重算宽度，防新增按钮溢出
        if ok:
            self.btn_open.grid()
            if sc == 1:
                self._append_log(f"✔ {done_txt}，已生成桌面快捷方式", "ok")
            elif sc == 2:
                self._append_log(f"✔ {done_txt}（桌面快捷方式创建失败，可打开安装目录手动启动）", "ok")
            else:
                self._append_log(f"✔ {done_txt}", "ok")
            if self._log_path:
                self._append_log(f"· 日志已保存：{self._log_path}", "muted")
            self._close_log_file()
            if sc == 1:
                messagebox.showinfo(
                    "完成", f"{done_txt}！\n\n已在桌面创建「MinerU 文档解析」快捷方式，"
                    "双击即可一键启动。")
            elif sc == 2:
                messagebox.showinfo(
                    "完成", f"{done_txt}！\n\n桌面快捷方式创建失败，"
                    "可点击「打开安装目录」手动启动（MinerU文档解析 文件夹内的 exe）。")
            else:
                messagebox.showinfo(
                    "完成", f"{done_txt}！\n\n可点击「打开安装目录」查看文件与使用说明。")
            self._open_guide()
        elif not cancelled:
            if self._log_path:
                self._append_log(f"· 安装日志已保存：{self._log_path}", "muted")
            self._close_log_file()
            messagebox.showerror("失败", "安装未完成，请查看上方日志。")
        else:
            self._close_log_file()

    def _on_close(self):
        if self.worker and self.worker.is_alive():
            if not messagebox.askyesno(
                    "退出", "安装正在进行，退出将停止安装并清理未完成内容。\n确定退出吗？"):
                return
            self._cancel.set()
            root = self.path_var.get().strip()
            self._safe_unlink(os.path.join(root, ".install_pause"))
            self._kill_proc_tree(self._proc)
            self._kill_proc_tree(self._pysetup)
            self._cleanup_after_stop(root)
        self._close_log_file()
        self.destroy()

    def _find_guide(self):
        res = Path(_resource_dir())
        for cand in (res / "使用说明.html",               # 打包 _MEIPASS 根
                     res.parent / "使用说明.html",         # 紧邻目录
                     res.parents[1] / "release" / "使用说明.html"):  # 源码运行 → 项目根
            if cand.is_file():
                return cand
        return None

    def _open_guide(self):
        guide = self._find_guide()
        if guide:
            webbrowser.open("file:///" + str(guide))

    def _open_dir(self):
        root = self.path_var.get().strip()
        if os.path.isdir(root):
            os.startfile(root)  # noqa: S606

    # ================= 后台流程 =================
    def _find_python(self):
        """探测 Python 3.11，返回参数列表（如 ["py", "-3.11"]）或 None。

        打包后 sys.executable 指向安装器自身，运行它等于再开一个安装窗口，
        必须排除（这是此前关闭/安装时弹出新窗口的根因）。
        """
        cands = [] if getattr(sys, "frozen", False) else [sys.executable]
        cands += ["python", "py"]
        for cand in cands:
            for args in (["-3.11"], ["-3"], []):
                try:
                    r = subprocess.run([cand] + args + ["--version"],
                                       capture_output=True, text=True, timeout=15,
                                       creationflags=_NO_WINDOW)
                    v = r.stdout.strip() or r.stderr.strip()
                    if "Python 3.11" in v:
                        return [cand] + args
                except Exception:
                    pass
        return None

    def _download_python(self):
        """自动下载安装 Python 3.11。安装包落 %TEMP%（此前落安装根目录，
        装完残留 ~25MB），用完即删，安装目录保持结构化。"""
        self.q.put(("log", "未找到 Python 3.11，正在自动下载 ..."))
        tmp_dir = os.path.join(tempfile.gettempdir(), "MinerU_installer")
        os.makedirs(tmp_dir, exist_ok=True)
        installer = os.path.join(tmp_dir, "python-setup.exe")

        def _cancel_hook(_blocks, _bs, _total):
            if self._cancel.is_set():
                raise _Cancelled("用户停止安装")

        for url in _PY_URLS:
            try:
                self.q.put(("log", "下载 " + url))
                urllib.request.urlretrieve(url, installer, reporthook=_cancel_hook)
                if os.path.getsize(installer) > 10_000_000:
                    break
            except _Cancelled:
                raise
            except Exception as e:
                self.q.put(("log", "下载失败，换源：" + str(e)))
        if not os.path.isfile(installer) or os.path.getsize(installer) <= 10_000_000:
            return None
        self.q.put(("log", "安装 Python（静默）..."))
        self._pysetup = subprocess.Popen(
            [installer, "/quiet", "InstallAllUsers=0", "PrependPath=0", "Include_test=0"],
            creationflags=_NO_WINDOW)
        try:
            while self._pysetup.poll() is None:
                if self._cancel.is_set():
                    self._kill_proc_tree(self._pysetup)
                    raise _Cancelled("用户停止安装")
                time.sleep(0.5)
        finally:
            self._safe_unlink(installer)   # 安装包用完即删，不留临时文件
        return self._find_python()

    def _run(self, root):
        try:
            self.q.put(("progress", 3))
            self.q.put(("status", "accent", "正在校验 Python 与运行环境…"))
            self.q.put(("comp", "python|installing|正在检测系统 Python …"))
            py = self._find_python()
            if py:
                self.q.put(("comp", "python|ok|系统已有 Python 3.11"))
            else:
                self.q.put(("comp", "python|installing|未检测到，正在自动下载安装 …"))
                py = self._download_python()
                if py:
                    self.q.put(("comp", "python|ok|Python 3.11 已自动安装"))
            if not py:
                raise RuntimeError("自动安装 Python 失败，请手动安装 Python 3.11 后重试")
            self.q.put(("log", "Python: " + " ".join(py)))

            res = _src_root()
            flow = os.path.join(_resource_dir(), "install_flow.py")
            result = os.path.join(root, "install_result.json")
            pause_file = os.path.join(root, ".install_pause")
            cmd = [*py, flow, "--root", root, "--src", res, "--result", result,
                   "--pause-file", pause_file,
                   "--dl-threads", str(getattr(self, "_dl_threads_val", 16)),
                   *([] if self.shortcut_var.get() else ["--no-shortcut"])]
            self.q.put(("stage", "copy"))
            self.q.put(("progress", 10))
            self._proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                          stderr=subprocess.STDOUT,
                                          encoding="utf-8", errors="replace",
                                          creationflags=_NO_WINDOW)
            stage_now = 0
            for line in self._proc.stdout:
                if self._cancel.is_set():
                    break
                line = line.rstrip()
                if not line:
                    continue
                m = re.match(r"^\[([a-z]+)\]\s?(.*)$", line)
                if m:
                    tag, rest = m.group(1), m.group(2)
                    if tag == "pkg":      # 逐包下载事件：高频，只驱动活动行，不刷日志
                        self.q.put(("pkg", rest))
                        continue
                    if tag == "mbeat":    # 模型下载心跳：同上
                        self.q.put(("mbeat", rest))
                        continue
                    if tag == "theat":    # torch 预下载心跳：同上
                        self.q.put(("theat", rest))
                        continue
                    if tag == "comp":     # 组件状态事件：驱动组件面板，不刷日志
                        self.q.put(("comp", rest))
                        continue
                    self.q.put(("tag", f"[{tag}] {rest}",
                                "stage" if tag in _TAG_STAGE else None))
                    if tag in _TAG_STAGE:
                        stage_now = _TAG_STAGE[tag]  # worker 本地跟踪，避免与 UI 线程竞态
                        self.q.put(("stage", tag))
                    if tag == "pause":
                        if "已暂停" in rest:
                            self.q.put(("status", "accent", "已暂停（点击「继续」恢复）"))
                        elif "已恢复" in rest:
                            self.q.put(("status", "accent", "安装中…"))
                    if tag == "model" and stage_now == 2:
                        mm = re.search(r"\((\d+)/(\d+)\)", rest)
                        if mm and "失败" not in rest:
                            d, t = int(mm.group(1)), int(mm.group(2))
                            if t:
                                self.q.put(("progress", 50 + 25 * d // t))
                else:
                    self.q.put(("log", line))
            self._proc.wait()
            if self._cancel.is_set():
                raise _Cancelled("用户停止安装")
            if self._proc.returncode != 0:
                raise RuntimeError("环境安装环节失败")
            self.q.put(("progress", 100))
            self.q.put(("log", ">>> 环境安装完成"))
            guide = self._find_guide()
            if guide:
                os.makedirs(root, exist_ok=True)
                shutil.copy2(str(guide), os.path.join(root, "使用说明.html"))
            sc = 0  # 0=未勾选创建 1=创建成功 2=勾选但失败
            try:
                with open(result, encoding="utf-8") as fh:
                    rj = json.load(fh)
                if "shortcut_ok" in rj:
                    sc = 1 if rj["shortcut_ok"] else 2
            except Exception:
                pass
            # 安装成功：清掉流程临时文件，安装根只留结构化内容
            # （主程序/ runtime/ 卸载器/ 使用说明/ mineru.json/ .install_manifest.json）
            for tmp in ("install_result.json", ".install_state.json",
                        ".install_pause", "python-setup.exe"):
                self._safe_unlink(os.path.join(root, tmp))
            self.q.put(("done", True, False, sc))
        except _Cancelled:
            self._cleanup_after_stop(root)
            self.q.put(("done", False, True))
        except Exception as e:  # noqa: BLE001
            self.q.put(("log", "错误：" + str(e)))
            self.q.put(("done", False, False))


class _Cancelled(Exception):
    """用户主动停止安装。"""


def _detect_installed_root():
    """按上次安装路径检测是否已安装（存在安装清单即认定），返回安装根或 None。"""
    prefs = {}
    try:
        with open(_prefs_path(), encoding="utf-8") as f:
            prefs = json.load(f)
    except Exception:
        pass
    for root in (prefs.get("path"), r"C:\MinerU_App"):
        if root and os.path.isfile(os.path.join(root, ".install_manifest.json")):
            return root
    return None


def _installed_version(root):
    """读取已安装版本号（清单缺失返回 '未知'）。"""
    try:
        with open(os.path.join(root, ".install_manifest.json"), encoding="utf-8") as f:
            return str(json.load(f).get("version", "未知"))
    except Exception:
        return "未知"


def _ver_tuple(v):
    """版本字符串 'x.y.z[.build]' → 数字元组，用于大小比较。
    支持任意段数（发布版带构建时间戳，如 1.0.0.202609051215），
    无法解析返回 (0, 0, 0)。"""
    parts = re.findall(r"\d+", str(v).strip())
    if not parts:
        return (0, 0, 0)
    t = tuple(int(x) for x in parts)
    return t if len(t) >= 3 else t + (0,) * (3 - len(t))


def main():
    app = Installer()
    app.mainloop()


if __name__ == "__main__":
    main()