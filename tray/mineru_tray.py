# -*- coding: utf-8 -*-
"""MinerU 本地服务托盘启动器。

双击本程序(exe)：后台拉起 MinerU WebUI 服务 → 系统托盘显示图标常驻。
- 托盘【打开浏览器界面】：在默认浏览器打开 MinerU 可视化解析界面。
- 托盘【重启服务】：停止并重启后台 MinerU 服务。
- 托盘【退出并停止服务】：优雅停止后台服务（先释放解析引擎/GPU 显存再退出进程）。
"""
import os
import socket
import subprocess
import sys
import time
import urllib.request
import webbrowser
from pathlib import Path

# ---------- 配置 ----------
# 定位 MinerU 根目录：从 exe/脚本所在目录向上查找含 venv 与 webui/app.py 的目录。
# 打包后无论 exe 放在哪里（含 dist 内）都能正确定位项目根与 venv。
def _find_project_root():
    if getattr(sys, "frozen", False):
        start = Path(sys.executable).resolve().parent
    else:
        start = Path(__file__).resolve().parent
    for d in (start, *start.parents):
        if (d / "venv" / "Scripts" / "python.exe").exists() and (d / "webui" / "app.py").exists():
            return d
    return start

MINERU_ROOT = _find_project_root()
VENV_PY = os.path.join(MINERU_ROOT, "venv", "Scripts", "python.exe")
HOST = "127.0.0.1"
PORT = 7860  # Gradio 默认端口
SERVICE_LOG = os.path.join(MINERU_ROOT, "service.log")
# 模型缓存目录：重定向到 MinerU 根目录下，避免默认写 C:\Users\SL\.modelscope
MODEL_CACHE = os.path.join(MINERU_ROOT, "models_cache")
CONFIG_JSON = os.path.join(MINERU_ROOT, "mineru.json")

# 服务启动方式：优先反射 mineru-gradio，其次 mineru-api 的 gradio 兼容入口
SERVICE_ARGS = None  # 由 ensure_command() 探测后填充


def _cmd(name):
    """返回 venv Scripts 下命令的完整路径（exe 或 .exe）。"""
    for exe in (name, name + ".exe", name + ".exe.exe"):
        p = os.path.join(os.path.dirname(VENV_PY), exe)
        if os.path.exists(p):
            return p
    return os.path.join(os.path.dirname(VENV_PY), name)


# 解析后端：pipeline 通用稳定、模型小；如需 VLM 高精度可改 hybrid-engine 并下载 vlm 模型
BACKEND = os.environ.get("MINERU_BACKEND", "pipeline")


def resolve_service_args():
    """启动自研 MinerU WebUI（webui/app.py，内部自行拉起/停止解析引擎）。"""
    global SERVICE_ARGS
    webui_py = os.path.join(MINERU_ROOT, "webui", "app.py")
    SERVICE_ARGS = [VENV_PY, webui_py]


def is_up(port=None):
    port = port or PORT
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(1.0)
        try:
            s.connect((HOST, port))
            return True
        except OSError:
            return False


PROC = None  # 后台服务进程句柄


def ensure_service():
    """确保后台服务在运行；未运行则拉起常驻子进程。"""
    global PROC
    if is_up():
        return
    resolve_service_args()
    logf = open(SERVICE_LOG, "a", encoding="utf-8")
    env = dict(os.environ)
    env.setdefault("PYTHONUNBUFFERED", "1")  # 让子进程日志实时落盘，避免缓存导致日志为空
    env.setdefault("MINERU_BACKEND", BACKEND)
    env.setdefault("MINERU_MODEL_SOURCE", "local")  # 使用 mineru.json models-dir 本地模型，避免联网下载
    env.setdefault("MODELSCOPE_CACHE", MODEL_CACHE)
    env.setdefault("MODELSCOPE_MODELS_CACHE", MODEL_CACHE)
    env.setdefault("MODELSCOPE_HOME", os.path.join(MODEL_CACHE, "sdk"))
    env.setdefault("MINERU_TOOLS_CONFIG_JSON", CONFIG_JSON)
    PROC = subprocess.Popen(
        SERVICE_ARGS,
        env=env,
        stdout=logf, stderr=subprocess.STDOUT,
        creationflags=subprocess.CREATE_NO_WINDOW if hasattr(subprocess, "CREATE_NO_WINDOW") else 0,
    )
    # 等待就绪
    for _ in range(120):
        if is_up():
            return True
        time.sleep(1)
    return False


