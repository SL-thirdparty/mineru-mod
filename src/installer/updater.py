# -*- coding: utf-8 -*-
"""MinerU 远程修复/升级器（P4）。

从用户 GitHub 仓库（SL-thirdparty/mineru-mod）dist 分支拉取 manifest.json，
与本地安装目录逐文件 sha256 对比：
  - 修复与升级二合一：哈希不一致 / 缺失的文件 = 差异文件，只补差异不全量重装；
  - venv / 模型缓存不在此通道（路径敏感且体积大），损坏由安装器修复模式重建。

镜像链（国内优先，逐级回退，均经字节精确性实测）：
  ghproxy.net → raw.githubusercontent.com → gh-proxy.com → cdn.jsdelivr.net/gh
  （jsdelivr 对大文件可能 403，仅作最后兜底；所有下载均带 sha256 校验，
  镜像内容被改动时自动换源重试，不会落盘损坏文件）

用法：
  python updater.py --check --root C:\\MinerU_App     # 检查差异，输出 JSON
  python updater.py --gui  --root C:\\MinerU_App      # 图形界面（托盘菜单入口）
  python updater.py --gui --root ... --tray-pid 1234  # 应用前先终止托盘进程树
"""
import argparse
import json
import os
import re
import socket
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request

_NO_WINDOW = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0

for _s in (sys.stdout, sys.stderr):
    try:
        if _s and _s.encoding and _s.encoding.lower().replace("-", "") != "utf8":
            _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

# 打包后 updater.pyc 与 fastdl.pyc 同在 _MEIPASS 根；源码运行时 fastdl 在 <根>/scripts/
_HERE = os.path.dirname(os.path.abspath(__file__))
for _p in (_HERE, os.path.join(os.path.dirname(os.path.dirname(_HERE)), "scripts"),
           os.path.join(_HERE, "scripts")):
    if os.path.isdir(_p) and _p not in sys.path:
        sys.path.insert(0, _p)

REPO = "SL-thirdparty/mineru-mod"
DIST_BRANCH = "dist"
TRAY_EXE = "MinerU文档解析.exe"
STAGE_DIR = ".update"                    # 差异文件下载暂存目录（安装根下）
MANIFEST_LOCAL = ".install_manifest.json"
WEBUI_PORT = 7860                        # WebUI 服务端口（与托盘启动器一致）
UA = "Mozilla/5.0 (MinerU-Updater)"


def source_bases():
    """拉取源前缀链：四类镜像的文件 URL 均为 <base>/<urlencoded(rel)>。
    国内优先（ghproxy.net 实测字节精确含大文件），官方源次之。
    可用环境变量 MINERU_UPDATE_BASES（分号分隔）覆盖（测试/自建镜像用）。"""
    env = os.environ.get("MINERU_UPDATE_BASES")
    if env:
        return [b.strip() for b in env.split(";") if b.strip()]
    raw = f"https://raw.githubusercontent.com/{REPO}/{DIST_BRANCH}"
    return [
        f"https://ghproxy.net/{raw}",
        raw,
        f"https://gh-proxy.com/{raw}",
        f"https://cdn.jsdelivr.net/gh/{REPO}@{DIST_BRANCH}",
    ]


def _url(base, rel):
    return f"{base}/{urllib.parse.quote(rel)}"


