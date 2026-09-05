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
    "checking":   ("#d97706", "#fef3c7", "#b45309", "检测中"),
    "downloading":("#2563eb", "#dbeafe", "#1d4ed8", "下载中"),
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


def _clip(text, font_m, avail):
    """文本超宽截断加省略号；可用宽度过小时返回空串（宁缺勿盖）。"""
    if not text or avail < 16:
        return ""
    if font_m.measure(text) <= avail:
        return text
    while text and font_m.measure(text + "…") > avail and len(text) > 1:
        text = text[:-1]
    return text + "…"


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
        self._name_measure = tkfont.Font(font=(FONT, 10))
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
        if self._status in ("installing", "downloading"):
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

        # 状态符号：已就绪 ✓ / 失败 ✗（替代圆点，状态一目了然）；
        # 下载中/安装中呼吸圆点，其余为静态圆点
        cy = h / 2
        sym = self._status in ("ok", "fail")
        cx = 16 if sym else 18
        if self._status == "ok":
            cv.create_text(cx, cy, text="✓", font=(FONT, 10, "bold"), fill=SUCCESS)
        elif self._status == "fail":
            cv.create_text(cx, cy, text="✗", font=(FONT, 10, "bold"), fill=DANGER)
        elif self._status in ("installing", "downloading"):
            t = (math.sin(self._pulse_t * 2 * math.pi) + 1) / 2
            r = 4.2 + t * 1.6
            halo = _mix(ACCENT_L, "#ffffff", 0.3 + t * 0.5)
            cv.create_oval(cx - r - 3.5, cy - r - 3.5, cx + r + 3.5, cy + r + 3.5,
                           fill=halo, outline="")
            cv.create_oval(cx - r, cy - r, cx + r, cy + r, fill=dot, outline="")
        else:
            r = 4.2
            cv.create_oval(cx - r, cy - r, cx + r, cy + r, fill=dot, outline="")

        # 徽章（右侧）
        bw = self._badge_measure.measure(btext) + 18
        bx2 = w - 14
        bx1 = bx2 - bw
        cv.create_rectangle(bx1, cy - 9, bx2, cy + 9, fill=bbg, outline="")
        cv.create_text((bx1 + bx2) / 2, cy, text=btext, anchor="center",
                       font=(FONT, 8, "bold"), fill=bfg)

        # 名称 + 详情布局：名称自左侧起排；详情右对齐到徽章左缘，
        # 可用宽按名称实际右端计算（此前固定从 44px 起算，长名称被详情盖住）
        name_x = 34
        name_txt = _clip(self.name, self._name_measure, bx1 - 10 - name_x)
        if name_txt:
            cv.create_text(name_x, cy, text=name_txt, anchor="w",
                           font=(FONT, 10), fill=INK)

        if self._detail:
            avail = bx1 - 10 - (name_x + self._name_measure.measure(name_txt)) - 14
            txt = _clip(self._detail, self._measure, avail)
            if txt:
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
            # 内部滚动容器：固定高度，可查看全部包记录
            wrap = tk.Frame(box, bg=DETAIL_BG)
            wrap.pack(fill="x")
            self._venv_sb = tk.Scrollbar(wrap, orient="vertical")
            self._venv_sb.pack(side="right", fill="y")
            self._venv_cv = tk.Canvas(wrap, bg=DETAIL_BG, highlightthickness=0,
                                       height=140, yscrollcommand=self._venv_sb.set)
            self._venv_cv.pack(side="left", fill="x", expand=True)
            self._venv_sb.config(command=self._venv_cv.yview)
            self._venv_list = tk.Frame(self._venv_cv, bg=DETAIL_BG)
            self._venv_win = self._venv_cv.create_window(0, 0, anchor="nw",
                                                           window=self._venv_list)
            self._venv_list.bind("<Configure>", lambda e: self._venv_cv.configure(
                scrollregion=self._venv_cv.bbox("all")))
            self._venv_cv.bind("<Configure>",
                lambda e: self._venv_cv.itemconfig(self._venv_win, width=e.width))
            self._venv_cv.bind("<Enter>", self._venv_bind_wheel)
            self._venv_cv.bind("<Leave>", self._venv_unbind_wheel)
            self._venv_labels = []
            self._venv_items = []   # (Label, 基础文本)：标记行状态时按基础文本重建
            self._MAX_VENV = 80
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
        """[pkg] 事件 → venv 明细行追加（保留全部记录，内部滚动查看）。
        行级状态：最新一行「… 下载中」，新行到来后上一行自动标记「✓ 已下载」；
        进入安装阶段由 mark_downloaded_all 统一收尾。"""
        if self._cid != "venv":
            return
        if self._venv_items:
            ln, base = self._venv_items[-1]
            ln.config(text="✓ " + base, fg=SUCCESS)
        txt = f"{name}  {size}" if size else name
        if total:
            txt = f"[{idx}/{total}] {txt}"
        ln = tk.Label(self._venv_list, text="… " + txt, font=(FONT_MONO, 8),
                      bg=DETAIL_BG, fg=ACCENT_2, anchor="w", justify="left")
        ln.pack(fill="x")
        self._venv_labels.append(ln)
        self._venv_items.append((ln, txt))
        while len(self._venv_labels) > self._MAX_VENV:
            old = self._venv_labels.pop(0)
            self._venv_items.pop(0)
            old.destroy()
        self._venv_cv.update_idletasks()
        self._venv_cv.yview_moveto(1.0)

    def mark_downloaded_all(self):
        """进入安装阶段：把 venv 明细中所有行标记为「✓ 已下载」。"""
        if self._cid != "venv":
            return
        for ln, base in self._venv_items:
            ln.config(text="✓ " + base, fg=SUCCESS)

    def push_model(self, done, total, got, tot_gb, speed, names):
        """[mbeat] 事件 → models 明细。"""
        if self._cid != "models":
            return
        ratio = got / max(tot_gb, 0.001)
        self._main.config(
            text=f"{done}/{total} 个文件 · {got:.2f}/{tot_gb:.2f} GB（{ratio * 100:.0f}%）"
                 f" · {speed:.1f} MB/s")
        self._cur.config(text=("… 正在下载：" + names) if names else "",
                         fg=ACCENT_2 if names else MUTED)

    # ---- 终态 / 重置 ----
    def finalize(self):
        """安装完成时标记明细为已完成：标题、行状态、内容行全部收尾。"""
        if self._cid == "venv":
            self._title.config(text="依赖安装明细（已完成）", fg=SUCCESS)
            for ln, base in self._venv_items:
                ln.config(text="✓ " + base, fg=SUCCESS)
        else:
            self._title.config(text="模型下载明细（已完成）", fg=SUCCESS)
            self._main.config(fg=SUCCESS)
            self._cur.config(text="✓ 全部文件已下载并通过完整性校验", fg=SUCCESS)

    def clear(self):
        """重新安装时清空明细。"""
        if self._cid == "venv":
            for ln in self._venv_labels:
                ln.destroy()
            self._venv_labels = []
            self._venv_items = []
            self._title.config(text="依赖安装明细", fg=ACCENT)
        else:
            self._title.config(text="模型下载明细", fg=ACCENT)
            self._main.config(text="—", fg=INK)
            self._cur.config(text="", fg=MUTED)

    # ---- venv 内部滚动辅助 ----
    def _venv_bind_wheel(self, _event=None):
        self._venv_cv.bind_all("<MouseWheel>", self._venv_on_wheel)

    def _venv_unbind_wheel(self, _event=None):
        self._venv_cv.unbind_all("<MouseWheel>")

    def _venv_on_wheel(self, event):
        self._venv_cv.yview_scroll(int(-1 * (event.delta / 120)), "units")