def stop_service():
    """停止后台服务进程（含内部引擎子进程，确保 GPU 显存释放）。

    优先优雅停机：调用 WebUI 的 /api/shutdown，其内部先停止解析引擎
    （释放显存与内存）再退出进程；若优雅停机失败或超时，再强杀进程树兜底。
    """
    global PROC
    if is_up():
        try:
            urllib.request.urlopen(
                f"http://{HOST}:{PORT}/api/shutdown", timeout=3
            ).read()
            for _ in range(30):       # 等待进程优雅退出
                if not is_up():
                    break
                time.sleep(0.2)
        except Exception:
            pass
    if PROC and PROC.poll() is None:
        # 兜底：强杀整个进程树（webui + 其拉起的 mineru-api 引擎子进程）
        subprocess.run(
            ["taskkill", "/PID", str(PROC.pid), "/T", "/F"],
            capture_output=True)
        try:
            PROC.wait(timeout=10)
        except Exception:
            pass
    # 兜底：按端口找并杀
    try:
        out = subprocess.check_output(
            f'netstat -ano | findstr :{PORT}', shell=True, text=True, errors="ignore")
        pids = {ln.split()[-1] for ln in out.splitlines() if ln.strip()}
        for pid in pids:
            if pid.isdigit() and pid != "0":
                subprocess.run(["taskkill", "/PID", pid, "/F"],
                               capture_output=True)
    except Exception:
        pass


def open_browser():
    webbrowser.open(f"http://{HOST}:{PORT}/")


# ---------- pystray 托盘 ----------
def build_tray():
    import pystray
    from PIL import Image, ImageDraw

    def make_icon_img():
        img = Image.new("RGBA", (64, 64), (24, 24, 24, 255))
        d = ImageDraw.Draw(img)
        d.rounded_rectangle([10, 10, 54, 54], radius=10, fill=(66, 133, 244, 255))
        d.text((20, 22), "Mn", fill=(255, 255, 255, 255))
        return img

    def do_open(icon, item):
        if not is_up():
            ensure_service()
        open_browser()

    def do_restart(icon, item):
        stop_service()
        time.sleep(1)
        ensure_service()

    def do_quit(icon, item):
        stop_service()   # 停止后台 WebUI，同时通过其 atexit 清理解析引擎子进程（释放 GPU）
        icon.stop()

    menu = pystray.Menu(
        pystray.MenuItem("打开浏览器界面", do_open, default=True),
        pystray.MenuItem("重启服务", do_restart),
        pystray.MenuItem("退出并停止服务", do_quit),
    )
    return pystray.Icon("MinerU", make_icon_img(), "MinerU 本地解析服务", menu)


def main():
    if not _acquire_singleton():
        print("MinerU 托盘已在运行（仅允许一个实例），本实例退出。",
              file=sys.stderr, flush=True)
        return
    ensure_service()
    tray = build_tray()
    tray.run()


_SINGLETON_MUTEX = None


def _acquire_singleton():
    """Windows 命名互斥体：仅允许一个托盘实例运行（双击多次只出一个托盘）。
    返回 True 表示本进程可继续；False 表示已有托盘在运行，应退出。"""
    global _SINGLETON_MUTEX
    if os.name != "nt":
        return True
    try:
        import ctypes
        handle = ctypes.windll.kernel32.CreateMutexW(None, False, "Local\\MinerU_Tray")
        if ctypes.windll.kernel32.GetLastError() == 183:   # ERROR_ALREADY_EXISTS
            return False
        _SINGLETON_MUTEX = handle
        return True
    except Exception:
        return True


if __name__ == "__main__":
    main()