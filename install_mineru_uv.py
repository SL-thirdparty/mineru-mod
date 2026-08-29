# -*- coding: utf-8 -*-
"""MinerU 安装器 v2（新建脚本，保留旧版 install_new_machine.py 不动）。

特点：
  1. 安装引擎：uv 优先（本机 C:\\AddPath\\uv.exe 或 PATH 中的 uv），uv 失败自动回退 pip 官方。
  2. 下载源：用户 --mirror 首选 -> 清华 -> 阿里 -> 中科大 -> 官方，逐源回退。
  3. 显示：阶段状态行 + 逐行进度 + 颜色（Windows VT）+ 汇总；全程写 install_mineru_uv.log。
  4. venv 残骸自愈：目录存在但不完整则先删除再重建。

用法：
    python install_mineru_uv.py [--mirror https://pypi.tuna.tsinghua.edu.cn/simple]
                                 [--local-torch-dir <dir>]  # 目录内预置 aria2 等下载的 torch/torchvision .whl，离线优先安装 CUDA torch
"""
import ctypes
import json
import os
import shutil
import subprocess
import sys

_ANSI = False


def enable_vt():
    """Windows 10+ 启用虚拟终端序列；失败则回退纯文本（不加颜色）。"""
    if sys.platform != "win32":
        return True
    try:
        k = ctypes.windll.kernel32
        h = k.GetStdHandle(-11)
        mode = ctypes.c_uint32()
        if k.GetConsoleMode(h, ctypes.byref(mode)):
            ENABLE_VIRTUAL_TERMINAL_PROCESSING = 0x0004
            k.SetConsoleMode(h, mode.value | ENABLE_VIRTUAL_TERMINAL_PROCESSING)
            return True
    except Exception:
        pass
    return False


_ANSI = enable_vt()
LOG = None


def c(s, code):
    return f"\x1b[{code}m{s}\x1b[0m" if _ANSI else s


GREEN, RED, YELLOW, CYAN, BOLD = "32", "31", "33", "36", "1"


def green(s): return c(s, GREEN)
def red(s): return c(s, RED)
def yellow(s): return c(s, YELLOW)
def cyan(s): return c(s, CYAN)


def emit(s=""):
    print(s, flush=True)
    try:
        LOG.write(s + "\n")
        LOG.flush()
    except Exception:
        pass


def run_tee(cmd):
    """执行命令，逐行实时打印到终端并写入日志；返回退出码。"""
    emit(f"$ {' '.join(cmd)}")
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                         text=True, errors="replace")
    for line in p.stdout:
        emit(line.rstrip())
    return p.wait()


def run_visible(cmd, tee):
    """统一走管道捕获：uv/pip 的 stdout+stderr 实时回显到终端并写入日志。
    取舍：虽然 TTY 下 uv/pip 的原生动画进度条因此退化为逐行文本（每行仍含包名/大小），
    但失败原因不再丢失——实体机上无法装通 torch 时，报错能完整落盘供回传定位，比进度条更关键。
    保留 tee 形参以兼容所有调用点，实际恒为管道模式。"""
    emit(f"$ {' '.join(cmd)}")
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                         text=True, errors="replace")
    for line in p.stdout:
        emit(line.rstrip())
    return p.wait()


TTY = sys.stdout.isatty() and os.environ.get("MINERU_LOG_TEE") != "1"


# ==================================================================
# 诊断日志：整机状态采集，供用户搬到目标机执行后回传（实体机可能有独立显卡）
# 每次运行动态生成 sysdiag_YYYYmmdd_HHMMSS.log，追加到 MinerU 根目录
# ==================================================================

def _ts():
    import datetime
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _find_nvidia_smi():
    """定位 nvidia-smi 可执行文件，返回去重后的候选路径列表。

    NVIDIA 的 nvidia-smi 通常不在 System32，而在 DriverStore 或
    C:/Program Files/NVIDIA Corporation/NVSMI 目录下；PATH 里的 nvidia-smi 也常时有时无
    （曾实测同一台有独显的机器两次探测分别为命中/ NONE）。故做多路鲁棒查找：
    PATH 裸名 → 常见固定路径 → where 解析 → DriverStore 通配扫描。
    """
    cands = ["nvidia-smi"]
    for p in (r"C:\Windows\System32\nvidia-smi.exe",
              r"C:\Windows\SysWOW64\nvidia-smi.exe",
              r"C:\Program Files\NVIDIA Corporation\NVSMI\nvidia-smi.exe",
              r"C:\Program Files\NVIDIA Corporation\NVSMI\nvidia-smi.bat"):
        cands.append(p)
    try:
        r = subprocess.run(["where", "nvidia-smi"], capture_output=True, text=True, timeout=30)
        if r.returncode == 0:
            for ln in r.stdout.splitlines():
                ln = ln.strip()
                if ln:
                    cands.append(ln)
    except Exception:
        pass
    try:
        root = r"C:\Windows\System32\DriverStore\FileRepository"
        if os.path.isdir(root):
            for d in os.listdir(root):
                p = os.path.join(root, d, "nvidia-smi.exe")
                if os.path.isfile(p):
                    cands.append(p)
    except Exception:
        pass
    out, seen = [], set()
    for c in cands:
        if c not in seen:
            seen.add(c)
            out.append(c)
    return out