class CompPanel(tk.Frame):
    """组件清单面板：标题+计数条 + 7 行组件卡片（可展开明细，可滚动）。"""

    _MAX_VIEW_H = 260   # 可视区最大高度（超出则滚动）

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

        # 可滚动容器：Canvas + Scrollbar + 内部 Frame
        outer = tk.Frame(self, bg=CARD)
        outer.pack(fill="x", padx=(6, 6), pady=(0, 10))
        self._scrollbar = tk.Scrollbar(outer, orient="vertical",
                                       command=self._on_scroll)
        self._scrollbar.pack(side="right", fill="y")
        self._canvas = tk.Canvas(outer, bg=CARD, highlightthickness=0,
                                 height=self._MAX_VIEW_H,
                                 yscrollcommand=self._on_scroll_set)
        self._canvas.pack(side="left", fill="both", expand=True)
        self._list = tk.Frame(self._canvas, bg=CARD)
        self._canvas_window = self._canvas.create_window(
            0, 0, anchor="nw", window=self._list)
        self._list.bind("<Configure>", lambda e: self._update_scrollregion())
        self._canvas.bind("<Configure>", self._on_canvas_resize)
        self._canvas.bind("<Enter>", lambda e: self._bind_wheel())
        self._canvas.bind("<Leave>", lambda e: self._unbind_wheel())

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

    # ---- 滚动 ----
    def _update_scrollregion(self):
        self._canvas.configure(scrollregion=self._canvas.bbox("all"))

    def _on_canvas_resize(self, event):
        self._canvas.itemconfig(self._canvas_window, width=event.width)
        self._update_scrollregion()
        bbox = self._canvas.bbox("all")
        content_h = (bbox[3] - bbox[1]) if bbox else 0
        if content_h <= event.height:
            self._scrollbar.pack_forget()
        else:
            self._scrollbar.pack(side="right", fill="y")

    def _on_scroll(self, *args):
        self._canvas.yview(*args)

    def _on_scroll_set(self, first, last):
        self._scrollbar.set(first, last)
        if float(first) <= 0.0 and float(last) >= 1.0:
            self._scrollbar.pack_forget()
        else:
            self._scrollbar.pack(side="right", fill="y")

    def _bind_wheel(self):
        self._canvas.bind_all("<MouseWheel>", self._on_wheel)

    def _unbind_wheel(self):
        self._canvas.unbind_all("<MouseWheel>")

    def _on_wheel(self, event):
        self._canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")

    # ---- 公开接口 ----
    def set_comp(self, cid, status, detail):
        if cid not in self._rows:
            return
        self._state[cid] = (status, detail)
        self._rows[cid].set(status, detail)
        self._refresh_counts()

    def set_pkg_feed(self, name, size, idx, total):
        """[pkg] down|包|大小 → venv 明细行追加。行状态由 GUI 按阶段驱动
        （解析→下载中→安装中→[comp] venv|ok 收尾），此处只维护明细列表。"""
        feed = self._feeds.get("venv")
        if feed:
            feed.push_pkg(name, size, idx, total)

    def set_model_feed(self, done, total, got, tot_gb, speed, names):
        """[mbeat] → models 明细 + 行详情。
        行状态只推进不倒退：已就绪/失败/校验中（installing）不被心跳打回「下载中」。"""
        feed = self._feeds.get("models")
        if feed:
            feed.push_model(done, total, got, tot_gb, speed, names)
        cur = self._state.get("models", ("wait", ""))[0]
        if cur in ("wait", "downloading"):
            d = f"{done}/{total} 个文件 · {got:.2f}/{tot_gb:.2f} GB · {speed:.1f} MB/s"
            self.set_comp("models", "downloading", d)

    def pulse(self):
        for row in self._rows.values():
            row.pulse()

    def set_all(self, status, detail):
        """批量置状态（进入检测/安装前把所有组件置「检测中」）。"""
        for cid in self._rows:
            self.set_comp(cid, status, detail)

    def reset(self):
        for cid in self._rows:
            self.set_comp(cid, "wait", "")
        for feed in self._feeds.values():
            feed.clear()

    def finalize_feeds(self):
        """安装完成时，将明细面板标记为已完成。"""
        for feed in self._feeds.values():
            feed.finalize()

    def get_states(self):
        """返回 {cid: (status, detail)} 供外部（如修复功能）查询。"""
        return dict(self._state)

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
        self.after(50, self._update_scrollregion)

    def _refresh_counts(self):
        n = len(self._state)
        ok = sum(1 for s, _ in self._state.values() if s == "ok")
        busy = sum(1 for s, _ in self._state.values()
                   if s in ("installing", "downloading"))
        wait = sum(1 for s, _ in self._state.values() if s == "wait")
        fail = sum(1 for s, _ in self._state.values() if s == "fail")
        check = sum(1 for s, _ in self._state.values() if s == "checking")
        parts = [f"{n} 项", f"已就绪 {ok}"]
        if check:
            parts.append(f"检测中 {check}")
        if busy:
            parts.append(f"进行中 {busy}")
        if wait:
            parts.append(f"待安装 {wait}")
        if fail:
            parts.append(f"失败 {fail}")
        self._counts.config(text=" · ".join(parts), fg=DANGER if fail else MUTED)
