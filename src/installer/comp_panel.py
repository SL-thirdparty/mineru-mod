# -*- coding: utf-8 -*-
"""安装器组件清单面板：7 项组件的实时状态卡片 + 计数条 + 可展开明细。

事件驱动（由 installer_gui 分发）：
  set_comp(cid, status, detail)                  ← [comp] 事件
  set_pkg_feed(name, size, idx, total)            ← [pkg] 事件（venv 明细）
  set_model_feed(done, total, got, tot_gb, speed, names) ← [mbeat] 事件（models 明细）
  pulse()                                         ← 主窗口动画循环调用（instlaling 行呼吸）
  reset()                                         ← 重新安装时清零
"""
import math
import tkinter as tk
import tkinter.font as tkfont

# 视觉 token 与 installer_gui 保持一致（避免循环导入，取值副本）
CARD      = "#ffffff"
INK       = "#182430"
MUTED     = "#6d7885"
FAINT     = "#9aa5b1"
ACCENT    = "#0e7490"
ACCENT_2  = "#14b8a6"
ACCENT_L  = "#e2f2f6"
SUCCESS   = "#15803d"
SUCCESS_L = "#e4f4ea"
DANGER    = "#c0362c"
DANGER_L  = "#fbe9e7"
TRACK     = "#e7ebee"
DETAIL_BG = "#f7fafb"

FONT = "Microsoft YaHei UI"
FONT_MONO = "Consolas"

# 状态 → (圆点色, 徽章底, 徽章字, 徽章文本)
STATUS_STYLE = {
    "wait":       ("#c2cad2", TRACK,     MUTED,  "待安装"),
    "installing": (ACCENT_2,   ACCENT_L,  ACCENT, "安装中"),
    "ok":         (SUCCESS,    SUCCESS_L, SUCCESS, "已就绪"),
    "fail":       (DANGER,     DANGER_L,  DANGER,  "失败"),
}

# 组件固定顺序（清单行序）：(id, 显示名称)
COMPS = [
    ("python",   "Python 3.11 运行时"),
    ("uv",       "安装引擎 uv"),
    ("app",      "应用主程序"),
    ("venv",     "运行环境与依赖"),
    ("cuda",     "GPU 加速"),
    ("models",   "解析模型"),
    ("shortcut", "桌面快捷方式"),
]

# 可展开明细的组件
_EXPANDABLE = {"venv", "models"}

_ROW_H = 34          # 行高


def _mix(c1, c2, t):
    def rgb(c):
        c = c.lstrip("#")
        return tuple(int(c[i:i + 2], 16) for i in (0, 2, 4))
    r1, g1, b1 = rgb(c1)
    r2, g2, b2 = rgb(c2)
    return "#%02x%02x%02x" % (int(r1 + (r2 - r1) * t),
                              int(g1 + (g2 - g1) * t),
                              int(b1 + (b2 - b1) * t))