def detect_gpu():
    """探测 NVIDIA 独立显卡；失败/不存在返回 None。

    返回 dict(name, vram_gb, driver_cuda) —— vram_gb 为整数 GB，driver_cuda 为"驱动支持
    的 CUDA 版本号"（如 12.6，用于 pick_cuda 精确选通道）。
    多路 nvidia-smi 定位探测，超时兜底，绝不抛异常。
    """
    # nvidia-smi 的 driver_version 是"驱动版本号"（例 581.95），不是 CUDA 版本；必须另查 cuda_version。
    # 但 cuda_version 字段部分 nvidia-smi 版本不支持，会导致整个探测返回非 0 → 有独显被误判为无。
    # 故容错：先试含 cuda_version 的查询，失败立即退回基础字段查询，保证只要 nvidia-smi 可执行就能识别 GPU。
    for probe in _find_nvidia_smi():
        for qfields in (["name", "memory.total", "driver_version", "cuda_version"],
                        ["name", "memory.total", "driver_version"]):
            try:
                r = subprocess.run(
                    [probe] + ["--query-gpu=" + ",".join(qfields), "--format=csv,noheader"],
                    capture_output=True, text=True, timeout=30)
            except Exception:
                continue
            if r.returncode != 0 or not r.stdout.strip():
                continue
            rows = [ln.strip() for ln in r.stdout.splitlines() if ln.strip()]
            name = mem = dver = cver = None
            total_gb = 0
            for row in rows:
                parts = [p.strip() for p in row.split(",")]
                if len(parts) >= 1:
                    name = parts[0]
                if len(parts) >= 2:
                    mem = parts[1]
                if len(parts) >= 3:
                    dver = parts[2]
                if len(parts) >= 4:
                    cver = parts[3]
                try:
                    if mem:
                        total_gb = int(float(mem.replace("MiB", "").replace("GiB", "").strip()) / 1024)
                except Exception:
                    total_gb = 0
                break
            # driver_cuda 存"驱动支持的 CUDA 版本号"（如 12.6）；取不到则置空，由 pick_cuda 选安全默认通道
            return {"name": name, "vram_gb": total_gb,
                    "driver_cuda": (cver or "").strip(), "driver_ver": dver or ""}
        # 该 probe 两套字段均不行 → 试下一个候选
    return None


def _wmi_nvidia_gpu():
    """用 WMI（Win32_VideoController）兜底判断是否存在 NVIDIA GPU，不依赖 nvidia-smi。

    当 nvidia-smi 定位失败时，据此区分"真无独显"与"有独显但 nvidia-smi 不可用"，
    避免有独显的机器被静默误判为 CPU。返回 NVIDIA 系显卡名列表；失败返回 []。"""
    names = []
    try:
        r = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "Get-CimInstance Win32_VideoController | ForEach-Object { $_.Name }"],
            capture_output=True, text=True, timeout=60)
        lines = [ln.strip() for ln in r.stdout.splitlines() if ln.strip()]
        for ln in lines:
            low = ln.lower()
            if any(k in low for k in ("nvidia", "geforce", "rtx ", "gtx ", "quadro", "tesla")):
                names.append(ln)
    except Exception:
        pass
    return names


# CUDA torch wheel 索引：国内镜像优先 -> 官方回退。cuX 由驱动 CUDA 版本决定后填充
PYTORCH_INDEXES = [
    "https://mirrors.aliyun.com/pytorch-wheels/{cu}",
    "https://mirrors.ustc.edu.cn/pytorch-wheels/{cu}",
    "https://download.pytorch.org/whl/{cu}",
]