def file_sha256(path):
    import hashlib
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def fetch_manifest(timeout=10):
    """镜像链逐级拉取远端 manifest.json，全部失败抛 RuntimeError。"""
    bases = source_bases()
    last = None
    for base in bases:
        try:
            req = urllib.request.Request(_url(base, "manifest.json"),
                                         headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                data = json.loads(r.read().decode("utf-8"))
            if (isinstance(data, dict) and data.get("version")
                    and isinstance(data.get("files"), dict)):
                return data
            last = "清单格式无效"
        except Exception as e:
            last = e
    raise RuntimeError(f"无法获取远程清单（已尝试 {len(bases)} 个镜像）：{last}")


def local_version(root):
    try:
        with open(os.path.join(root, MANIFEST_LOCAL), encoding="utf-8") as f:
            return json.load(f).get("version", "未知")
    except Exception:
        return "未知（未找到安装清单）"


def local_created(root):
    try:
        with open(os.path.join(root, MANIFEST_LOCAL), encoding="utf-8") as f:
            return str(json.load(f).get("created", "")).strip()
    except Exception:
        return ""


def _ver_tuple(v):
    """版本字符串 'x.y.z[.build]' → 数字元组，用于大小比较。
    支持任意段数（发布版带构建时间戳，如 1.0.0.202609051215），
    无法解析返回 (0, 0, 0)。"""
    parts = re.findall(r"\d+", str(v).strip())
    if not parts:
        return (0, 0, 0)
    t = tuple(int(x) for x in parts)
    return t if len(t) >= 3 else t + (0,) * (3 - len(t))


def check(root):
    """对比远端 manifest 与本地文件，返回差异信息 dict。

    up_to_date 判定：文件全部一致（同版本热修复/构建时间戳差异不算更新），
    或本地版本已不早于远端（dist 分支陈旧时防止把新装「降级」回旧文件）。
    """
    remote = fetch_manifest()
    added, changed = [], []
    for rel, sha in remote["files"].items():
        p = os.path.join(root, *rel.split("/"))
        if not os.path.isfile(p):
            added.append(rel)
        elif file_sha256(p) != sha:
            changed.append(rel)
    cur = local_version(root)
    files_match = not added and not changed
    local_newer = _ver_tuple(cur) > _ver_tuple(remote["version"])
    return {
        "root": root,
        "local_version": cur,
        "remote_version": remote["version"],
        "local_created": local_created(root),
        "remote_created": str(remote.get("created", "")).strip(),
        "added": added,
        "changed": changed,
        "total": len(remote["files"]),
        "files_match": files_match,
        "local_newer": local_newer,
        # 文件全同 → 最新；文件不同但本地版本更新 → 不做降级更新
        "up_to_date": files_match or local_newer,
        "manifest": remote,
    }


def read_dl_threads(root, default=16):
    """下载线程数：与安装器/WebUI 共享 mineru.json 的 download-threads。"""
    try:
        with open(os.path.join(root, "mineru.json"), encoding="utf-8") as f:
            return min(64, max(4, int(json.load(f).get("download-threads", default))))
    except Exception:
        return default


def download(root, remote, rels, threads=16, on_event=None):
    """下载差异文件到 <root>/.update/ 暂存（fastdl 多源竞速 + sha256 校验）。"""
    try:
        import fastdl
    except ImportError:
        raise RuntimeError("缺少 fastdl 下载引擎")
    dl = fastdl.Downloader(
        source_bases(), _url, threads=threads, seg_size=8 << 20,
        race_min=8 << 20, stall=30.0, on_event=on_event or (lambda *a: None))
    stage = os.path.join(root, STAGE_DIR)
    for rel in rels:
        dst = os.path.join(stage, *rel.split("/"))
        os.makedirs(os.path.dirname(dst), exist_ok=True)   # fastdl 不建父目录
        dl.add(rel, dst, None, remote["files"][rel])
    return dl.run_with_retry(3)


def _running_tray_procs():
    """当前运行中的主程序进程数（0 = 未运行）。"""
    try:
        out = subprocess.run(
            ["tasklist", "/FI", f"IMAGENAME eq {TRAY_EXE}", "/NH"],
            capture_output=True, text=True, creationflags=_NO_WINDOW)
        return sum(1 for ln in (out.stdout or "").splitlines()
                   if TRAY_EXE in ln)
    except Exception:
        return 0


def _port_open(port):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(1.0)
        try:
            s.connect(("127.0.0.1", port))
            return True
        except OSError:
            return False


def _stop_webui(port=None):
    """停止后台 WebUI 服务（含其解析引擎子进程，释放 GPU 显存）。

    优先走 /api/shutdown 优雅停机；失败则按端口定位进程 pid 强杀其进程树。
    更新器进程自身是 python.exe，绝不按映像名清理，避免误杀自己。"""
    port = port or WEBUI_PORT
    try:
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/api/shutdown", data=b"", method="POST")
        urllib.request.urlopen(req, timeout=3).read()
        for _ in range(30):
            if not _port_open(port):
                return True
            time.sleep(0.2)
    except Exception:
        pass
    try:
        out = subprocess.run(
            ["netstat", "-ano"], capture_output=True, text=True,
            creationflags=_NO_WINDOW)
        for ln in (out.stdout or "").splitlines():
            parts = ln.split()
            if len(parts) >= 5 and parts[0] == "TCP" \
                    and parts[1].endswith(f":{port}") \
                    and parts[3] in ("LISTENING", "ESTABLISHED"):
                pid = parts[4]
                if pid.isdigit():
                    subprocess.run(["taskkill", "/PID", pid, "/T", "/F"],
                                   capture_output=True,
                                   creationflags=_NO_WINDOW)
                return True
    except Exception:
        pass
    return False


def _stop_tray(tray_pid=None):
    """终止托盘进程树（含其 WebUI 子进程），为换文件解除占用。

    关键：托盘「检查更新」拉起的更新器是托盘进程的子进程，若对托盘带 /T
    强杀，会把正在执行更新的本进程一并杀掉。因此：
      - 先优雅停 WebUI（失败则按端口强杀其进程树，不碰 python 映像名）；
      - 托盘由 PID 单独结束（不带 /T）；
      - 兜底按映像名结束残留托盘（不带 /T）。
    """
    if not _running_tray_procs() and not tray_pid:
        return True
    _stop_webui()
    if tray_pid and str(tray_pid).isdigit():
        subprocess.run(["taskkill", "/PID", str(tray_pid), "/F"],
                       capture_output=True, creationflags=_NO_WINDOW)
    else:
        subprocess.run(["taskkill", "/IM", TRAY_EXE, "/T", "/F"],
                       capture_output=True, creationflags=_NO_WINDOW)
    subprocess.run(["taskkill", "/IM", TRAY_EXE, "/F"],
                   capture_output=True, creationflags=_NO_WINDOW)
    for _ in range(50):                      # 等待句柄释放
        if not _running_tray_procs():
            return True
        time.sleep(0.2)
    return False


def apply_update(root, remote, rels, tray_pid=None):
    """停止托盘 → 暂存文件替换到安装目录 → 重写本地清单 → 返回是否成功。"""
    _stop_tray(tray_pid)
    stage = os.path.join(root, STAGE_DIR)
    for rel in rels:
        src = os.path.join(stage, *rel.split("/"))
        dst = os.path.join(root, *rel.split("/"))
        if not os.path.isfile(src):
            return False
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        os.replace(src, dst)
    with open(os.path.join(root, MANIFEST_LOCAL), "w", encoding="utf-8") as f:
        json.dump({"version": remote["version"],
                   "created": remote.get("created", ""),
                   "files": remote["files"]}, f, ensure_ascii=False, indent=1)
    return True


def restart_tray(root):
    """应用完成后重启新版本托盘。"""
    exe = os.path.join(root, TRAY_EXE)
    if os.path.isfile(exe):
        subprocess.Popen([exe], close_fds=True,
                         creationflags=subprocess.DETACHED_PROCESS | _NO_WINDOW)
        return True
    return False


def clean_stage(root):
    """清理下载暂存目录。"""
    import shutil
    stage = os.path.join(root, STAGE_DIR)
    try:
        shutil.rmtree(stage, ignore_errors=True)
    except Exception:
        pass


# ---------------- 图形界面 ----------------

_C_BG, _C_CARD, _C_FG = "#101623", "#182234", "#dce6f5"
_C_ACCENT, _C_DIM = "#4a8cff", "#7d8ca3"


def run_gui(root, tray_pid=None):
    import tkinter as tk
    from tkinter import ttk, messagebox

    win = tk.Tk()
    win.title("MinerU 更新")
    win.configure(bg=_C_BG)
    win.geometry("560x430")
    win.minsize(520, 400)

    state = {"diff": None, "remote": None, "tray_pid": tray_pid}

    head = tk.Frame(win, bg=_C_BG)
    head.pack(fill="x", padx=24, pady=(18, 6))
    tk.Label(head, text="远程修复 / 升级", bg=_C_BG, fg=_C_FG,
             font=("Microsoft YaHei UI", 16, "bold")).pack(anchor="w")
    info_var = tk.StringVar(value="正在连接远程仓库检查更新 …")
    tk.Label(win, textvariable=info_var, bg=_C_BG, fg=_C_DIM,
             font=("Microsoft YaHei UI", 10)).pack(anchor="w", padx=24)

    card = tk.Frame(win, bg=_C_CARD)
    card.pack(fill="both", expand=True, padx=24, pady=12)
    list_var = tk.StringVar(value="")
    tk.Label(card, textvariable=list_var, bg=_C_CARD, fg=_C_FG, justify="left",
             font=("Consolas", 9)).pack(anchor="w", padx=14, pady=12)

    prog = ttk.Progressbar(win, mode="determinate")
    prog.pack(fill="x", padx=24)
    status_var = tk.StringVar(value="")
    tk.Label(win, textvariable=status_var, bg=_C_BG, fg=_C_DIM,
             font=("Microsoft YaHei UI", 9)).pack(anchor="w", padx=24, pady=(4, 8))

    btns = tk.Frame(win, bg=_C_BG)
    btns.pack(fill="x", padx=24, pady=(0, 16))

    def _fmt(diff):
        lines = [f"本地版本：{diff['local_version']}    远端版本：{diff['remote_version']}"]
        if diff.get("local_created") or diff.get("remote_created"):
            lines.append(
                f"本地构建：{diff.get('local_created') or '未知'}"
                f"    远端构建：{diff.get('remote_created') or '未知'}")
        lines.append(f"远端共 {diff['total']} 个文件 · "
                     f"新增 {len(diff['added'])} · 更新 {len(diff['changed'])}")
        lines.append("")
        for rel in (diff["added"] + diff["changed"])[:12]:
            lines.append("  " + rel + ("  （缺失）" if rel in diff["added"] else ""))
        more = len(diff["added"]) + len(diff["changed"]) - 12
        if more > 0:
            lines.append(f"  … 等共 {more} 个文件未列出")
        return "\n".join(lines)

    def do_check():
        def worker():
            try:
                diff = check(root)
            except Exception as e:
                info_var.set("检查更新失败")
                list_var.set(str(e))
                return
            state["diff"], state["remote"] = diff, diff["manifest"]
            info_var.set(f"检查完成 —— {'已是最新版本' if diff['up_to_date'] else '发现可更新内容'}")
            if diff["up_to_date"]:
                if diff["local_newer"] and (diff["added"] or diff["changed"]):
                    list_var.set("本地版本已更新于远端（不执行降级更新）。")
                else:
                    list_var.set("本地文件与远端完全一致，无需修复或升级。")
                go_btn.config(state="disabled")
            else:
                list_var.set(_fmt(diff))
                go_btn.config(state="normal")
                # 发现可更新且程序正在运行 → 主动弹窗让用户选择处理方式
                win.after(0, _ask_running)
        threading.Thread(target=worker, daemon=True).start()

    go_btn = tk.Button(btns, text="下载并应用更新", state="disabled",
                       bg=_C_ACCENT, fg="white", relief="flat", padx=18, pady=6,
                       font=("Microsoft YaHei UI", 10, "bold"))
    go_btn.pack(side="right")

    waiting = {"on": False, "cancel": False}

    def do_update():
        diff, remote = state["diff"], state["remote"]
        if not diff or not remote:
            return
        if waiting["on"]:            # 等待模式下再次点击 → 取消等待
            waiting["cancel"] = True
            return
        if _ask_running():
            _start_download()

    def _ask_running():
        """程序运行中询问处理方式：是=立即关闭继续；否=等待退出（可取消）；取消=中止。
        返回 True 表示可继续下载（无进程或已关闭）。"""
        if waiting["on"]:
            return False
        n = _running_tray_procs()
        if not n:
            return True
        r = messagebox.askyesnocancel(
            "MinerU 正在运行",
            f"检测到 {n} 个 MinerU 进程正在运行。\n\n"
            "更新需要覆盖主程序文件，请先退出 MinerU。\n\n"
            "「是」立即关闭进程并继续更新\n"
            "「否」等待任务完成后再更新（等待期间可取消）\n"
            "「取消」中止本次更新")
        if r is None:
            return False
        if r is False:
            waiting["on"], waiting["cancel"] = True, False
            go_btn.config(text="取消等待", state="normal", command=do_update)
            _wait_tick()
            return False
        _stop_tray(state["tray_pid"])
        return True

    def _wait_tick():
        if waiting["cancel"]:
            waiting["on"] = False
            status_var.set("已取消等待，可再次点击下载并应用更新")
            go_btn.config(text="下载并应用更新", state="normal", command=do_update)
            return
        n = _running_tray_procs()
        if n == 0:
            waiting["on"] = False
            status_var.set("MinerU 已退出，可开始更新")
            go_btn.config(text="下载并应用更新", state="normal", command=do_update)
            return
        status_var.set(f"正在等待 MinerU 退出（当前 {n} 个进程）…")
        win.after(1000, _wait_tick)

    def _start_download():
        diff, remote = state["diff"], state["remote"]
        rels = diff["added"] + diff["changed"]
        go_btn.config(state="disabled", text="下载并应用更新")
        status_var.set("正在下载差异文件 …")
        # 下载线程只写计数器，主线程轮询刷新（tk 控件禁止跨线程直接操作）
        st = {"done": 0, "total": len(rels), "note": "", "result": None}

        def on_event(*a):
            ev = a[0]
            if ev == "done" and len(a) >= 3 and a[2]:
                st["done"] += 1
            elif ev == "switch" and len(a) >= 4:
                st["note"] = f"下载源切换：{a[2]} → {a[3]}"
            elif ev == "retry" and len(a) >= 3:
                st["note"] = f"第 {a[2]} 轮重试 {a[1]} 个文件"

        def worker():
            try:
                ok, fail = download(root, remote, rels,
                                    threads=read_dl_threads(root), on_event=on_event)
                st["result"] = fail or []
            except Exception as e:
                st["result"] = str(e)

        def tick():
            prog["maximum"] = st["total"]
            prog["value"] = st["done"]
            txt = f"已下载 {st['done']}/{st['total']} 个文件"
            if st["note"]:
                txt += f" · {st['note']}"
            status_var.set(txt)
            if st["result"] is None:
                win.after(200, tick)
                return
            if isinstance(st["result"], str):
                status_var.set(f"下载失败：{st['result']}")
                go_btn.config(state="normal")
                return
            if st["result"]:
                status_var.set(f"{len(st['result'])} 个文件下载失败，已中止（可重试）")
                go_btn.config(state="normal")
                return
            status_var.set("下载完成，正在应用更新（托盘将自动重启）…")
            win.after(300, _apply)

        def _apply():
            if not apply_update(root, remote, rels, tray_pid=state["tray_pid"]):
                status_var.set("应用更新失败：暂存文件缺失")
                go_btn.config(state="normal")
                return
            clean_stage(root)
            restart_tray(root)
            messagebox.showinfo("更新完成",
                               f"已更新到版本 {remote['version']}，托盘已重启。", parent=win)
            win.destroy()

        threading.Thread(target=worker, daemon=True).start()
        win.after(200, tick)

    go_btn.config(command=do_update)
    win.protocol("WM_DELETE_WINDOW", win.destroy)
    win.after(200, do_check)
    win.mainloop()


def main():
    ap = argparse.ArgumentParser(description="MinerU 远程修复/升级器")
    ap.add_argument("--root", required=True, help="安装根目录")
    ap.add_argument("--check", action="store_true", help="仅检查差异，输出 JSON")
    ap.add_argument("--gui", action="store_true", help="图形界面模式")
    ap.add_argument("--tray-pid", default=None, help="托盘进程 PID（应用前终止其进程树）")
    args = ap.parse_args()

    if args.gui:
        run_gui(args.root, tray_pid=args.tray_pid)
        return
    if args.check:
        try:
            print(json.dumps(check(args.root), ensure_ascii=False, indent=1))
        except Exception as e:
            print(json.dumps({"error": str(e)}, ensure_ascii=False))
            sys.exit(1)


if __name__ == "__main__":
    main()