class _Row(tk.Canvas):
    """单行组件卡片：状态圆点 + 名称 + 徽章 + 详情；点击可展开（有明细的行）。"""

    def __init__(self, master, cid, name, on_toggle):
        super().__init__(master, height=_ROW_H, bg=CARD, highlightthickness=0,
                         cursor="hand2" if cid in _EXPANDABLE else "arrow")
        self.cid = cid
        self.name = name
        self._on_toggle = on_toggle
        self._status = "wait"
        self._detail = ""
        self._expanded = False
        self._pulse_t = 0.0
        self._measure = tkfont.Font(font=(FONT, 9))
        self._badge_measure = tkfont.Font(font=(FONT, 8, "bold"))
        self.bind("<Button-1>", lambda e: self._on_toggle(self.cid))
        self.bind("<Configure>", lambda e: self._draw())

    def set(self, status, detail):
        self._status = status
        self._detail = detail or ""
        self._draw()

    def set_expanded(self, on):
        self._expanded = on
        self._draw()

    def pulse(self):
        if self._status == "installing":
            self._pulse_t = (self._pulse_t + 0.09) % 1.0
            self._draw()

    # ---- 绘制 ----
    def _draw(self):
        cv = self
        cv.delete("all")
        w = max(cv.winfo_width(), 320)
        h = _ROW_H
        dot, bbg, bfg, btext = STATUS_STYLE.get(self._status, STATUS_STYLE["wait"])

        # 悬浮浅底（可展开行提示可点击）
        if self.cid in _EXPANDABLE and self._expanded:
            cv.create_rectangle(0, 0, w, h, fill="#f2f7f9", outline="")

        # 状态圆点（installing 呼吸：半径/亮度随相位脉动）
        cx, cy = 18, h / 2
        if self._status == "installing":
            t = (math.sin(self._pulse_t * 2 * math.pi) + 1) / 2
            r = 4.2 + t * 1.6
            halo = _mix(ACCENT_L, "#ffffff", 0.3 + t * 0.5)
            cv.create_oval(cx - r - 3.5, cy - r - 3.5, cx + r + 3.5, cy + r + 3.5,
                           fill=halo, outline="")
        else:
            r = 4.2
        cv.create_oval(cx - r, cy - r, cx + r, cy + r, fill=dot, outline="")

        # 名称
        cv.create_text(34, cy, text=self.name, anchor="w",
                       font=(FONT, 10), fill=INK)

        # 徽章（右侧）
        bw = self._badge_measure.measure(btext) + 18
        bx2 = w - 14
        bx1 = bx2 - bw
        cv.create_rectangle(bx1, cy - 9, bx2, cy + 9, fill=bbg, outline="")
        cv.create_text((bx1 + bx2) / 2, cy, text=btext, anchor="center",
                       font=(FONT, 8, "bold"), fill=bfg)

        # 详情（徽章左侧，超长省略）
        if self._detail:
            avail = bx1 - 44 - 10
            txt = self._detail
            while txt and self._measure.measure(txt) > avail and len(txt) > 1:
                txt = txt[:-1]
            if txt != self._detail:
                txt = txt[:-1] + "…"
            cv.create_text(bx1 - 10, cy, text=txt, anchor="e",
                           font=(FONT, 9), fill=MUTED)

        # 展开指示（可展开行）
        if self.cid in _EXPANDABLE:
            arrow = "\u25BE" if self._expanded else "\u25B8"   # ▾ / ▸
            cv.create_text(26, cy, text=arrow, font=(FONT, 8), fill=FAINT)


class _FeedPanel(tk.Frame):
    """组件明细面板：venv 显示逐包下载；models 显示文件级状态。
    数据持续更新（折叠时也保持最新），展开即见。"""

    def __init__(self, master, cid):
        super().__init__(master, bg=DETAIL_BG)
        self._cid = cid
        # 左侧 accent 竖线
        bar = tk.Frame(self, bg=ACCENT_L, width=3)
        bar.pack(side="left", fill="y", padx=(26, 0))
        box = tk.Frame(self, bg=DETAIL_BG)
        box.pack(side="left", fill="both", expand=True, padx=(8, 14), pady=5)
        self._box = box
        self._lines = []
        if cid == "venv":
            self._title = tk.Label(box, text="依赖安装明细", font=(FONT, 8, "bold"),
                                   bg=DETAIL_BG, fg=ACCENT, anchor="w")
            self._title.pack(fill="x")
            for _ in range(3):
                ln = tk.Label(box, text="—", font=(FONT_MONO, 8),
                              bg=DETAIL_BG, fg=MUTED, anchor="w", justify="left")
                ln.pack(fill="x")
                self._lines.append(ln)
        else:
            self._title = tk.Label(box, text="模型下载明细", font=(FONT, 8, "bold"),
                                  bg=DETAIL_BG, fg=ACCENT, anchor="w")
            self._title.pack(fill="x")
            self._main = tk.Label(box, text="—", font=(FONT, 9),
                                  bg=DETAIL_BG, fg=INK, anchor="w", justify="left")
            self._main.pack(fill="x")
            self._cur = tk.Label(box, text="", font=(FONT_MONO, 8),
                                 bg=DETAIL_BG, fg=MUTED, anchor="w", justify="left")
            self._cur.pack(fill="x")

    # ---- 数据更新 ----
    def push_pkg(self, name, size, idx, total):
        """[pkg] 事件 → venv 明细行滚动。"""
        if self._cid != "venv" or not self._lines:
            return
        txt = f"{name}  {size}" if size else name
        if total:
            txt = f"[{idx}/{total}] {txt}"
        # 轮转：最旧的抬到最上
        texts = [ln.cget("text") for ln in self._lines]
        texts = ([txt] + texts)[:len(self._lines)]
        for ln, t in zip(self._lines, texts):
            ln.config(text=t)

    def push_model(self, done, total, got, tot_gb, speed, names):
        """[mbeat] 事件 → models 明细。"""
        if self._cid != "models":
            return
        ratio = got / max(tot_gb, 0.001)
        self._main.config(
            text=f"{done}/{total} 个文件 · {got:.2f}/{tot_gb:.2f} GB（{ratio * 100:.0f}%）"
                 f" · {speed:.1f} MB/s")
        self._cur.config(text=("正在下载：" + names) if names else "")