def pick_cuda(driver_cuda):
    """由驱动 CUDA 版本挑选其能承载的最高 torch CUDA wheel 通道。

    PyTorch wheel 自带对应 CUDA runtime，运行时只需驱动 CUDA>=wheel 内置版本。
    故取满足  wheelCUDA <= 驱动CUDA  的最高通道。候选按现代版本降序。
    """
    try:
        drv = float(str(driver_cuda).split()[0]) if str(driver_cuda).strip() else 0.0
    except Exception:
        drv = 0.0
    if drv <= 0:
        # 拿不到驱动支持的 CUDA 版本：现代 NVIDIA 卡（Ada/Blackwell）+ torch 2.13 的 CUDA 通道
        # 主要在 cu124/cu128，UV 依赖注入的 torch 2.13 需 cu128 才匹配（cu124 上限约 torch 2.8，
        # 强选 cu124 会降级或装不上）。故默认 cu128，驱动向下兼容、镜像与官方均可能托管。
        return "cu128"
    # 现代 PyTorch 提供 wheel 的通道，从高到低。
    # 去除 cu129：该通道 aliyun/ustc 镜像不托管（常致三源连败），且 4070 这类卡用不到。
    for tag in ("cu128", "cu126", "cu124", "cu121", "cu118"):
        try:
            wheel_cuda = int(tag[2:-1]) + int(tag[-1]) / 10.0  # cu126 -> 12 + 0.6 = 12.6
        except Exception:
            continue
        if wheel_cuda <= drv + 1e-6:
            return tag
    return "cu118"


class Diagnostics:
    """全量诊断采集，写入 sysdiag_<时间戳>.log；安装各步骤调用 self.step() 写水线。

    用户把该日志文件回传后即可还原目标机真实环境（GPU/torch/后端/模型/关键 env）。
    """

    def __init__(self, root):
        import datetime
        self.path = os.path.join(root, "sysdiag_%s.log" % datetime.datetime.now().strftime("%Y%m%d_%H%M%S"))
        self.lines = []
        self.header(root)

    def w(self, msg):
        self.lines.append(msg)

    def header(self, root):
        self.w("=" * 62)
        self.w(" MinerU 环境诊断快照  %s" % _ts())
        self.w("=" * 62)
        self.w("工作目录: " + root)

    def section(self, title):
        self.w("")
        self.w("---- %s ----" % title)

    def step(self, step_no, text):
        self.w("")
        self.w("[STEP %s] %s" % (step_no, text))

    def write(self):
        import io
        # 追加系统信息
        self.collect_system()
        self.collect_env()
        try:
            with open(self.path, "w", encoding="utf-8") as f:
                f.write("\n".join(self.lines) + "\n")
        except Exception as e:
            emit(red("       [诊断] 写入失败: " + str(e)))
        return self.path

    def collect_system(self):
        import platform
        self.section("系统")
        self.w("platform: %s %s %s" % (platform.system(), platform.release(), platform.platform()))
        self.w("py: " + sys.version.splitlines()[0])
        gpu = detect_gpu()
        self.w("nvidia-smi GPU: " + (json.dumps(gpu, ensure_ascii=False) if gpu else "NONE (未检测到 NVIDIA 独立显卡)"))

    def collect_env(self):
        self.section("关键环境变量（cv 关键项）")
        keys = ["MINERU_DEVICE_MODE", "MINERU_VIRTUAL_VRAM_SIZE", "MINERU_MODEL_SOURCE",
                "MINERU_TOOLS_CONFIG_JSON", "MINERU_BACKEND", "MINERU_TABLE_ENABLE",
                "MINERU_FORCE_CPU", "MODELSCOPE_CACHE", "MODELSCOPE_MODELS_CACHE"]
        for k in keys:
            self.w("%s = %s" % (k, os.environ.get(k)))


def _log_tail(path, n=18):
    """读取安装日志末尾 n 行并剥离 ANSI 色码，用于把 pip 失败的真实报错追加进 sysdiag，
    省去失败时还要单独回传 install_mineru_uv.log 的往返。"""
    if not path:
        return []
    try:
        import re
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            lines = fh.read().splitlines()
        ansi = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
        return [ansi.sub("", ln) for ln in lines[-n:] if ln.strip()]
    except Exception:
        return []


PIP_MIRRORS = [
    "https://pypi.tuna.tsinghua.edu.cn/simple",
    "https://mirrors.aliyun.com/pypi/simple/",
    "https://pypi.mirrors.ustc.edu.cn/simple",
    None,  # 官方 pypi.org，最终兜底
]


def find_uv():
    """按序探测 uv：安装器同目录 -> MinerU 根 -> 本机 C:\\AddPath -> PATH。"""
    here = os.path.dirname(os.path.abspath(__file__))
    cands = [os.path.join(here, "uv.exe"), os.path.join(here, "uv"),
             r"C:\AddPath\uv.exe"]
    for p_ in os.environ.get("PATH", "").split(os.pathsep):
        for e in (".exe", ".bat", ""):
            f = os.path.join(p_, "uv" + e)
            if os.path.exists(f):
                cands.append(f)
    seen = set()
    for f in cands:
        if f in seen:
            continue
        seen.add(f)
        if os.path.exists(f):
            return f
    return None


