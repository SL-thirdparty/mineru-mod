# -*- coding: utf-8 -*-
"""MinerU 自研独立 WebUI —— 后端任务调度 + REST API。

复用官方 mineru 作为解析引擎（内部 ReusableLocalAPIServer 拉起独立矿池进程），
本进程只负责：任务队列管理、并发调度、文件级进度、结果下载、资源释放。
不修改任何官方包。
"""
import asyncio
import atexit
import gc
import json
import mimetypes
import os
import re
import subprocess
import sys
import threading
import time
import uuid
import urllib.request
from copy import deepcopy
from pathlib import Path

import httpx
import uvicorn
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from mineru.cli import api_client
from mineru.cli.api_client import (
    ReusableLocalAPIServer,
    UploadAsset,
    build_parse_request_form_data,
)
from mineru.cli.common import normalize_task_stem

# ---------------- 基础路径 ----------------
ROOT = Path(__file__).resolve().parent.parent          # MinerU 根目录
DATA = ROOT / "_data"
UPLOADS = DATA / "uploads"
OUTPUTS = DATA / "outputs"
STATIC = Path(__file__).resolve().parent / "static"
CONFIG_JSON = ROOT / "mineru.json"

UPLOADS.mkdir(parents=True, exist_ok=True)
OUTPUTS.mkdir(parents=True, exist_ok=True)

# ---------------- 配置（可用环境变量覆盖；运行时可经 /api/config 调整并持久化） ----------------
WEBUI_HOST = os.environ.get("MINERU_WEBUI_HOST", "127.0.0.1")
WEBUI_PORT = int(os.environ.get("MINERU_WEBUI_PORT", "7860"))
MAX_WORKERS = 1                          # 引擎串行解析，单 worker 逐个处理
DEFAULT_BACKEND = os.environ.get("MINERU_BACKEND", "pipeline")

CONFIG_PATH = DATA / "config.json"
DEFAULT_OUTPUT_ROOT = os.environ.get("MINERU_WEBUI_OUTPUT", r"D:\MinerU-Output")
DEFAULT_PARAMS = {   # 解析参数默认值（可经设置界面调整并持久化，作为文档解析页的初始值）
    "lang": "ch",                  # 语言：ch/en/ja/ko/auto
    "backend": DEFAULT_BACKEND,    # 引擎：pipeline / hybrid-engine
    "effort": "medium",            # 推理强度：high/medium/low/none
    "max_pages": 1000,             # 最大页数
    "formula": True,               # 公式识别
    "table": True,                 # 表格识别
    "image_analysis": True,        # 图像分析
    "is_ocr": False,               # 强制 OCR
}
DEFAULT_CONFIG = {
    "output_dir": DEFAULT_OUTPUT_ROOT,                     # 解析结果输出根目录（每批一个子目录）
    "idle_release_seconds": int(os.environ.get("MINERU_WEBUI_IDLE_RELEASE", "30")),
    "batch_close_seconds": int(os.environ.get("MINERU_WEBUI_BATCH_CLOSE", "60")),  # 队列清空闲置多久后关闭批次
    "default_params": dict(DEFAULT_PARAMS),                # 解析参数默认值（文档解析页初始参数）
    "formats": {                                                  # 导出内容（对应官方 return_* 开关）
        "md": True,               # Markdown
        "middle_json": True,      # 中间过程 JSON
        "model_output": True,     # 模型原始输出
        "content_list": True,     # 内容列表
        "images": True,           # 图片
        "original_file": True,    # 原始文件
    },
}

FORMAT_TO_RETURN = {   # 前端格式键 -> build_parse_request_form_data 参数
    "md": "return_md",
    "middle_json": "return_middle_json",
    "model_output": "return_model_output",
    "content_list": "return_content_list",
    "images": "return_images",
    "original_file": "return_original_file",
}