class CompPanel(tk.Frame):
    """组件清单面板：标题+计数条 + 7 行组件卡片（可展开明细）。"""

    def __init__(self, master):
        super().__init__(master, bg=CARD)
        self._state = {}          # cid → (status, detail)
        self._rows = {}
        self._feeds = {}
        self._expanded = set()

        head = tk.Frame(self, bg=CARD)
        head.pack(fill="x", padx=(22, 20), pady=(12, 4))
        tk.Label(head, text="组件清单", font=(FONT, 10, "bold"),
                 bg=CARD, fg=INK).pack(side="left")
        self._counts = tk.Label(head, text="", font=(FONT, 9),
                                bg=CARD, fg=MUTED)
        self._counts.pack(side="right")

        self._list = tk.Frame(self, bg=CARD)
        self._list.pack(fill="x", padx=(6, 6), pady=(0, 10))
        for cid, name in COMPS:
            row = _Row(self._list, cid, name, self._toggle)
            row.pack(fill="x")
            self._rows[cid] = row
            self._state[cid] = ("wait", "")
            feed = _FeedPanel(self._list, cid) if cid in _EXPANDABLE else None
            if feed:
                feed.pack(fill="x")
                feed.pack_forget()
                self._feeds[cid] = feed
        self._refresh_counts()

    # ---- 公开接口 ----
    def set_comp(self, cid, status, detail):
        if cid not in self._rows:
            return
        self._state[cid] = (status, detail)
        self._rows[cid].set(status, detail)
        self._refresh_counts()

    def set_pkg_feed(self, name, size, idx, total):
        """[pkg] down|包|大小 → venv 行详情 + 明细滚动。"""
        feed = self._feeds.get("venv")
        if feed:
            feed.push_pkg(name, size, idx, total)
        if name:
            d = f"第 {idx}/{total} 个包 · {name}" if total else name
            if size:
                d += f"（{size}）"
            self.set_comp("venv", "installing", d)

    def set_model_feed(self, done, total, got, tot_gb, speed, names):
        """[mbeat] → models 行详情 + 明细。"""
        feed = self._feeds.get("models")
        if feed:
            feed.push_model(done, total, got, tot_gb, speed, names)
        d = f"{done}/{total} 个文件 · {got:.2f}/{tot_gb:.2f} GB · {speed:.1f} MB/s"
        self.set_comp("models", "installing", d)

    def pulse(self):
        for row in self._rows.values():
            row.pulse()

    def reset(self):
        for cid in self._rows:
            self.set_comp(cid, "wait", "")

    # ---- 内部 ----
    def _toggle(self, cid):
        if cid not in _EXPANDABLE:
            return
        feed = self._feeds.get(cid)
        if not feed:
            return
        if cid in self._expanded:
            self._expanded.discard(cid)
            feed.pack_forget()
            self._rows[cid].set_expanded(False)
        else:
            self._expanded.add(cid)
            feed.pack(after=self._rows[cid], fill="x")
            self._rows[cid].set_expanded(True)

    def _refresh_counts(self):
        n = len(self._state)
        ok = sum(1 for s, _ in self._state.values() if s == "ok")
        inst = sum(1 for s, _ in self._state.values() if s == "installing")
        wait = sum(1 for s, _ in self._state.values() if s == "wait")
        fail = sum(1 for s, _ in self._state.values() if s == "fail")
        parts = [f"{n} 项", f"已就绪 {ok}"]
        if inst:
            parts.append(f"安装中 {inst}")
        if wait:
            parts.append(f"待安装 {wait}")
        if fail:
            parts.append(f"失败 {fail}")
        self._counts.config(text=" · ".join(parts), fg=DANGER if fail else MUTED)