class Installer:
    def __init__(self, root, mirror, diag=None, local_torch_dir=None):
        self.root = root
        self.mirror = mirror
        self.diag = diag
        self.local_torch_dir = local_torch_dir and os.path.abspath(local_torch_dir)
        self.venv = os.path.join(root, "venv")
        self.vpy = os.path.join(self.venv, "Scripts", "python.exe")
        self.uv = find_uv()

    def mirror_sources(self):
        if self.mirror:
            return [self.mirror] + [s for s in PIP_MIRRORS if s != self.mirror]
        return list(PIP_MIRRORS)

    # ---- 步骤 1：校验模型目录 ----
    def check_model(self):
        cache = os.path.join(self.root, "models_cache")
        kit = os.path.join(cache, "models", "OpenDataLab--PDF-Extract-Kit-1.0", "snapshots", "master")
        # VLM（hybrid 后端）模型未预置 → 若误用 hybrid 会现场联网下载 1.2B 模型导致慢
        vlm_dirs = [
            os.path.join(cache, "models", "OpenDataLab--MinerU2.5-Pro-2605-1.2B"),
        ]
        vlm_ok = any(os.path.isdir(d) for d in vlm_dirs)
        if os.path.isdir(kit):
            emit(green("[1/4] 模型目录就位（pipeline 权重已检测到）"))
            self._diag("模型: pipeline 权重就位; VLM(hybrid)权重预置=" + str(vlm_ok))
        elif os.path.isdir(cache):
            emit(yellow("[1/4] 警告：models_cache 存在但未找到完整模型，识别将不可用"))
            self._diag("模型: models_cache 存在但缺 pipeline 完整权重")
        else:
            emit(yellow("[1/4] 警告：未检测到 models_cache，请把模型文件夹拷入后再运行"))
            self._diag("模型: 未检测到 models_cache")
        if not vlm_ok:
            emit(yellow("       [提示] 未预置 VLM 模型：务必将后端固定为 pipeline，避免现场下载 1.2B 模型"))

    def _diag(self, msg):
        if self.diag:
            self.diag.w(msg)

    # ---- 步骤 2：创建/自愈虚拟环境 ----
    def ensure_venv(self):
        if os.path.exists(self.venv):
            if os.path.exists(self.vpy) and os.path.isdir(os.path.join(self.venv, "Lib", "site-packages")):
                emit(green("[2/4] venv 已存在且完整，跳过创建"))
                return True
            emit(yellow("       ↻ 检测到残存 venv（不完整），移除后重建"))
            shutil.rmtree(self.venv, ignore_errors=True)
        emit(cyan("[2/4] 创建虚拟环境 venv ..."))
        if self.uv:
            if run_tee([self.uv, "venv", self.venv]) == 0:
                return True
            emit(yellow("       ↻ uv 建 venv 失败，回退 python -m venv"))
        return run_tee([sys.executable, "-m", "venv", self.venv]) == 0

    # ---- 步骤 3：安装依赖（uv 优先 -> pip 兜底，镜像多源回退）----
    def install_deps(self):
        specs = ["mineru[core]", "pywin32", "pystray"]
        if self.uv:
            if self._install_with_uv(specs):
                emit(green("       ✓ 依赖安装完成（uv 引擎）"))
                return True
            emit(yellow("       ↻ uv 源均失败，回退 pip（镜像多源 → 官方兜底）"))
        return self._install_with_pip(specs)

    def _install_with_uv(self, specs):
        for src in self.mirror_sources():
            emit(f"       [uv 下载源] {src or '官方 pypi.org'}")
            # TTY：继承父终端，uv 原生进度条显示包名/大小/百分比/速度
            # 非 TTY：--no-progress 禁用动画，逐行文本便于写入日志
            cmd = [self.uv, "pip", "install", "--python", self.vpy]
            if not TTY:
                cmd += ["--no-progress", "--color", "never"]
            if src:
                cmd += ["--index-url", src]
            cmd += specs
            if run_visible(cmd, not TTY) == 0:
                return True
        return False

    def _install_with_pip(self, specs):
        for src in self.mirror_sources():
            emit(f"       [pip 下载源] {src or '官方 pypi.org'}")
            # TTY 下 pip 原生进度条自带大小/百分比；非 TTY 自动退化为逐行 "Downloading 包名 (大小)"
            cmd = [self.vpy, "-m", "pip", "install", "--prefer-binary",
                   "--timeout", "60", "--retries", "3"]
            if src:
                cmd += ["-i", src]
            cmd += specs
            if run_visible(cmd, not TTY) == 0:
                return True
        emit(red("       错误：所有安装源均失败"))
        return False

    def torch_offline_install(self):
        """从本地目录离线安装 CUDA torch（--local-torch-dir 提供，通常为 aria2 预下载的 wheel）。
        用 --no-index --find-links 只查该目录，杜绝联网；返回 (ok, info2)。"""
        d = self.local_torch_dir
        if not d or not os.path.isdir(d):
            return False, None
        wheels = [f for f in os.listdir(d)
                  if f.lower().endswith(".whl") and (f.lower().startswith("torch") or f.lower().startswith("torchvision"))]
        if not wheels:
            self.diag.w("本地离线目录存在但未发现 torch/torchvision wheel: " + d)
            emit(yellow("       [离线] 目录存在但未发现 torch/torchvision .whl：%s" % d))
            return False, None
        emit(cyan("       [torch 离线] 检测到本地 wheel %d 个，跳过联网直接从目录安装" % len(wheels)))
        self.diag.w("离线安装目录: %s ; wheels=%s" % (d, ", ".join(sorted(wheels))))
        rc = run_visible([self.vpy, "-m", "pip", "install",
                          "--no-index", "--find-links", d,
                          "--force-reinstall", "--no-deps",
                          "torch", "torchvision"], not TTY)
        info2 = self.torch_info()
        avail = bool(info2 and info2.get("avail"))
        self.diag.w("  → 离线 rc=%s avail=%s torch=%s" % (rc, avail,
                    (json.dumps(info2, ensure_ascii=False) if info2 else "读取失败")))
        if avail:
            emit(green("       [torch 离线] CUDA torch 安装成功：%s device(s)" % info2["dev"]))
            return True, info2
        emit(yellow("       [torch 离线] 离线安装未生效（avail=False），将回退联网尝试"))
        return False, None

    # ---- 设备/GPU 决策：装 CUDA torch 还是保持 CPU（核心，决定是否真用 GPU）----
    def torch_info(self):
        code = "import torch,json;d={'ver':torch.__version__,'cuda_built':(torch.version.cuda),'avail':torch.cuda.is_available(),'dev':torch.cuda.device_count() if torch.cuda.is_available() else 0};print(json.dumps(d))"
        try:
            r = subprocess.run([self.vpy, "-c", code], capture_output=True, text=True, timeout=90)
            out = r.stdout.strip()
            if r.returncode == 0 and out:
                return json.loads(out.splitlines()[-1])
        except Exception:
            pass
        return None

    def onnx_probe_ensure(self):
        """探测 onnxruntime 是否支持 CUDA；有 GPU 存在且无 CUDA provider 则重装 onnxruntime-gpu（国内镜像优先）。
        探测前先 import torch + onnxruntime.preload_dlls()，让 onnxruntime 复用 torch 打进的 CUDA12.8/cuDNN9 DLL，
        CUDAExecutionProvider 才会被正确列出。"""
        code = ("import torch,onnxruntime as ort,json\n"
                "try:\n ort.preload_dlls()\nexcept Exception:\n pass\n"
                "print(json.dumps({'ver': ort.__version__,'cuda':'CUDAExecutionProvider' in ort.get_available_providers()}))")
        try:
            r = subprocess.run([self.vpy, "-c", code], capture_output=True, text=True, timeout=90)
            out = r.stdout.strip()
            if r.returncode == 0 and out:
                import json
                d = json.loads(out.splitlines()[-1])
            else:
                d = None
        except Exception:
            d = None
        if d and d.get("cuda"):
            self.diag.w("onnxruntime: ver=%s has_cuda_provider=%s" % (d.get("ver"), d.get("cuda")))
            emit(green("       [onnxruntime] CUDA provider 已就绪 → 所有 pipeline 模型可加速"))
            return True
        # 无 CUDA provider 或探测失败（未安装）→ 尝试安装 onnxruntime-gpu。
        # 本函数仅在 finalize_torch 的有 GPU 分支被调用，故缺失/失败时装上即可。
        if d:
            self.diag.w("onnxruntime: ver=%s has_cuda_provider=%s" % (d.get("ver"), d.get("cuda")))
            emit(cyan("       [onnxruntime] 当前 %s 无 CUDA provider → 正在安装 onnxruntime-gpu..." % d.get("ver")))
        else:
            self.diag.w("onnxruntime: 探测失败（可能未安装）→ 尝试安装 onnxruntime-gpu")
            emit(cyan("       [onnxruntime] 探测失败（可能未安装）→ 正在安装 onnxruntime-gpu..."))
        # 避免与 uv 注入的 CPU 版 onnxruntime 冲突：先卸载两者再装 gpu，确保 import 取到 GPU provider
        subprocess.run([self.vpy, "-m", "pip", "uninstall", "-y", "onnxruntime", "onnxruntime-gpu"],
                       capture_output=True, text=True, timeout=120)
        emit("       [onnxruntime] 已卸载 CPU 版 onnxruntime → 装 gpu 版")
        self.diag.w("已卸载 onnxruntime/onnxruntime-gpu（防双包冲突）")
        # 关键：版本匹配 torch cu128 → onnxruntime-gpu < 1.27（1.27+默认 CUDA 13，不兼容；1.21-1.26 对应 CUDA 12.8）
        spec = "onnxruntime-gpu<1.27"
        ok = False
        for src in self.mirror_sources():
            emit("       [源] %s (%s)" % ((src or "官方 pypi.org"), spec))
            self.diag.w("尝试 onnxruntime-gpu from %s 限定 %s" % ((src or "官方"), spec))
            cmd = [self.vpy, "-m", "pip", "install", "--prefer-binary",
                   "--timeout", "180", "--retries", "3"]
            if src:
                cmd += ["-i", src]
            cmd.append(spec)
            rc = run_visible(cmd, not TTY)
            if rc == 0:
                # 安装完重探一次（同样先 preload torch 的 CUDA/cuDNN DLL）
                code2 = ("import torch,onnxruntime as ort,json\n"
                        "try:\n ort.preload_dlls()\nexcept Exception:\n pass\n"
                        "print(json.dumps({'ver': ort.__version__,'cuda':'CUDAExecutionProvider' in ort.get_available_providers()}))")
                r = subprocess.run([self.vpy, "-c", code2], capture_output=True, text=True, timeout=90)
                out2 = r.stdout.strip()
                if r.returncode == 0 and out2:
                    d2 = json.loads(out2.splitlines()[-1])
                    if d2.get("cuda"):
                        ok = True
                        break
        self.diag.w("onnxruntime-gpu 结论: " + ("成功" if ok else "失败（请回传本日志排查）"))
        if ok:
            emit(green("       [onnxruntime] CUDA provider 就绪 → pipeline 模型可走 GPU 加速"))
        else:
            emit(yellow("       [警告] onnxruntime-gpu 安装失败 → pipeline 仍为 CPU 推理"))
        return ok

    def finalize_torch(self):
        """依赖装完后：探测 GPU 并做出 torch+onnxruntime 决策。
        关键事实：pipeline 后端绝大多数模型走 ONNX Runtime，不是 torch；要 GPU 必须同时满足：
          1. onnxruntime-gpu 已安装且 CUDA provider 可用
          2. torch.cuda.is_available() 用来决策 MINERU_DEVICE_MODE
        - 无 NVIDIA 独显：保持 CPU torch，走 pipeline（轻量）后端。
        - 有独显：先确保 CUDA torch，再确保 onnxruntime-gpu 可用。
        返回 (ok, info)。"""
        self.diag.section("Torch / GPU 决策")
        gpu = detect_gpu()
        self.diag.w("nvidia-smi GPU: " + (json.dumps(gpu, ensure_ascii=False) if gpu else "NONE"))
        info = self.torch_info()
        self.diag.w("torch now: " + json.dumps(info, ensure_ascii=False) if info else "torch now: 读取失败/未安装")

        def _avail(i):
            return bool(i and i.get("avail"))

        if not gpu:
            wmi = _wmi_nvidia_gpu()
            if wmi:
                self.diag.w("WMI 检测到 NVIDIA GPU: " + "; ".join(wmi)
                            + "（nvidia-smi 未定位到 → 可能被误判为无独显）")
                emit(yellow("       [提示] WMI 检测到存在 NVIDIA GPU（%s），但 nvidia-smi 定位失败；"
                            "请装好 NVIDIA 驱动或将 nvidia-smi 所在目录加入 PATH 后重跑本安装器" % "; ".join(wmi)))
            else:
                self.diag.w("WMI NVIDIA GPU: 无（真无独显或已禁用独显）")
            emit(green("       [GPU] 未检测到 NVIDIA 独立显卡 → 保持 CPU 版 torch，走 pipeline 后端（轻量、不下载 VLM 模型）"))
            self.diag.w("决策: 探测不到 nvidia-smi → 固定 pipeline + CPU torch；onnxruntime 保持 CPU 版")
            return True, info
        # 有独显但 torch 还是 CPU → 重装 CUDA torch
        cu = pick_cuda(gpu.get("driver_cuda", ""))
        if not _avail(info):
            emit(cyan("       [GPU] 检测到 %s（%sGB，驱动CUDA %s）→ 重装 CUDA torch（%s）..." % (
                gpu["name"], gpu.get("vram_gb", "?"), gpu.get("driver_cuda", "?"), cu)))
            self.diag.w("决策: 有独显但 torch=CPU → 重装 %s" % cu)
            if LOG:
                LOG.flush()
            # 依赖走 uv 安装时，venv 内可能没有 pip（实测出现 No module named pip，三个源全 rc=1）；
            # torch/onnxruntime-gpu 重装依赖 pip，故先用 stdlib 自带 ensurepip 补回。
            _ensure = subprocess.run([self.vpy, "-m", "ensurepip", "--upgrade"],
                                     capture_output=True, text=True, timeout=180)
            if _ensure.returncode == 0:
                emit("       [pip] venv 已补齐 pip（ensurepip）")
                self.diag.w("ensurepip: ok (venv pip 已补齐)")
            else:
                emit(yellow("       [警告] ensurepip 失败: " + (_ensure.stderr.strip()[:200] or _ensure.stdout.strip()[:200])))
                self.diag.w("ensurepip: 失败 " + (_ensure.stderr.strip()[:200] or ""))
            ok_torch = False
            # 优先用本地预下载的 wheel（aria2 多线程拉起、免联网），失败再走镜像→官方联网
            if self.local_torch_dir:
                ok_torch, info2 = self.torch_offline_install()
                if not ok_torch:
                    info2 = self.torch_info()
            # 联网回退：国内镜像 × 选定通道 → 国内镜像 × 可选兜底通道 → 官方 × 选定通道
            if not ok_torch:
                mirror_tpls = PYTORCH_INDEXES[:2]
                official_tpl = PYTORCH_INDEXES[2]
                fallback_cu = cu if cu in ("cu118", "cu121", "cu124") else "cu124"
                idxs, seen = [], set()
                for ch in (cu, fallback_cu):
                    for t in mirror_tpls:
                        idx = t.format(cu=ch)
                        if idx not in seen:
                            seen.add(idx)
                            idxs.append(idx)
                if official_tpl.format(cu=cu) not in seen:
                    idxs.append(official_tpl.format(cu=cu))
                for idx in idxs:
                    emit("       [torch 源] " + idx)
                    self.diag.w("尝试 CUDA torch 源: " + idx)
                    rc = run_visible([self.vpy, "-m", "pip", "install", "--prefer-binary",
                                  "--force-reinstall", "--no-deps",
                                  "--timeout", "180", "--retries", "3",
                                  "--index-url", idx,
                                  "torch", "torchvision"], not TTY)
                    info2 = self.torch_info()
                    avail = bool(info2 and info2.get("avail"))
                    self.diag.w("  → rc=%s avail=%s torch=%s" % (rc, avail,
                                (json.dumps(info2, ensure_ascii=False) if info2 else "读取失败")))
                    if rc != 0:
                        tail = _log_tail(LOG.name if LOG else None)
                        if tail:
                            self.diag.w("  → pip 失败日志尾行:")
                            for ln in tail:
                                self.diag.w("     " + ln)
                    if avail:
                        ok_torch = True
                        break
            self.diag.w("CUDA torch 重装结论: " + ("成功" if ok_torch else "失败（请回传本日志排查）"))
            if not ok_torch:
                emit(yellow("       [警告] CUDA torch 安装失败，将回退 CPU + pipeline；请回传本日志排查"))
            else:
                emit(green("       [GPU] CUDA torch 就绪：%s device(s)" % info2["dev"]))
            info = info2 if ok_torch else info
        else:
            ok_torch = True
            emit(green("       [GPU] torch 已可调用 CUDA：%s device(s)" % info["dev"]))
        # 关键第二步：确保 onnxruntime 有 CUDA provider（这才是 pipeline 真加速）
        ok_onnx = self.onnx_probe_ensure()
        overall = ok_torch and ok_onnx
        return overall, info

    # ---- 步骤 4：生成 mineru.json ----
    def write_config(self):
        emit(cyan("[4/4] 生成 mineru.json 指向模型目录 ..."))
        cache = os.path.join(self.root, "models_cache")
        kit = os.path.join(cache, "models", "OpenDataLab--PDF-Extract-Kit-1.0", "snapshots", "master")
        base = kit if os.path.isdir(kit) else cache
        cfg = {"models-dir": {"pipeline": base.replace("\\", "/")}, "model-source": "modelscope"}
        path = os.path.join(self.root, "mineru.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2)
        self.diag.w("mineru.json: pipeline models-dir=" + base.replace("\\", "/") + " ; model-source=modelscope")
        emit(green("       已写入: " + path))


def main():
    root = os.path.dirname(os.path.abspath(__file__))
    mirror = None
    local_torch_dir = None
    if "--mirror" in sys.argv:
        i = sys.argv.index("--mirror")
        if i + 1 < len(sys.argv):
            mirror = sys.argv[i + 1]
    if "--local-torch-dir" in sys.argv:
        i = sys.argv.index("--local-torch-dir")
        if i + 1 < len(sys.argv):
            local_torch_dir = sys.argv[i + 1]
    # 未显式指定时，自动探测同目录下的约定离线目录 torch_wheels，双击 bat 也能命中
    if not local_torch_dir:
        default_cand = os.path.join(root, "torch_wheels")
        if os.path.isdir(default_cand) and any(
                f.lower().endswith(".whl") and (f.lower().startswith("torch") or f.lower().startswith("torchvision"))
                for f in os.listdir(default_cand)):
            local_torch_dir = default_cand
    if local_torch_dir:
        emit(cyan("  [离线 torch] 已启用本地 wheel 目录: " + local_torch_dir))

    global LOG
    LOG = open(os.path.join(root, "install_mineru_uv.log"), "w", encoding="utf-8")
    diag = Diagnostics(root)
    diag_result = ""
    try:
        emit("=" * 60)
        emit(cyan("MinerU 安装器 v2（uv 优先 + pip 兜底 / GPU 自适配）"))
        emit("=" * 60)
        emit("  安装目录: " + root)
        ins = Installer(root, mirror, diag, local_torch_dir=local_torch_dir)
        emit("  安装引擎: " + (cyan(os.path.basename(ins.uv)) if ins.uv else yellow("pip（未检测到 uv）")))
        emit("  PyPI    : " + (mirror or "国内镜像→官方回退"))
        gpu = detect_gpu()
        if gpu:
            emit("  独立显卡: " + cyan("%s（%sGB，驱动CUDA %s）" % (gpu["name"], gpu["vram_gb"], gpu["driver_cuda"])))
        else:
            emit("  独立显卡: " + yellow("未检测到（虚拟/集显环境 → 固定 pipeline 后端）"))
        emit("")

        diag.step("01", "模型与 GPU 探测")
        ins.check_model()
        diag.step("02", "创建/自愈虚拟环境")
        if not ins.ensure_venv():
            emit(red("创建虚拟环境失败。"))
            return 1
        diag.step("03", "安装 mineru[core] 等依赖")
        if not ins.install_deps():
            emit(red("依赖安装失败。"))
            return 1
        diag.step("04", "GPU 决策：CUDA torch / CPU pipeline")
        ok_torch, tinfo = ins.finalize_torch()
        diag.step("05", "生成 mineru.json")
        ins.write_config()

        # 后端固定结论写入诊断（供实体机回传核对）
        dev_mode = "cuda" if (tinfo and tinfo.get("avail")) else "cpu"
        diag.section("安装结论 / 使用约定")
        diag.w("后端: 固定使用 pipeline（轻量）。避免 hybrid-engine（需现场下载 1.2B VLM 模型，无GPU极慢）。")
        diag.w("设备模式: MINERU_DEVICE_MODE=" + dev_mode)
        diag.w("启用GPU: %s" % ("是（CUDA torch 就绪）" if (tinfo and tinfo.get("avail")) else "否（CPU pipeline）"))

        emit("")
        emit(green("=" * 60))
        emit(green("安装完成！"))
        emit("  固定后端: " + cyan("pipeline"))
        emit("  设备模式: " + cyan("cuda" if (tinfo and tinfo.get("avail")) else "cpu"))
        emit("  使用方式: 双击 MinerU_Tray\\MinerU_Tray.exe （请在页面保持 pipeline 后端，勿切 hybrid）")
        emit("=" * 60)
        diag_result = diag.write()
        emit(cyan("  诊断日志(回传用): " + diag_result))
        emit(cyan("  将该文件回传，实体机异常即可据此定位。"))
        return 0
    except Exception as e:  # noqa: BLE001
        import traceback
        emit(red(f"错误: {e}"))
        emit(traceback.format_exc())
        try:
            diag_result = diag.write()
            emit(cyan("  诊断日志(回传用): " + diag_result))
        except Exception:
            pass
        return 1
    finally:
        try:
            LOG.close()
        except Exception:
            pass


if __name__ == "__main__":
    sys.exit(main())