class ConfigStore:
    """运行配置：内存 + 持久化到 _data/config.json。"""

    def __init__(self, path, defaults):
        self._path = Path(path)
        self._defaults = defaults
        self._lock = threading.Lock()
        self._data = self._load()

    def _load(self):
        data = deepcopy(self._defaults)
        try:
            if self._path.exists():
                saved = json.loads(self._path.read_text(encoding="utf-8"))
                for k, v in saved.items():
                    data[k] = v
                f = data.get("formats") or {}
                base = self._defaults["formats"]
                for k in base:
                    f.setdefault(k, base[k])
                data["formats"] = f
                dp = data.get("default_params") or {}
                base_dp = self._defaults["default_params"]
                for k in base_dp:
                    dp.setdefault(k, base_dp[k])
                data["default_params"] = dp
        except Exception:
            pass
        return data

    def get(self):
        with self._lock:
            return deepcopy(self._data)

    def update(self, patch):
        with self._lock:
            for k in ("output_dir", "idle_release_seconds", "batch_close_seconds"):
                if k in patch and patch[k] is not None:
                    self._data[k] = patch[k]
            if isinstance(patch.get("formats"), dict):
                f = dict(self._defaults["formats"])
                f.update(patch["formats"])
                self._data["formats"] = f
            if isinstance(patch.get("default_params"), dict):
                dp = dict(self._defaults["default_params"])
                dp.update({k: v for k, v in patch["default_params"].items() if k in self._defaults["default_params"]})
                self._data["default_params"] = dp
            self._save()

    def _save(self):
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._path.write_text(
                json.dumps(self._data, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception:
            pass


CONFIG = ConfigStore(CONFIG_PATH, DEFAULT_CONFIG)

app = FastAPI(title="MinerU WebUI")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ==================================================================
# 任务状态定义
# ==================================================================
ST_PREPARE = "preparing"        # 准备请求
ST_CHECK = "checking"           # 检查服务（等待引擎就绪）
ST_SUBMIT = "submitting"        # 提交任务
ST_QUEUE = "queued"             # 排队
ST_PROCESS = "processing"       # 解析中
ST_DOWNLOAD = "downloading"     # 下载结果
ST_OUTPUT = "organizing"        # 整理输出
ST_DONE = "done"
ST_ERROR = "error"
ST_CANCELED = "canceled"

# 任务生命周期阶段（与官方前端 8 步状态面板对齐），供前端阶段指示器展示
STEP_SEQUENCE = [
    ST_PREPARE,
    ST_CHECK,
    ST_SUBMIT,
    ST_QUEUE,
    ST_PROCESS,
    ST_DOWNLOAD,
    ST_OUTPUT,
]
TERMINAL_STEPS = (ST_DONE, ST_ERROR, ST_CANCELED)

STEP_LABELS = {
    ST_PREPARE: "准备请求",
    ST_CHECK: "检查服务",
    ST_SUBMIT: "提交任务",
    ST_QUEUE: "排队",
    ST_PROCESS: "解析中",
    ST_DOWNLOAD: "下载结果",
    ST_OUTPUT: "整理输出",
}


# ==================================================================
# 解析选项（表单字段构造）
# ==================================================================
def build_options(lang, backend, formula, table, image_analysis, is_ocr, effort, max_pages, formats=None):
    """构造解析请求表单。formats 为导出内容开关（None 时取全局 CONFIG）。"""
    f = dict(DEFAULT_CONFIG["formats"])
    if formats:
        f.update(formats)
    return_params = {}
    for key, ret_name in FORMAT_TO_RETURN.items():
        return_params[ret_name] = bool(f.get(key, True))

    form_data = build_parse_request_form_data(
        lang_list=[lang],
        backend=backend,
        parse_method="auto",
        formula_enable=bool(formula),
        table_enable=bool(table),
        server_url=None,
        start_page_id=0,
        end_page_id=max(0, max_pages - 1),
        effort=effort,
        image_analysis=bool(image_analysis),
        return_md=return_params["return_md"],
        return_middle_json=return_params["return_middle_json"],
        return_model_output=return_params["return_model_output"],
        return_content_list=return_params["return_content_list"],
        return_images=return_params["return_images"],
        response_format_zip=True,
        return_original_file=return_params["return_original_file"],
        client_side_output_generation=False,
    )
    return form_data


# ==================================================================
# 引擎管理（内部 mineru-api 进程；停止它=释放 GPU 显存）
# ==================================================================
def _ensure_modelscope_home():
    """ModelScope SDK 默认缓存到 ~/.modelscope；受限账户/沙箱下不可写时，
    回退到项目内 _data/modelscope，避免引擎因无法建目录而启动失败。"""
    home = Path.home() / ".modelscope"
    try:
        home.mkdir(parents=True, exist_ok=True)
        (home / "hub").mkdir(parents=True, exist_ok=True)
        return
    except Exception:
        alt = DATA / "modelscope"
        try:
            alt.mkdir(parents=True, exist_ok=True)
            (alt / "hub").mkdir(parents=True, exist_ok=True)
        except Exception:
            return
        os.environ["MODELSCOPE_HOME"] = str(alt)
        os.environ["MODELSCOPE_CACHE"] = str(alt / "hub")


class Engine:
    def __init__(self):
        self._manager = ReusableLocalAPIServer()
        self._server = None
        self._lock = threading.Lock()
        self._manual_stopped = False
        self._running = False
        self._starting = False

    def ensure_started(self):
        """确保内部解析引擎在运行且健康就绪，返回 (base_url, server)。
        冷启动（加载模型）较久，期间 _starting=True，供前端展示“引擎启动中”。
        新拉起的引擎需等 /health 返回 healthy 后才视为“启动完成”。"""
        if not self._running:
            self._starting = True
        try:
            with self._lock:
                self._manual_stopped = False
                # 让引擎读取项目内 mineru.json（models-dir 指向本地模型缓存），
                # 并强制 local 来源，避免每次冷启动联网下载模型
                os.environ.setdefault("MINERU_TOOLS_CONFIG_JSON", str(CONFIG_JSON))
                os.environ.setdefault("MINERU_MODEL_SOURCE", "local")
                # 引擎不支持真正的并发推理：hybrid 后端 VLM 模型进程内共享单例且
                # 经线程池推理，多文件并发时线程同时调用同一模型的 generate()，
                # 内部状态互相踩踏导致张量形状错乱而崩溃（RuntimeError: expanded size）。
                # 强制引擎串行处理请求，前端并发数只决定“同时排队提交的文件数”。
                os.environ["MINERU_API_MAX_CONCURRENT_REQUESTS"] = "1"
                _ensure_modelscope_home()
                server, started = self._manager.ensure_started()
                self._server = server
                # 首次拉起时等待模型加载完成，期间保持“启动中”
                if started:
                    self._wait_engine_healthy(server, server.base_url)
                self._running = True
                return server.base_url, server
        finally:
            self._starting = False

    @staticmethod
    def _wait_engine_healthy(server, base_url, timeout=300.0):
        """同步等待引擎 /health 返回 healthy（模型加载完成）。
        在 worker 线程内调用，期间 _starting=True 保持“启动中”展示。"""
        import httpx as _httpx
        deadline = time.time() + timeout
        last_err = ""
        while time.time() < deadline:
            proc = getattr(server, "process", None)
            if proc is not None and getattr(proc, "poll", lambda: None)() is not None:
                raise RuntimeError(f"本地解析引擎进程异常退出：{last_err or '无错误信息'}")
            try:
                with _httpx.Client(timeout=2.0) as c:
                    payload = c.get(f"{base_url}/health").json()
                if payload.get("status") == "healthy":
                    return
                last_err = json.dumps(payload, ensure_ascii=False)[:200]
            except Exception as e:  # noqa: BLE001
                last_err = str(e)[:200]
            time.sleep(1)
        raise RuntimeError(f"等待本地解析引擎就绪超时：{last_err or '无错误信息'}")

    def is_running(self):
        return self._running

    def state(self, busy=0):
        """引擎运行状态（供前端展示）：
        - "stopped"  未启动 / 已释放
        - "starting" 冷启动中（加载模型）
        - "running"  进程在跑，且有任务在忙
        - "idle"     进程在跑，当前空闲
        busy: 当前未完成任务数（>0 视为运行中）。"""
        if self._starting:
            return "starting"
        if not self._running:
            return "stopped"
        return "running" if busy else "idle"

    def stop(self):
        """停止内部解析引擎，释放 GPU 显存与内存。"""
        self._starting = False
        with self._lock:
            if self._server is not None:
                try:
                    self._manager.stop()
                finally:
                    self._server = None
                    self._running = False
                    _force_gc()
            self._manual_stopped = True

    def should_release(self):
        return self._manual_stopped or self._server is None


ENGINE = Engine()


def _proc_alive(proc):
    """进程是否仍在运行（兼容 subprocess.Popen 与 multiprocessing.Process）。"""
    if proc is None:
        return False
    poll = getattr(proc, "poll", None)
    if callable(poll):
        return poll() is None
    return getattr(proc, "is_alive", lambda: False)()


def _stop_engine_safely():
    """停止解析引擎，并确认其进程已退出（含进程树），避免 GPU 显存悬挂。
    返回 True 表示引擎已不再运行。"""
    server = getattr(ENGINE, "_server", None)
    try:
        ENGINE.stop()
    except Exception:
        pass
    proc = getattr(server, "process", None) if server else None
    if _proc_alive(proc):
        pid = getattr(proc, "pid", None)
        if pid:
            try:
                subprocess.run(
                    ["taskkill", "/PID", str(pid), "/T", "/F"],
                    capture_output=True, timeout=15)
            except Exception:
                pass
        try:
            wait = getattr(proc, "wait", None)
            if callable(wait):
                wait(timeout=10)
        except Exception:
            pass
    return not _proc_alive(proc)


# 进程正常退出（如 Ctrl+C / uvicorn 停止）时的兜底：确保解析引擎子进程被终止。
# 注意 os._exit 不会触发 atexit，故 /api/shutdown 内主动调用 _stop_engine_safely()。
atexit.register(lambda: _stop_engine_safely())


def engine_state():
    """统一计算引擎状态：running/idle 依据是否还有未完成任务区分。"""
    return ENGINE.state(busy=STORE.remaining())


# ==================================================================
# 引擎内部阶段跟踪：引擎子进程继承本进程 stdout，经重定向落盘到
# _data/logs/engine.log；尾随该文件解析 MinerU pipeline 阶段标记，
# 供前端展示“当前处理到哪个阶段”（版面/OCR/公式/表格/页面等）。
# 解析失败时优雅降级为“解析中”，不影响主流程。
# ==================================================================
ENGINE_STAGE_MARKERS = [
    # (正则, 阶段文案)；{b}/{t} 会替换为批量进度
    (r"DocAnalysis init done!", "模型加载完成"),
    (r"model init cost", "模型初始化"),
    (r"Pipeline processing-window multi-file run", "开始解析"),
    (r"Pipeline processing window batch\s*(\d+)/(\d+)", "页面批量分析 {b}/{t}"),
    (r"Table-ocr rec", "表格识别"),
    (r"Table-ocr det", "表格检测"),
    (r"OCR-det", "OCR 文字检测"),
    (r"Processing pages", "页面处理中"),
    (r"Compression successful", "结果压缩"),
]


class EngineStageTracker:
    def __init__(self):
        self._lock = threading.Lock()
        self._stage = ""
        self._log_path = DATA / "logs" / "engine.log"

    def start(self):
        try:
            self._log_path.parent.mkdir(parents=True, exist_ok=True)
            threading.Thread(target=self._tail_loop, daemon=True).start()
        except Exception:
            pass

    def current(self):
        with self._lock:
            return self._stage or ""

    def _set(self, stage):
        with self._lock:
            self._stage = stage

    def _tail_loop(self):
        """从文件末尾开始增量读取，解析最近出现的阶段标记。"""
        size = 0
        try:
            if self._log_path.exists():
                size = self._log_path.stat().st_size
        except Exception:
            pass
        while True:
            try:
                cur = self._log_path.stat().st_size
            except Exception:
                cur = -1
            if cur > size:
                try:
                    with open(self._log_path, "rb") as f:
                        f.seek(size)
                        chunk = f.read(cur - size)
                    size = cur
                    self._parse(chunk)
                except Exception:
                    pass
            elif cur >= 0 and cur < size:   # 日志被截断/轮转
                size = 0
            time.sleep(0.8)

    def _parse(self, chunk):
        text = chunk.decode("utf-8", errors="replace")
        matched = None
        for line in text.splitlines():
            for pattern, label in ENGINE_STAGE_MARKERS:
                m = re.search(pattern, line)
                if m:
                    matched = (label, m)
        if matched:
            label, m = matched
            if m and m.groups():
                try:
                    label = label.replace("{b}", m.group(1)).replace("{t}", m.group(2))
                except Exception:
                    pass
            self._set(label)


ENGINE_STAGE = EngineStageTracker()


def _redirect_engine_log():
    """将本进程 stdout/stderr（引擎子进程继承）重定向到 _data/logs/engine.log，
    供 EngineStageTracker 解析引擎处理阶段。失败时静默跳过。"""
    try:
        log_dir = DATA / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        path = log_dir / "engine.log"
        fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
        os.dup2(fd, 1)
        os.dup2(fd, 2)
        if fd > 2:
            os.close(fd)
    except Exception:
        pass


def _gradio_upload_name(file_path):
    """与官方 build_gradio_upload_name 等价：规范化上传文件名（去特殊字符）。
    从 mineru.cli.common.normalize_task_stem 复用官方规范化规则。"""
    p = Path(file_path)
    return f"{normalize_task_stem(p.stem)}{p.suffix}"


def _force_gc():
    try:
        import torch
        if torch.cuda.is_available():
            torch.cuda.empty_cache()
    except Exception:
        pass
    gc.collect()


def _safe_dir_name(name):
    """目录名安全化：剔除非法字符，避免空名。"""
    s = re.sub(r'[\\/:*?"<>|\r\n\t]', "_", str(name)).strip().rstrip(".")
    return s[:120] or "file"


# ==================================================================
# 批次管理（D:\MinerU-Output\<日期_时间[_批次名]>\<文件目录>\）
# 批次 = 输出根目录下的一个文件夹；文件夹名 = 时间戳 [+ "_" + 用户批次名]。
# 批次数目跨会话持久：下拉列表来自扫描输出根目录，本会话创建/打开过的进注册表。
# 同一批（含执行中追加的文件）共享一个批次目录；
# 队列清空并闲置 batch_close_seconds 后自动关闭批次，下一批重新建目录。
# ==================================================================
BATCH_STAMP_RE = re.compile(r"^(\d{4}-\d{2}-\d{2}_\d{6})(.*)$")


def parse_batch_dirname(folder):
    """解析批次文件夹名 -> (stamp, user_name)；非本应用命名格式时 name 取整个文件夹名。"""
    m = BATCH_STAMP_RE.match(folder or "")
    if m:
        stamp, rest = m.group(1), m.group(2)
        return stamp, (rest[1:] if rest.startswith("_") else "")
    return "", (folder or "")


def batch_folder_name(stamp, name):
    """由时间戳与用户批次名构造文件夹名；无名批次只用时间戳。"""
    n = _safe_dir_name(name) if name else ""
    return stamp if not n else f"{stamp}_{n}"


def _batch_has_active_tasks(batch_id):
    return any(
        t.get("batch_id") == batch_id and t.get("status") not in TERMINAL_STEPS
        for t in STORE.all()
    )


def _patch_task_dirs(old_id, old_dir, new_dir, new_id):
    """重命名批次后，同步修正已结束任务的输出路径引用，保证“打开目录/预览”仍可用。"""
    old_s, new_s = str(old_dir), str(new_dir)
    for t in STORE.all():
        if t.get("batch_id") != old_id:
            continue
        t["batch_id"] = new_id
        r = t.get("result")
        if not r:
            continue
        for key in ("dir", "md_path", "json_path"):
            v = r.get(key)
            if v and str(v).startswith(old_s):
                r[key] = str(new_dir) + str(v)[len(old_s):]


class BatchManager:
    """批次注册表：批次 = 输出根目录下的文件夹（文件夹名即 id，跨会话稳定）。"""

    MAX_HISTORY = 200
    SCAN_LIMIT = 60          # 列表最多返回最近 N 个批次

    def __init__(self):
        self._lock = threading.Lock()
        self._batches = {}        # folder_name -> batch dict（本会话创建/打开过的）
        self._order = []          # folder_name，按创建先后
        self._current_id = None
        self._fallback_warning = ""   # 配置的输出根目录不可写时的回退提示

    def _roots(self):
        roots = [Path(CONFIG.get().get("output_dir") or OUTPUTS), OUTPUTS]
        seen, out = set(), []
        for r in roots:
            try:
                key = str(r.resolve())
            except Exception:
                key = str(r)
            if key not in seen:
                seen.add(key)
                out.append(r)
        return out

    def _make(self, name=""):
        """新建批次文件夹并返回批次 dict；根目录不可写时回退到项目内 OUTPUTS。"""
        roots = self._roots()
        primary = roots[0]
        for _ in range(8):
            stamp = time.strftime("%Y-%m-%d_%H%M%S")
            folder = batch_folder_name(stamp, name)
            # 先确认该文件夹名在所有根下都不存在（同名批次防覆盖），同名则等下一秒重试
            if any((r / folder).exists() for r in roots):
                time.sleep(0.05)
                continue
            for root in roots:
                try:
                    root.mkdir(parents=True, exist_ok=True)
                    bdir = root / folder
                    bdir.mkdir(parents=True, exist_ok=True)
                except Exception:
                    continue
                if root == primary:
                    self._fallback_warning = ""
                else:
                    self._fallback_warning = (
                        f"输出根目录不可写（{primary}），已回退到 {root}，"
                        f"请检查目录权限或在设置中修改输出目录"
                    )
                return {
                    "id": folder,
                    "stamp": stamp,
                    "name": name or "",
                    "dir": str(bdir),
                    "created_at": time.time(),
                    "closed": False,
                    "in_session": True,
                }
        raise PermissionError(f"无法创建任何输出目录（{primary} / {OUTPUTS}）")

    def _register(self, batch):
        self._batches[batch["id"]] = batch
        if batch["id"] not in self._order:
            self._order.append(batch["id"])
        self._current_id = batch["id"]
        while len(self._order) > self.MAX_HISTORY:
            old = self._order.pop(0)
            self._batches.pop(old, None)

    def _find_locked(self, batch_id):
        """按 id（文件夹名）在注册表或磁盘上定位批次；调用方须持有 _lock。"""
        if batch_id in self._batches:
            return self._batches[batch_id]
        for root in self._roots():
            d = root / batch_id
            if d.is_dir():
                stamp, uname = parse_batch_dirname(batch_id)
                return {
                    "id": batch_id,
                    "stamp": stamp,
                    "name": uname,
                    "dir": str(d.resolve()),
                    "created_at": d.stat().st_mtime,
                    "closed": False,
                    "in_session": False,
                }
        return None

    def create(self, name=""):
        """强制新建批次并设为当前批次，返回其深拷贝。name 为可选批次名（用于文件夹后缀）。"""
        with self._lock:
            batch = self._make(name=name)
            self._register(batch)
            return deepcopy(batch)

    def open(self, batch_id=None, name=""):
        """返回目标批次：优先按 batch_id 复用（本会话或历史文件夹，已关闭则重新打开并设为当前），
        否则返回当前开放批次；都不满足则新建（name 用于新批次文件夹命名）。"""
        with self._lock:
            if batch_id is not None:
                b = self._find_locked(batch_id)
                if b:
                    b["closed"] = False
                    self._register(b)
                    return deepcopy(b)
            cur = self._batches.get(self._current_id) if self._current_id else None
            if cur and not cur["closed"]:
                return deepcopy(cur)
            batch = self._make(name=name)
            self._register(batch)
            return deepcopy(batch)

    def resolve(self, batch_id=None):
        """按 batch_id 定位批次（存在即返回，不要求为当前；不影响当前批次），
        未指定或不存在时走 open()。供任务处理期按提交时锁定的批次建目录。"""
        if batch_id is not None:
            with self._lock:
                b = self._find_locked(batch_id)
                if b:
                    return deepcopy(b)
        return self.open()

    def current(self):
        with self._lock:
            b = self._batches.get(self._current_id) if self._current_id else None
            return deepcopy(b) if b else None

    def current_id(self):
        with self._lock:
            return self._current_id

    def is_open(self):
        with self._lock:
            b = self._batches.get(self._current_id) if self._current_id else None
            return bool(b) and not b["closed"]

    def fallback_warning(self):
        with self._lock:
            return self._fallback_warning

    def file_dir(self, stem, batch_id=None):
        """为单个文件在指定/当前批次下创建独立子目录，返回其绝对路径。"""
        b = self.resolve(batch_id)
        d = Path(b["dir"]) / _safe_dir_name(stem)
        d.mkdir(parents=True, exist_ok=True)
        return d

    def close(self):
        with self._lock:
            cur = self._batches.get(self._current_id) if self._current_id else None
            if cur:
                cur["closed"] = True

    def scan(self):
        """扫描输出根目录（含回退目录）下全部批次文件夹，合并本会话注册表状态，
        按创建时间倒序返回最近 SCAN_LIMIT 个。"""
        out = {}
        for root in self._roots():
            try:
                entries = [e for e in root.iterdir() if e.is_dir()]
            except Exception:
                entries = []
            for d in entries:
                folder = d.name
                try:
                    ctime = d.stat().st_mtime
                except Exception:
                    ctime = 0
                stamp, uname = parse_batch_dirname(folder)
                out[folder] = {
                    "id": folder,
                    "stamp": stamp,
                    "name": uname,
                    "dir": str(d.resolve()),
                    "created_at": ctime,
                    "closed": True,
                    "in_session": False,
                }
        with self._lock:
            for folder, b in self._batches.items():
                if folder in out:
                    out[folder].update({
                        "closed": b["closed"],
                        "created_at": b["created_at"],
                        "name": b["name"],
                        "in_session": True,
                    })
        items = sorted(out.values(), key=lambda b: b["created_at"], reverse=True)
        return items[: self.SCAN_LIMIT]

    def rename(self, batch_id, new_name):
        """重命名批次：文件夹名 = 时间戳[+_名称]（无名称则仅时间戳）。
        批次内存在未结束任务（进行中/排队）时禁止重命名。返回新批次 dict。"""
        new_name = (new_name or "").strip()
        with self._lock:
            b = self._find_locked(batch_id)
            if not b:
                raise KeyError("批次不存在")
            if _batch_has_active_tasks(batch_id):
                raise RuntimeError("批次内仍有进行中/排队任务，请等待结束后再重命名")
            stamp = b["stamp"] or parse_batch_dirname(batch_id)[0] or time.strftime("%Y-%m-%d_%H%M%S")
            new_id = batch_folder_name(stamp, new_name)
            old_dir = Path(b["dir"]).resolve()
            if new_id == batch_id:
                return deepcopy(b)
            new_dir = old_dir.parent / new_id
            if new_dir.exists():
                raise RuntimeError("同名批次文件夹已存在")
            os.rename(str(old_dir), str(new_dir))
            b.update({"id": new_id, "name": new_name, "dir": str(new_dir), "closed": True})
            self._batches.pop(batch_id, None)
            self._batches[new_id] = b
            if batch_id in self._order:
                self._order[self._order.index(batch_id)] = new_id
            if self._current_id == batch_id:
                self._current_id = new_id
            _patch_task_dirs(batch_id, old_dir, new_dir, new_id)
            return deepcopy(b)

    def reset_root(self):
        """输出根目录变更时清空批次与历史，下一批落到新根目录。"""
        with self._lock:
            self._batches.clear()
            self._order.clear()
            self._current_id = None
            self._fallback_warning = ""


BATCH = BatchManager()
IDLE_SINCE = None        # 队列空闲起始时间（scheduler 更新，供前端倒计时）


# ==================================================================
# 任务队列（线程安全）
# ==================================================================
class TaskStore:
    def __init__(self):
        self._lock = threading.Lock()
        self.tasks = {}           # id -> task dict
        self.order = []           # 保序
        self.total = 0
        self.completed = 0
        self.active_processing = 0

    # ---- 变更 ----
    def add(self, task):
        with self._lock:
            self.tasks[task["id"]] = task
            self.order.append(task["id"])
            self.total += 1

    def get(self, tid):
        with self._lock:
            return self.tasks.get(tid)

    def all(self):
        with self._lock:
            return [self.tasks[tid] for tid in self.order]

    def remove(self, tid):
        with self._lock:
            if tid not in self.tasks:
                return False
            t = self.tasks[tid]
            if t.get("status") == ST_DONE:
                self.completed = max(0, self.completed - 1)
            del self.tasks[tid]
            if tid in self.order:
                self.order.remove(tid)
            self.total = max(0, self.total - 1)
            return True

    def set_status(self, tid, status, extra=None):
        with self._lock:
            t = self.tasks.get(tid)
            if not t:
                return
            t["status"] = status
            if extra:
                t.update(extra)
            if status in (ST_DONE, ST_ERROR, ST_CANCELED):
                t["finished_at"] = time.time()
                if status == ST_DONE:
                    self.completed += 1
        # 在锁外更新活动计数：锁为非重入 Lock，持锁内调用会自死锁
        self.recalc_active()

    def recalc_active(self):
        with self._lock:
            self.active_processing = sum(
                1 for tt in self.tasks.values() if tt.get("status") == ST_PROCESS
            )

    def remaining(self):
        with self._lock:
            return sum(
                1 for t in self.tasks.values()
                if t.get("status") not in (ST_DONE, ST_ERROR, ST_CANCELED)
            )

    def progress(self):
        with self._lock:
            return {
                "total": self.total,
                "completed": self.completed,
                "final": self.total - self.completed if False else None,
                "remaining": sum(
                    1 for t in self.tasks.values()
                    if t.get("status") not in (ST_DONE, ST_ERROR, ST_CANCELED)
                ),
            }


STORE = TaskStore()
QUEUE = asyncio.Queue()          # 待处理任务队列
STOP_FLAG = threading.Event()


# ==================================================================
# 单个文件解析（复用官方 client，全程状态回调）
# ==================================================================
async def process_one(task):
    tid = task["id"]
    fpath = Path(task["file_path"])
    stime = time.monotonic()

    # 任务可能在排队期间被取消（已从 STORE 移除）。
    if STORE.get(tid) is None:
        return

    def set_st(st, **kw):
        STORE.set_status(tid, st, extra={"elapsed": round(time.monotonic() - stime, 1), **kw})

    set_st(ST_PREPARE)
    try:
        # 确保引擎在运行且健康就绪（冷启动需加载模型，ensure_started 内等待 /health healthy）
        set_st(ST_CHECK)
        base_url, _server = await asyncio.to_thread(ENGINE.ensure_started)

        form_data = build_options(
            task["options"]["lang"],
            task["options"]["backend"],
            task["options"]["formula"],
            task["options"]["table"],
            task["options"]["image_analysis"],
            task["options"]["is_ocr"],
            task["options"]["effort"],
            task["options"]["max_pages"],
            formats=task["options"].get("formats"),
        )
        # 上传名用原文件名，让引擎产物（md/json）按原文件命名，而非内部 uuid
        upload_assets = [
            UploadAsset(path=fpath, upload_name=_gradio_upload_name(task["filename"]))
        ]

        set_st(ST_SUBMIT)
        submit_response = await asyncio.to_thread(
            api_client.submit_parse_task_sync, base_url, upload_assets, form_data
        )

        # 状态轮询（排队 -> 解析中）
        async with httpx.AsyncClient(timeout=httpx.Timeout(600.0, connect=30.0)) as client:
            def handle_snapshot(snapshot):
                st = snapshot.status
                msg = None
                if st == "pending":
                    q = snapshot.queued_ahead
                    set_st(ST_QUEUE, queued_ahead=q, detail="排队等待中")
                elif st == "processing":
                    set_st(ST_PROCESS, detail="解析中")
                elif st == "completed":
                    set_st(ST_PROCESS)
                elif st == "failed":
                    raise RuntimeError("引擎返回了失败状态")

            await api_client.wait_for_task_result(
                client=client,
                submit_response=submit_response,
                task_label=fpath.name,
                status_snapshot_callback=handle_snapshot,
            )

            set_st(ST_DOWNLOAD)
            zip_path = await api_client.download_result_zip(
                client=client, submit_response=submit_response, task_label=fpath.name
            )

        set_st(ST_OUTPUT)
        # 结果写入提交时锁定的批次目录：D:\MinerU-Output\<日期_时间>\<原文件名>\，不保留 zip
        file_dir = BATCH.file_dir(Path(task["filename"]).stem, batch_id=task.get("batch_id"))
        md_path, json_path, files = await asyncio.to_thread(
            extract_result, zip_path, file_dir)
        zip_path.unlink(missing_ok=True)

        preview_md = ""
        if md_path:
            try:
                preview_md = md_path.read_text(encoding="utf-8", errors="replace")[:8000]
            except Exception:
                preview_md = ""

        set_st(ST_DONE, detail="", result={
            "dir": str(file_dir),
            "md_path": str(md_path) if md_path else None,
            "json_path": str(json_path) if json_path else None,
            "files": files,
            "preview_md": preview_md,
            "duration": round(time.monotonic() - stime, 1),
        })
    except Exception as e:  # noqa: BLE001
        set_st(ST_ERROR, error=str(e)[:500], duration=round(time.monotonic() - stime, 1))
    finally:
        # 任务可能已被移除；仍在队列中则重算活动计数（避免并发误清零）
        if STORE.get(tid) is not None:
            STORE.recalc_active()


def extract_result(zip_path, out_dir):
    """解压结果 zip 到 out_dir，去掉内部的顶层任务目录与 auto/vlm 等解析方式目录，
    直接铺平到 out_dir（引擎输出结构固定为 <任务名>/<parse_method>/...）。
    返回 (md路径, json路径, 文件相对路径列表)。"""
    import zipfile
    md_path = json_path = None
    files = []
    try:
        with zipfile.ZipFile(zip_path) as zf:
            infos = [i for i in zf.infolist() if not i.is_dir()]
            # 计算所有条目共有的顶层目录层级（引擎产物形如 <任务名>/auto/<文件>）
            common = None
            for info in infos:
                parts = info.filename.replace("\\", "/").split("/")
                if common is None:
                    common = parts[:-1]
                else:
                    k = 0
                    while (k < len(common) and k < len(parts) - 1
                           and common[k] == parts[k]):
                        k += 1
                    common = common[:k]
                if not common:
                    break
            strip_n = len(common) if common else 0
            for info in infos:
                raw = info.filename.replace("\\", "/")
                if raw.startswith("/") or ".." in raw.split("/"):
                    continue
                parts = [p for p in raw.split("/") if p and p != "."]
                if strip_n and len(parts) > strip_n:
                    parts = parts[strip_n:]
                # 兜底：若仍残留 auto/vlm 等单层包装目录，再剥一层
                while parts and parts[0] in ("auto", "vlm", "office"):
                    parts = parts[1:]
                if not parts:
                    continue
                name = "/".join(parts)
                target = out_dir / name
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(zf.read(info.filename))
                files.append(name)
        mds = sorted(out_dir.rglob("*.md"))
        jsons = sorted(out_dir.rglob("*.json"))
        if mds:
            md_path = next((p for p in mds if p.parent == out_dir), mds[0])
        if jsons:
            json_path = next((p for p in jsons if p.parent == out_dir), jsons[0])
    except Exception:
        pass
    return md_path, json_path, files


# ==================================================================
# 调度器：并发处理，空闲自动释放
# ==================================================================
async def scheduler():
    async def worker(idx):
        while not STOP_FLAG.is_set():
            try:
                task = await asyncio.wait_for(QUEUE.get(), timeout=0.3)
            except asyncio.TimeoutError:
                continue
            try:
                await process_one(task)
            finally:
                QUEUE.task_done()

    workers = [asyncio.create_task(worker(i)) for i in range(MAX_WORKERS)]

    # 空闲释放监控：没有进行中任务即视为空闲，累计超过配置阈值则释放引擎 / 关闭批次
    global IDLE_SINCE
    last_busy = time.time()
    idle_warned = False
    while not STOP_FLAG.is_set():
        if STORE.active_processing == 0 and STORE.remaining() == 0:
            if not idle_warned:
                last_busy = time.time()
                idle_warned = True
                IDLE_SINCE = last_busy
            idle_secs = CONFIG.get().get("idle_release_seconds", 120)
            if time.time() - last_busy > idle_secs and ENGINE.is_running():
                ENGINE.stop()  # 释放 GPU 显存
            batch_close = CONFIG.get().get("batch_close_seconds", 60)
            if BATCH.is_open() and time.time() - last_busy > batch_close:
                BATCH.close()  # 关闭批次，下一批重新建目录
        else:
            idle_warned = False
            IDLE_SINCE = None
        await asyncio.sleep(1)

    for w in workers:
        w.cancel()


@app.on_event("startup")
async def _startup():
    threading.Thread(target=_background_loop, daemon=True).start()
    ENGINE_STAGE.start()


def _background_loop():
    """独立事件循环运行调度器，避免与 FastAPI 主循环冲突。"""
    import traceback
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(scheduler())
    except Exception:
        traceback.print_exc()  # 落盘到 webui.err.log，避免静默吞掉调度器异常
    finally:
        # 收尾：取消所有待处理任务，优雅关闭事件循环
        pending = asyncio.all_tasks(loop)
        for t in pending:
            t.cancel()
        loop.run_until_complete(
            asyncio.gather(*pending, return_exceptions=True)
        )
        loop.close()


# ==================================================================
# REST API
# ==================================================================
@app.post("/api/shutdown")
def shutdown():
    """优雅关闭：后台线程先停止解析引擎（释放 GPU 显存与内存），
    再退出 WebUI 进程。托盘退出时调用，避免强杀导致资源未释放。"""
    def _graceful_exit():
        try:
            _stop_engine_safely()
        except Exception:
            pass
        time.sleep(0.6)
        try:
            os._exit(0)
        except Exception:
            pass
    threading.Thread(target=_graceful_exit, daemon=True).start()
    return {"ok": True, "message": "正在停止服务并释放资源…"}


@app.get("/api/health")
def health():
    eng = ENGINE.is_running()
    p = STORE.progress()
    cfg = CONFIG.get()
    return {
        "engine_running": eng,
        "engine_state": engine_state(),
        "backend": DEFAULT_BACKEND,
        "queue": p,
        "idle_release": cfg.get("idle_release_seconds", 120),
    }


@app.get("/api/config")
def get_config():
    """返回当前运行配置（含格式化后的输出目录绝对路径）。"""
    cfg = CONFIG.get()
    out = cfg.get("output_dir") or str(OUTPUTS)
    cfg["output_dir_abs"] = str(Path(out).resolve())
    return cfg


@app.post("/api/config")
async def update_config(payload: dict):
    """更新运行配置并持久化。白名单：output_dir / idle_release_seconds / batch_close_seconds / formats。"""
    cfg = CONFIG.get()
    out = payload.get("output_dir")
    if out:
        try:
            Path(out).mkdir(parents=True, exist_ok=True)
        except Exception as e:
            raise HTTPException(400, f"输出目录不可用: {e}")
        if out != cfg.get("output_dir"):
            BATCH.reset_root()   # 根目录变更，下一批落到新目录
    if "idle_release_seconds" in payload and payload["idle_release_seconds"] is not None:
        try:
            payload["idle_release_seconds"] = max(5, int(payload["idle_release_seconds"]))
        except Exception:
            raise HTTPException(400, "空闲释放时间必须是整数（秒）")
    if "batch_close_seconds" in payload and payload["batch_close_seconds"] is not None:
        try:
            payload["batch_close_seconds"] = max(10, min(86400, int(payload["batch_close_seconds"])))
        except Exception:
            raise HTTPException(400, "批次关闭时间必须是整数（秒）")
    # 解析参数默认值：仅白名单字段 + 值域校验
    dp = payload.get("default_params")
    if isinstance(dp, dict):
        LANG_OK = {"ch", "en", "ja", "ko", "auto"}
        BACKEND_OK = {"pipeline", "hybrid-engine"}
        EFFORT_OK = {"high", "medium", "low", "none"}
        if "lang" in dp and dp["lang"] not in LANG_OK:
            raise HTTPException(400, "语言取值无效")
        if "backend" in dp and dp["backend"] not in BACKEND_OK:
            raise HTTPException(400, "引擎取值无效")
        if "effort" in dp and dp["effort"] not in EFFORT_OK:
            raise HTTPException(400, "推理强度取值无效")
        if "max_pages" in dp and dp["max_pages"] is not None:
            try:
                dp["max_pages"] = max(1, min(99999, int(dp["max_pages"])))
            except Exception:
                raise HTTPException(400, "最大页数必须是整数（1-99999）")
        for k in ("formula", "table", "image_analysis", "is_ocr"):
            if k in dp and not isinstance(dp[k], bool):
                dp[k] = bool(dp[k])
    CONFIG.update(payload)
    return CONFIG.get()


@app.post("/api/tasks")
async def create_task(
    files: list[UploadFile] = File(...),
    lang: str = Form("ch"),
    backend: str = Form("pipeline"),
    formula: bool = Form(True),
    table: bool = Form(True),
    image_analysis: bool = Form(True),
    is_ocr: bool = Form(False),
    effort: str = Form("medium"),
    max_pages: int = Form(1000),
    formats: str = Form(""),
    batch: str = Form("current"),
    batch_name: str = Form(""),
):
    """一次提交一个或多个文件（支持执行中追加 = 队列自动接上）。
    formats：逗号分隔的启用格式键（如 "md,middle_json"）；空=用全局配置。
    batch：提交目标批次 —— "current"=当前开放批次(默认，已关闭则自动新建) /
           "new"=新建批次 / 批次文件夹名=复用历史批次。
    batch_name：新建批次的可选名称，用于文件夹名后缀（如 2026-08-29_092823_社稳报告）。"""
    fmt = _parse_formats(formats)
    # 提交时锁定目标批次：新建 / 复用指定历史批次 / 当前开放批次（不存在或已关闭则新建）。
    # 任务绑定 batch_id（批次文件夹名），处理期按该批次建目录，避免空闲自动关闭后跑错批次。
    target = (batch or "current").strip()
    new_name = (batch_name or "").strip()
    if target == "new":
        BATCH.create(name=new_name)
    elif target and target != "current":
        BATCH.open(target)
    else:
        BATCH.open(name=new_name)
    locked_batch_id = BATCH.current_id()
    results = []
    for f in files:
        tid = uuid.uuid4().hex[:10]
        filename = f.filename or "file.pdf"
        # 保留后缀防上传名混乱，用 uuid 作内部文件名
        ext = Path(filename).suffix or ".pdf"
        safe_name = f"{tid}{ext}"
        save_path = UPLOADS / safe_name
        content = await f.read()
        save_path.write_bytes(content)

        task = {
            "id": tid,
            "filename": filename,
            "file_path": str(save_path),
            "batch_id": locked_batch_id,
            "status": ST_PREPARE,
            "created_at": time.time(),
            "elapsed": 0,
            "queued_ahead": None,
            "detail": "",
            "options": {
                "lang": _norm_lang(lang),
                "backend": backend,
                "formula": formula,
                "table": table,
                "image_analysis": image_analysis,
                "is_ocr": is_ocr,
                "effort": effort,
                "max_pages": max_pages,
                "formats": fmt,
            },
            "result": None,
            "error": "",
        }
        STORE.add(task)
        await QUEUE.put(task)
        results.append({"id": tid, "filename": filename, "status": task["status"]})
    return {"tasks": results}


def _parse_formats(raw):
    """解析逗号分隔的格式键为 {key: bool}；空串回退到全局配置。"""
    base = dict(DEFAULT_CONFIG["formats"])
    if not raw or not raw.strip():
        return CONFIG.get().get("formats", base)
    enabled = {k.strip() for k in raw.split(",") if k.strip()}
    return {k: (k in enabled) for k in base}


def _norm_lang(lang):
    if lang and "(" in lang and ")" in lang:
        return lang.split("(")[0].strip()
    return lang or "ch"


@app.post("/api/batches/rename")
def rename_batch(payload: dict):
    """重命名批次（修改输出文件夹名，追加/去掉批次名）。
    批次内存在未结束任务时禁止，返回新批次元数据。"""
    bid = (payload or {}).get("id") or ""
    name = (payload or {}).get("name") or ""
    if not bid:
        raise HTTPException(400, "缺少批次 ID")
    try:
        b = BATCH.rename(bid, name)
    except KeyError:
        raise HTTPException(404, "批次不存在")
    except RuntimeError as e:
        raise HTTPException(400, str(e))
    return {"ok": True, "batch": b}


@app.get("/api/tasks")
def list_tasks():
    p = STORE.progress()
    tasks = STORE.all()
    # 各批次当前任务数（供前端选择器显示"N 个任务"）
    counts = {}
    for t in tasks:
        bid = t.get("batch_id")
        if bid:
            counts[bid] = counts.get(bid, 0) + 1
    batch_list = []
    for b in BATCH.scan():
        batch_list.append({
            "id": b["id"],
            "stamp": b.get("stamp", ""),
            "name": b.get("name", ""),
            "dir": b["dir"],
            "closed": b.get("closed", True),
            "task_count": counts.get(b["id"], 0),
        })
    return {
        "total": p["total"],
        "completed": p["completed"],
        "active_processing": STORE.active_processing,
        "engine_running": ENGINE.is_running(),
        "engine_state": engine_state(),
        "engine_stage": ENGINE_STAGE.current(),
        "batch": BATCH.current(),
        "batch_id": BATCH.current_id(),
        "batch_open": BATCH.is_open(),
        "batches": batch_list,
        "idle_since": IDLE_SINCE,
        "fallback_warning": BATCH.fallback_warning(),
        "tasks": tasks,
    }


@app.delete("/api/tasks/{tid}")
def delete_task(tid: str, delete_files: int = 0):
    """删除任务（含已完成/失败/排队/准备中的）。
    进行中的解析任务不可删除，需等待结束。
    delete_files=1 时连同其磁盘输出目录一并删除（仅限批次输出根目录内）。
    """
    t = STORE.get(tid)
    if not t:
        raise HTTPException(404, "任务不存在")
    if t["status"] == ST_PROCESS:
        raise HTTPException(400, "任务正在解析中，无法删除，请等待完成或失败")
    result_dir = None
    if t.get("result") and t["result"].get("dir"):
        result_dir = t["result"]["dir"]
    STORE.remove(tid)
    removed_dir = ""
    if delete_files and result_dir:
        removed_dir = _safe_delete_result_dir(result_dir)
    return {"removed": True, "dir": removed_dir}


@app.post("/api/tasks/clear")
def clear_finished(delete_files: int = 0):
    """清空所有已结束（完成/失败/已取消）的任务。
    delete_files=1 时一并删除各任务磁盘输出目录。"""
    finished = [t for t in STORE.all() if t["status"] in TERMINAL_STEPS]
    removed_dirs = []
    for t in finished:
        d = None
        if t.get("result") and t["result"].get("dir"):
            d = t["result"]["dir"]
        STORE.remove(t["id"])
        if delete_files and d:
            removed_dirs.append(_safe_delete_result_dir(d))
    return {"cleared": len(finished), "dirs": removed_dirs}


def _safe_delete_result_dir(path):
    """安全删除任务输出目录：仅允许删除批次输出根目录（含回退目录）内的子目录。"""
    try:
        candidates = [
            Path(CONFIG.get().get("output_dir") or "").resolve(),
            OUTPUTS.resolve(),
        ]
        target = Path(path).resolve()
        for root in candidates:
            try:
                target.relative_to(root)
            except ValueError:
                continue
            if target != root:
                import shutil
                shutil.rmtree(target, ignore_errors=True)
                return str(target)
        return ""
    except Exception:
        return ""


@app.post("/api/tasks/{tid}/retry")
async def retry_task(tid: str):
    """失败任务重试：重置状态后重新入队（不重复计入 total）。"""
    t = STORE.get(tid)
    if not t:
        raise HTTPException(404, "任务不存在")
    if t["status"] != ST_ERROR:
        raise HTTPException(400, "仅失败任务可重试")
    t.update(status=ST_PREPARE, error="", result=None, elapsed=0,
             queued_ahead=None, detail="", finished_at=None)
    STORE.active_processing = sum(
        1 for tt in STORE.tasks.values() if tt.get("status") == ST_PROCESS
    )
    await QUEUE.put(t)
    return {"retried": True, "id": tid}


@app.get("/api/tasks/{tid}/result.md")
def result_md(tid: str):
    t = STORE.get(tid)
    if not t or not t.get("result") or not t["result"].get("md_path"):
        raise HTTPException(404, "Markdown 结果不存在")
    mp = Path(t["result"]["md_path"])
    if not mp.exists():
        raise HTTPException(404, "Markdown 文件已丢失")
    return FileResponse(mp, media_type="text/markdown; charset=utf-8")


@app.get("/api/tasks/{tid}/result.json")
def result_json(tid: str):
    t = STORE.get(tid)
    if not t or not t.get("result") or not t["result"].get("json_path"):
        raise HTTPException(404, "JSON 结果不存在")
    jp = Path(t["result"]["json_path"])
    if not jp.exists():
        raise HTTPException(404, "JSON 文件已丢失")
    payload = json.loads(jp.read_text(encoding="utf-8", errors="replace"))
    return JSONResponse(payload)


@app.get("/api/tasks/{tid}/files")
def task_files(tid: str):
    """列出任务结果目录内的全部文件（供前端预览图片/JSON）。"""
    t = STORE.get(tid)
    if not t or not t.get("result") or not t["result"].get("dir"):
        raise HTTPException(404, "结果目录不存在")
    d = Path(t["result"]["dir"])
    if not d.exists():
        raise HTTPException(404, "结果目录已丢失")
    files = []
    for p in sorted(d.rglob("*")):
        if p.is_file():
            files.append({
                "name": p.name,
                "rel": str(p.relative_to(d)).replace("\\", "/"),
                "ext": p.suffix.lower().lstrip("."),
                "size": p.stat().st_size,
            })
    return {"dir": str(d), "files": files}


@app.get("/api/tasks/{tid}/file")
def task_file(tid: str, name: str = ""):
    """按相对路径返回结果目录内的文件（用于预览图片等）。"""
    t = STORE.get(tid)
    if not t or not t.get("result") or not t["result"].get("dir"):
        raise HTTPException(404, "结果目录不存在")
    d = Path(t["result"]["dir"]).resolve()
    target = (d / name).resolve()
    if not str(target).startswith(str(d)) or not target.is_file():
        raise HTTPException(404, "文件不存在")
    return FileResponse(target)


@app.post("/api/pick_dir")
def pick_dir():
    """在服务器桌面弹出系统文件夹选择器（tkinter），返回所选路径。"""
    def ask():
        try:
            import tkinter as tk
            from tkinter import filedialog
            root = tk.Tk()
            root.withdraw()
            root.attributes("-topmost", True)
            path = filedialog.askdirectory(title="选择输出目录")
            root.destroy()
            return path or ""
        except Exception:
            return ""
    path = ask()
    if not path:
        return {"canceled": True, "path": ""}
    try:
        Path(path).mkdir(parents=True, exist_ok=True)
    except Exception:
        pass
    return {"canceled": False, "path": path}


@app.post("/api/open_dir")
def open_dir(payload: dict):
    """在系统文件管理器中打开指定目录（空路径则打开输出根目录）。"""
    path = (payload or {}).get("path") or CONFIG.get().get("output_dir") or str(OUTPUTS)
    p = Path(path)
    if not p.exists():
        raise HTTPException(404, "目录不存在")
    try:
        os.startfile(str(p))
    except Exception as e:
        raise HTTPException(500, f"打开目录失败: {e}")
    return {"ok": True}


# 静态资源：no-cache 确保每次用 ETag/Last-Modified 重新校验，
# 升级后浏览器不会因内存/启发式缓存而继续使用旧版前端资源
class _NoCacheStaticFiles(StaticFiles):
    async def get_response(self, path: str, scope):
        resp = await super().get_response(path, scope)
        resp.headers["Cache-Control"] = "no-cache"
        return resp


app.mount("/static", _NoCacheStaticFiles(directory=str(STATIC)), name="static")


@app.get("/")
async def index():
    # 按静态文件 mtime 附加版本号：前端改动后浏览器 URL 变化，强制拉取新资源，
    # 配合 /static 的 no-cache 头，彻底避免升级后读到旧版 CSS/JS
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    for name in ("style.css", "app.js", "markdown-it.min.js", "markdown-it-multimd-table.min.js",
                 "highlight.min.js", "katex.min.js", "purify.min.js", "katex.min.css", "highlight-github.min.css"):
        p = STATIC / "lib" / name if name.startswith(("markdown", "highlight", "katex", "purify")) else STATIC / name
        v = int(p.stat().st_mtime) if p.exists() else 0
        html = html.replace(f"/static/{name}", f"/static/{name}?v={v}")
    return HTMLResponse(html)


# ==================================================================
# 单实例保证：仅允许一个 WebUI 进程运行（从而引擎也只有一份）
# ==================================================================
_SINGLETON_MUTEX = None          # 保持互斥体句柄存活（进程退出时由系统释放）


def _acquire_singleton():
    """Windows 命名互斥体：全局唯一运行实例。
    返回 True 表示本进程可继续；False 表示已有实例在运行，应退出。"""
    global _SINGLETON_MUTEX
    if os.name != "nt":
        return True
    try:
        import ctypes
        name = f"Local\\MinerU_WebUI_{WEBUI_PORT}"
        handle = ctypes.windll.kernel32.CreateMutexW(None, False, name)
        if ctypes.windll.kernel32.GetLastError() == 183:   # ERROR_ALREADY_EXISTS
            return False
        _SINGLETON_MUTEX = handle
        return True
    except Exception:
        # 非 Windows / 异常环境不阻止启动，交由端口检查兜底
        return True


def _port_already_served():
    """兜底检查：端口已被本服务的健康实例占用（互斥体失效时的第二道防线）。"""
    try:
        with urllib.request.urlopen(
            f"http://{WEBUI_HOST}:{WEBUI_PORT}/api/health", timeout=1.5
        ) as r:
            data = json.loads(r.read().decode("utf-8"))
        return isinstance(data, dict) and "engine_state" in data
    except Exception:
        return False


def main():
    # 先重定向引擎日志（在引擎子进程可能被拉起前），供阶段跟踪解析
    _redirect_engine_log()
    if not _acquire_singleton():
        print(
            "检测到 MinerU WebUI 已在运行（仅允许一个实例），本实例退出。",
            file=sys.stderr, flush=True)
        return
    if _port_already_served():
        print(
            f"端口 {WEBUI_PORT} 已被 MinerU WebUI 占用，本实例退出。",
            file=sys.stderr, flush=True)
        return
    uvicorn.run(app, host=WEBUI_HOST, port=WEBUI_PORT, log_level="info")


if __name__ == "__main__":
    main()