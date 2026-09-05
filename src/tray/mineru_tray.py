# -*- coding: utf-8 -*-
"""MinerU 本地服务托盘启动器。

双击本程序(exe)：后台拉起 MinerU WebUI 服务 → 服务就绪后自动打开浏览器网页 → 系统托盘显示图标常驻。
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
# 定位安装根目录：从 exe/脚本所在目录向上查找含 runtime/venv 的目录。
# 打包后 exe 位于 <安装根>/MinerU文档解析/ 内，WebUI 字节码在 _MEIPASS 中，
# 源码不落盘；源码运行时同项目根。
def _find_project_root():
    if getattr(sys, "frozen", False):
        start = Path(sys.executable).resolve().parent
    else:
        start = Path(__file__).resolve().parent
    for d in (start, *start.parents):
        if (d / "runtime" / "venv" / "Scripts" / "python.exe").exists():
            return d
    return start

MINERU_ROOT = _find_project_root()
RUNTIME = os.path.join(MINERU_ROOT, "runtime")
SRC = os.path.join(MINERU_ROOT, "src")
VENV_PY = os.path.join(RUNTIME, "venv", "Scripts", "python.exe")
HOST = "127.0.0.1"
PORT = 7860  # Gradio 默认端口
# 模型缓存目录：重定向到 runtime/models_cache，避免默认写 C:\Users\SL\.modelscope
MODEL_CACHE = os.path.join(RUNTIME, "models_cache")
CONFIG_JSON = os.path.join(MINERU_ROOT, "mineru.json")

# ---------------- 运行日志（每次运行清空之前的日志） ----------------
# 日志统一落安装根 logs/（与 WebUI/安装器/诊断日志一致），runtime/_data 仅存运行数据。
# 文件名 = 软件名称_日期_时间，每次启动新建一个文件。
APP_LOG_NAME = "MinerU"


def _logs_dir():
    d = os.environ.get("MINERU_LOG_DIR")
    if d:
        return Path(d)
    return Path(MINERU_ROOT) / "logs"


def _clean_old_logs(logs_dir):
    """每次运行清空之前日志：删除 logs 目录下本软件历史运行日志。"""
    try:
        for p in logs_dir.glob(f"{APP_LOG_NAME}_*.log"):
            try:
                p.unlink()
            except OSError:
                pass
    except OSError:
        pass


def _fresh_log_path():
    logs_dir = _logs_dir()
    logs_dir.mkdir(parents=True, exist_ok=True)
    _clean_old_logs(logs_dir)
    return logs_dir / f"{APP_LOG_NAME}_{time.strftime('%Y-%m-%d_%H-%M-%S')}.log"

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
    """启动自研 MinerU WebUI（内部自行拉起/停止解析引擎）。
    打包后运行 _MEIPASS/webui/app.pyc（编译字节码，源码不落盘）；
    源码运行时直接跑 src/webui/app.py。"""
    global SERVICE_ARGS
    if getattr(sys, "frozen", False):
        webui_entry = os.path.join(sys._MEIPASS, "webui", "app.pyc")
    else:
        webui_entry = os.path.join(SRC, "webui", "app.py")
    SERVICE_ARGS = [VENV_PY, webui_entry]


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
    # 每次启动新建运行日志（logs/MinerU_日期_时间.log），并清空之前的日志
    log_path = _fresh_log_path()
    logf = open(log_path, "w", encoding="utf-8")
    logf.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} | INFO  | MinerU 托盘服务启动（后台拉起 WebUI）\n")
    logf.flush()
    env = dict(os.environ)
    env["MINERU_ROOT"] = str(MINERU_ROOT)         # 告知 WebUI 项目根（定位 runtime/ 与 mineru.json）
    env["MINERU_LOG_DIR"] = str(log_path.parent)  # 告知 WebUI 日志目录（exe 所在目录下的 logs）
    env["MINERU_LOG_FILE"] = str(log_path)        # 主业务日志；引擎原始日志由 WebUI 派生（*_engine.log）
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
        cwd=str(MINERU_ROOT),  # 与 webui 内 chdir 一致：引擎子进程 cwd 稳定为安装根
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
            req = urllib.request.Request(
                f"http://{HOST}:{PORT}/api/shutdown", data=b"", method="POST")
            urllib.request.urlopen(req, timeout=3).read()
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


def launch_updater():
    """拉起更新器 GUI（独立 venv python 进程）：
    检查远端 manifest → 差异下载 → 终止本托盘进程树 → 换文件 → 重启新版本。"""
    if getattr(sys, "frozen", False):
        updater_entry = os.path.join(sys._MEIPASS, "updater.pyc")
    else:
        updater_entry = os.path.join(MINERU_ROOT, "src", "installer", "updater.py")
    if not os.path.isfile(updater_entry) or not os.path.isfile(VENV_PY):
        return False
    subprocess.Popen(
        [VENV_PY, updater_entry, "--gui", "--root", str(MINERU_ROOT),
         "--tray-pid", str(os.getpid())],
        creationflags=subprocess.CREATE_NO_WINDOW if hasattr(subprocess, "CREATE_NO_WINDOW") else 0,
    )
    return True


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

    def do_update(icon, item):
        launch_updater()

    menu = pystray.Menu(
        pystray.MenuItem("打开浏览器界面", do_open, default=True),
        pystray.MenuItem("重启服务", do_restart),
        pystray.MenuItem("检查更新", do_update),
        pystray.MenuItem("退出并停止服务", do_quit),
    )
    return pystray.Icon("MinerU", make_icon_img(), "MinerU 本地解析服务", menu)


def main():
    if not _acquire_singleton():
        print("MinerU 托盘已在运行（仅允许一个实例），本实例退出。",
              file=sys.stderr, flush=True)
        return
    if ensure_service():
        if not os.environ.get("MINERU_NO_BROWSER"):
            open_browser()  # 一键启动：后台服务就绪后自动打开网页
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