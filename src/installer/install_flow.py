# -*- coding: utf-8 -*-
"""MinerU 一键安装流程（供安装器 GUI 调用，也可命令行直接运行）。

在目标机完成：复制运行文件 → 创建 venv → 装依赖 → GPU 决策 → 下载模型 → 生成桌面快捷方式。

用法:
    python install_flow.py --root C:\\MinerU --src <资源源目录>
                           [--mirror <pypi镜像>] [--local-torch-dir <dir>]
                           [--result <结果json路径>] [--skip-model]

说明:
  - 复用 install_mineru_uv.py 的 Installer 类（同目录或打包资源内）。
  - 模型只下载 pipeline（PDF-Extract-Kit-1.0 中实际用到的 40 个文件 ≈ 2.4GB），
    不下载 VLM（hybrid 后端用）。多线程分段下载 + sha256 完整性校验 + 断点续传。
  - 每步输出带 [阶段] 前缀，供 GUI 解析进度；最终把结果写入 --result 指定的 JSON。
"""
import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import threading
import time
import urllib.parse
from pathlib import Path

# 无控制台 GUI 父进程启动控制台子进程时避免闪黑框
_NO_WINDOW = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0

# 被 GUI 以管道启动时 stdout 走 locale(GBK)，uv 输出的 ✓ 等字符会抛
# UnicodeEncodeError 中断安装；统一 UTF-8 输出（GUI 端按 UTF-8 解码）
for _s in (sys.stdout, sys.stderr):
    try:
        if _s and _s.encoding and _s.encoding.lower().replace("-", "") != "utf8":
            _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

# 允许从打包资源目录 import install_mineru_uv：
#   打包后本文件位于 _MEIPASS 根，install_mineru_uv.py 位于 _MEIPASS/scripts/
#   源码运行本文件位于 src/installer/，install_mineru_uv.py 位于 <项目根>/scripts/
_HERE = os.path.dirname(os.path.abspath(__file__))
for _p in (_HERE, os.path.join(_HERE, "scripts"),
           os.path.join(os.path.dirname(os.path.dirname(_HERE)), "scripts")):
    if os.path.isdir(_p) and _p not in sys.path:
        sys.path.insert(0, _p)
try:
    import install_mineru_uv as base
except ImportError:
    base = None
else:
    # 供安装器 GUI 调用时输出 [pkg] 结构化事件（逐包下载进度）
    base.GUI_EVENTS = True

PIPELINE_MODEL = "OpenDataLab/PDF-Extract-Kit-1.0"

# 应用版本号：写入安装清单，供远程修复/升级对比（P4）
APP_VERSION = "1.0.0"

# ---------------- 模型下载（fastdl 多源竞速引擎） ----------------
# 仓库全量 184 文件 ≈ 14GB，其中引擎实际只用以下 40 个（≈ 2.42GB，已在本机验证）。
# 精准下载可避免整仓拉取；sha256 用于下载后完整性校验。
_MS_DL = ("https://modelscope.cn/api/v1/models/" + PIPELINE_MODEL
          + "/repo?FilePath={fp}&Revision=master")  # modelscope 国内源
_HFM_DL = ("https://hf-mirror.com/open-data-lab/PDF-Extract-Kit-1.0"
           "/resolve/main/{fp}")                    # HuggingFace 镜像兜底源
_MS_SRC, _HFM_SRC = "modelscope", "hf-mirror"
_DL_THREADS_DEFAULT = 16        # 下载线程数默认（范围 4-64，D3）

MODEL_FILES = [
    ('models/Layout/PP-DocLayoutV2/config.json', 3787, '18a696b54c64c4fa582afcd3a41407c4b65a99dc7ab187ad2fed8af8e4128ad8'),
    ('models/Layout/PP-DocLayoutV2/model.safetensors', 214798436, 'e60f3725aeedc88fd319416ef166bda79171a41516a301c27cab9132dc2739d2'),
    ('models/Layout/PP-DocLayoutV2/preprocessor_config.json', 575, '56281a70c931a291dcaf653605fb4df713fd823f65e939aecd6005c26346a103'),
    ('models/MFR/pp_formulanet_plus_m/PP-FormulaNet_plus-M.pth', 617338934, '034efee70ef56d8ab7cf3b9b945865cdaf22461ad03b0f6e68bf9234f167f035'),
    ('models/MFR/pp_formulanet_plus_m/PP-FormulaNet_plus-M_inference.yml', 2244564, '87b5f3d7f2b2fe553627d77b37f496608ca150ebd0ef62d362591edca47b5538'),
    ('models/MFR/unimernet_hf_small_2503/README.md', 1657, '96574f3857e919353024edc423b9165dbf46e902cfe97b5b7ec552283bd744f6'),
    ('models/MFR/unimernet_hf_small_2503/config.json', 5094, '64c02e9897410658f7668c6a334a8b306276e4697656cbe06f86d8c4f01fc040'),
    ('models/MFR/unimernet_hf_small_2503/generation_config.json', 191, 'd56ca9d5c5efa4283a2565ae42771bafd02910b56ef9c53e9b441c9c4c896d09'),
    ('models/MFR/unimernet_hf_small_2503/model.safetensors', 810036696, '9244e2565585c0f89bc3a6eeeea080ef3c588375fc0d536074fe88e80b917cda'),
    ('models/MFR/unimernet_hf_small_2503/special_tokens_map.json', 552, '358c249e2fb29060c6b73157d428853b0c48710deffc8ee670ab1013880946c9'),
    ('models/MFR/unimernet_hf_small_2503/tokenizer.json', 3581950, 'f8e29e3c3a8017f067b62a3d2d9211bb4cebc08a25afe58d3a6069981e3684d6'),
    ('models/MFR/unimernet_hf_small_2503/tokenizer_config.json', 4522, '28b99e33895e06389c26c139b1333b82b7f5d8ed5f4fd14998acfd7c20989338'),
    ('models/OCR/paddleocr_torch/Multilingual_PP-OCRv3_det_infer.pth', 2540826, '05eb1c89030b269b830ba7f2d424a4ac80c7593ea1795fef9777fedbc18e383f'),
    ('models/OCR/paddleocr_torch/arabic_PP-OCRv5_rec_infer.pth', 24079925, '2ae0a5e1e8151105eb864c4784a8a435821dd1aeddcd4c3018041b0aa897add0'),
    ('models/OCR/paddleocr_torch/ch_PP-OCRv4_rec_infer.pth', 26919769, 'cb4265bb4300a2487e93e82ccfa1924bf9cd1194c1a202ab17a96b4911c27e0b'),
    ('models/OCR/paddleocr_torch/ch_PP-OCRv4_rec_server_doc_infer.pth', 101167457, 'f65e699f4ca792fbce0e92d1df4c9bbdefe3e21bbdb01c3075cc49470b9bc1cc'),
    ('models/OCR/paddleocr_torch/ch_PP-OCRv4_rec_server_infer.pth', 96808553, '2c0c9f5180ae3e4d8ea9d3830116ac49900abbb2af3985db02c2bbf484bb0bf9'),
    ('models/OCR/paddleocr_torch/ch_PP-OCRv5_det_infer.pth', 14506268, 'df848ed5060bac4d0f6e58572aea97d92e909a8a87cf292849237b0e84f6ffdb'),
    ('models/OCR/paddleocr_torch/ch_PP-OCRv5_rec_infer.pth', 32611609, 'd20ee8dac2ca63e2d1989b02ecc42595c71d61bf8dd8c8ddc5ad2ee68e7b5be2'),
    ('models/OCR/paddleocr_torch/ch_PP-OCRv5_rec_server_infer.pth', 134640672, '4767ddc90c1532ec01d881a980dae0a0b92679f4f82f88c4e9f92563de69e740'),
    ('models/OCR/paddleocr_torch/ch_PP-OCRv6_medium_rec_infer.safetensors', 76741720, '5f43c16f2a684b1d2284662178bdb604febd3d6bfdb5ca73828d08d0f7c0c3e9'),
    ('models/OCR/paddleocr_torch/ch_PP-OCRv6_small_det_infer.safetensors', 9938124, '89a96a8adc4e9cd0c994098edc76022e496d35844392562b4694c8fbc583f2da'),
    ('models/OCR/paddleocr_torch/ch_PP-OCRv6_small_rec_infer.safetensors', 21204736, 'f65a332afe5aa663f0b9d5706f4ae8457b5b4058a842d5c1eb22df505c27d642'),
    ('models/OCR/paddleocr_torch/ch_ptocr_mobile_v2.0_cls_infer.pth', 588638, 'bfe13860824b3365c0c7f7ccfcddc8ff11645c60051739ff18bc9913f60c98e1'),
    ('models/OCR/paddleocr_torch/cyrillic_PP-OCRv5_rec_infer.pth', 24129777, 'ddca2b729846e418b62c557ce7af3a8d2e1ba335e9b3c79d7462640e649b72f7'),
    ('models/OCR/paddleocr_torch/devanagari_PP-OCRv5_rec_infer.pth', 23991219, '69eb4ce12aa71366a03bb391cb653e48ae7330e4ef08d3a6948d32dccc4d67f4'),
    ('models/OCR/paddleocr_torch/el_PP-OCRv5_rec_infer.pth', 23889711, '2ed368d4d02a6733d0be2b982b8ed2d205603c0395e6c4994158527dca637644'),
    ('models/OCR/paddleocr_torch/en_PP-OCRv5_rec_infer.pth', 23929399, '259a277836bf0e094910949ef4635927ad3f82c20b37f4010749af968967e282'),
    ('models/OCR/paddleocr_torch/eslav_PP-OCRv5_rec_infer.pth', 23964463, '8dbe3608918ff5444befe155ae6299dd9655273ac02fb862c65dd1304df54b93'),
    ('models/OCR/paddleocr_torch/ka_PP-OCRv3_rec_infer.pth', 8979659, '4a537f8aa90afb4f3bb63d0950c2d408b18d586509956b4f56652ef0829764f3'),
    ('models/OCR/paddleocr_torch/korean_PP-OCRv5_rec_infer.pth', 29495617, '405b72b79a652a87c49d92700d5677e82375c5f6e242b6f54e5faf2264f8aeb5'),
    ('models/OCR/paddleocr_torch/latin_PP-OCRv5_rec_infer.pth', 24118861, 'eeb50a7998a44ac6f3a03855774d5c12aeca93e2209412679879d2bf604a8fd6'),
    ('models/OCR/paddleocr_torch/seal_PP-OCRv4_det_infer.pth', 14506268, 'a7777ca66448ab90948ce5a3257e4c959d6eacf0489fbadd5133dbe8f89662ae'),
    ('models/OCR/paddleocr_torch/seal_PP-OCRv4_det_server_infer.pth', 114030092, '283d716bdd93d011edca4563d218a767b273c47bf9c32cb3e8a0baf5b12c8242'),
    ('models/OCR/paddleocr_torch/ta_PP-OCRv5_rec_infer.pth', 23966667, '577d0530f55e3856064fb31e8bcd3ca4714151bacd93696a1f8dda9c06e4bdb3'),
    ('models/OCR/paddleocr_torch/te_PP-OCRv5_rec_infer.pth', 23977665, '85a7d6ce53591c288da965559dd1cebd12e26294554a6e72641d82070acdd5b9'),
    ('models/OCR/paddleocr_torch/th_PP-OCRv5_rec_infer.pth', 23971991, '9830b0c1532620851b6cdcd6bb2f4abed7d84ea70112497cc7d1e86a5885fdc6'),
    ('models/TabCls/paddle_table_cls/PP-LCNet_x1_0_table_cls.onnx', 6776877, 'c84bf1d79c1c74d534b5b12adb14dd12151c42f7ae3e4be4f1042b830f80b949'),
    ('models/TabRec/SlanetPlus/slanet-plus.onnx', 7758305, 'd57a942af6a2f57d6a4a0372573c696a2379bf5857c45e2ac69993f3b334514b'),
    ('models/TabRec/UnetStructure/unet.onnx', 8335007, '0ea48d3a17e35ef5c2e498a5e799566073234d39b1079ca21d9f4fafe73c6d20'),
]


def _emit(msg):
    print(msg, flush=True)


def step(tag, msg):
    _emit(f"[{tag}] {msg}")


def comp(cid, status, detail):
    """组件状态事件：[comp] id|status|detail（GUI 组件清单可视化）。
    status: wait 待安装 / installing 安装中 / ok 已就绪 / fail 失败。"""
    _emit(f"[comp] {cid}|{status}|{detail}")


# 暂停标志文件路径（GUI 写入/删除；存在即暂停）。由 main() 按 --pause-file 设置。
PAUSE_FILE = None


def _pause_gate():
    """暂停检查点：标志文件存在则阻塞等待，直至被删除（GUI「继续」）。"""
    if not PAUSE_FILE or not os.path.isfile(PAUSE_FILE):
        return
    step("pause", "已暂停（等待安装器窗口点击「继续」）")
    while os.path.isfile(PAUSE_FILE):
        time.sleep(0.5)
    step("pause", "已恢复，继续安装")


def _write_state(root, steps):
    """记录已完成阶段，供安装器在「停止」时判断哪些半成品需要清理。"""
    try:
        with open(os.path.join(root, ".install_state.json"), "w", encoding="utf-8") as f:
            json.dump({"steps": steps}, f, ensure_ascii=False)
    except OSError:
        pass


def _read_state_steps(root):
    try:
        with open(os.path.join(root, ".install_state.json"), encoding="utf-8") as f:
            return set(json.load(f).get("steps", []))
    except Exception:
        return set()


def precheck(root):
    """安装开始时输出各组件预检状态：系统已有→ok（断点续传可见），缺失→wait。

    只做秒级探测（存在性 + 大小匹配）；完整 sha256 校验留给下载阶段。
    返回 dict（models 已就绪计数），供下载阶段复用。"""
    # uv：本机查找（缺失回退 pip，不阻塞安装）
    has_uv = False
    if base is not None:
        try:
            has_uv = bool(base.find_uv())
        except Exception:
            pass
    comp("uv", "ok", "已检测（高速安装引擎）" if has_uv else "未检测到，将使用 pip（较慢）")

    # 应用主程序
    app_dir = os.path.join(root, "MinerU文档解析")
    if os.path.isfile(os.path.join(app_dir, "MinerU文档解析.exe")):
        n = sum(len(fns) for _, _, fns in os.walk(app_dir))
        comp("app", "ok", f"已存在（{n} 个文件）")
    else:
        comp("app", "wait", "待复制")

    # venv + 依赖
    vpy = os.path.join(root, "runtime", "venv", "Scripts", "python.exe")
    sp = os.path.join(root, "runtime", "venv", "Lib", "site-packages")
    if os.path.isfile(vpy) and os.path.isdir(sp):
        if "deps" in _read_state_steps(root):
            comp("venv", "ok", "环境与依赖已就绪（跳过）")
        else:
            comp("venv", "wait", "已存在，需补装依赖")
    else:
        comp("venv", "wait", "待创建（含约 110 个依赖包）")

    # CUDA：预检只报探测结果；状态仍 wait（torch 在依赖阶段之后才决策）
    gpu = None
    if base is not None:
        try:
            gpu = base.detect_gpu()
        except Exception:
            gpu = None
    if gpu:
        comp("cuda", "wait", f"检测到 {gpu.get('name') or 'NVIDIA 显卡'}（将装 CUDA 版）")
    else:
        comp("cuda", "wait", "未检测到独显（将使用 CPU 模式）")

    # 模型
    kit = os.path.join(root, "runtime", "models_cache", "models",
                       "OpenDataLab--PDF-Extract-Kit-1.0", "snapshots", "master")
    have = 0
    for fp, size, _sha in MODEL_FILES:
        try:
            if os.path.isfile(os.path.join(kit, fp.replace("/", os.sep))) \
                    and os.path.getsize(os.path.join(kit, fp.replace("/", os.sep))) == size:
                have += 1
        except OSError:
            pass
    total = len(MODEL_FILES)
    if have >= total:
        comp("models", "ok", f"{have}/{total} 文件已就绪（断点续传）")
    elif have:
        comp("models", "wait", f"{have}/{total} 文件已就绪，还需下载 {total - have}")
    else:
        comp("models", "wait", "待下载（约 2.4GB）")

    # 桌面快捷方式
    lnk = os.path.isfile(os.path.join(_desktop_dir(), "MinerU 文档解析.lnk"))
    comp("shortcut", "ok" if lnk else "wait", "已存在" if lnk else "待创建")
    return {"models_have": have}


def _file_sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _file_ok(path, size, sha):
    try:
        if not os.path.isfile(path) or os.path.getsize(path) != size:
            return False
        return not sha or _file_sha256(path) == sha
    except OSError:
        return False


class _Counter:
    def __init__(self):
        self.bytes = 0
        self.files = 0
        self.lock = threading.Lock()
        self.t0 = time.time()

    def add(self, n):
        with self.lock:
            self.bytes += n

    def done_file(self):
        with self.lock:
            self.files += 1

    def speed(self):
        return self.bytes / 1048576 / max(time.time() - self.t0, 0.01)


def _safe_unlink(path):
    try:
        if os.path.exists(path):
            os.remove(path)
    except OSError:
        pass


def _model_url(src, fp):
    """源名 → 该文件的下载 URL（fp 为仓库内相对路径）。"""
    if src == _HFM_SRC:
        return _HFM_DL.format(fp=urllib.parse.quote(fp))
    return _MS_DL.format(fp=urllib.parse.quote(fp))


def download_models(root, vpy=None, threads=_DL_THREADS_DEFAULT):
    """多源竞速下载 pipeline 模型（40 文件 ≈ 2.42GB）并做 sha256 完整性校验。

    - 源链 modelscope → hf-mirror：开始时并发测速（~5s）选最快源作主源；
      测速全失败回退固定序（绝不因此中止）
    - 大文件（≥100MB）逐文件竞速择优；段级停滞 >30s 自动换源 + 断点续传
    - 已存在且校验通过的文件自动跳过（断点续传）
    - [model]/[mbeat]/[comp] 事件协议与旧版保持一致
    """
    try:
        import fastdl
    except ImportError:
        step("model", "错误：缺少 fastdl 下载引擎")
        comp("models", "fail", "下载引擎缺失")
        return False

    kit = os.path.join(root, "runtime", "models_cache", "models",
                       "OpenDataLab--PDF-Extract-Kit-1.0", "snapshots", "master")
    total_files = len(MODEL_FILES)
    total_bytes = sum(s for _, s, _ in MODEL_FILES)

    pending, ok_existing = [], 0
    for fp, size, sha in MODEL_FILES:
        dest = os.path.join(kit, fp.replace("/", os.sep))
        if _file_ok(dest, size, sha):
            ok_existing += 1
        else:
            pending.append((fp, size, sha))

    if ok_existing:
        step("model", f"已存在且校验通过 {ok_existing}/{total_files} 个文件（断点续传生效）")
    if not pending:
        step("model", f"模型完整（{total_files} 文件 · {total_bytes / 1024 ** 3:.2f} GB），跳过下载")
        comp("models", "ok", f"已就绪（{total_files} 文件 · "
                             f"{total_bytes / 1024 ** 3:.2f} GB，断点续传）")
        return True

    pend_gb = sum(s for _, s, _ in pending) / 1024 ** 3
    pend_bytes = sum(s for _, s, _ in pending)

    # 源链测速：用首个大文件的 GET 前缀读流（无 Range 依赖），失败回退固定序
    sources = [_MS_SRC, _HFM_SRC]
    step("model", "并发测速模型下载源（约 5s）...")
    probe_fp = next((fp for fp, s, _ in MODEL_FILES if s > 100 << 20),
                    MODEL_FILES[0][0])
    ranked = fastdl.probe(sources, lambda s: _model_url(s, probe_fp),
                          probe_bytes=1 << 20, window=5.0, timeout=8)
    if ranked:
        for name, mbps in ranked:
            step("model", "测速 %s：%s" % (name, "%.1f MB/s" % mbps if mbps > 0 else "不可达"))
        best = [n for n, v in ranked if v > 0]
        if best:
            sources = best + [s for s in sources if s not in best]

    step("model", f"开始下载（{len(pending)} 个文件 · {pend_gb:.2f} GB · "
                  f"{threads} 线程 · 源链 {' → '.join(sources)}）")
    comp("models", "installing",
         f"{ok_existing}/{total_files} 已就绪 · 正在下载 {len(pending)} 个")

    counter = _Counter()          # 完成文件计数（字节进度用 dl.counter）

    def on_event(kind, *a):
        if kind == "done":
            key, ok = a
            counter.done_file()
            done = ok_existing + counter.files
            if ok:
                step("model", f"({done}/{total_files}) {key} · 平均 {dl.counter.speed():.1f} MB/s")
            else:
                step("model", f"({done}/{total_files}) 失败：{key}")
        elif kind == "race":
            key, winner = a
            step("model", f"竞速择优 {key.rsplit('/', 1)[-1]} → {winner}")
        elif kind == "switch":
            key, old, new = a
            step("model", f"换源续传 {key.rsplit('/', 1)[-1]}：{old} → {new}")
        elif kind == "retry":
            n, rnd = a
            step("model", f"第 {rnd} 轮重试 {n} 个失败文件 ...")

    dl = fastdl.Downloader(
        sources, _model_url, threads=threads, seg_size=32 << 20,
        race_min=100 << 20, stall=30.0, on_event=on_event,
        pause_check=_pause_gate)
    for fp, size, sha in pending:
        dl.add(fp, os.path.join(kit, fp.replace("/", os.sep)), size, sha)

    # 心跳线程：每 3s 输出字节级进度事件，GUI 实时显示已下载量/速度/剩余时间
    _hb_stop = threading.Event()

    def _heartbeat():
        while not _hb_stop.wait(3.0):
            if PAUSE_FILE and os.path.isfile(PAUSE_FILE):
                continue
            names = ",".join(os.path.basename(k) for k in dl.active_names()[:3])
            got = dl.counter.bytes
            step("mbeat", "%d/%d|%.4f|%.2f|%.2f|%.1f|%s" % (
                ok_existing + counter.files, total_files,
                min(got / max(pend_bytes, 1), 1.0),
                got / 1024 ** 3, pend_bytes / 1024 ** 3,
                dl.counter.speed(), names))

    threading.Thread(target=_heartbeat, daemon=True).start()
    try:
        dl.run_with_retry(rounds=3)
    finally:
        _hb_stop.set()

    # 最终完整性校验（引擎内已逐文件 sha 校验，这里兜底复核）
    bad = [fp for fp, size, sha in MODEL_FILES
           if not _file_ok(os.path.join(kit, fp.replace("/", os.sep)), size, sha)]
    if bad:
        step("model", f"完整性校验未通过 {len(bad)}/{total_files} 个文件，"
                      f"请重新运行安装（已完成文件会自动续传）")
        comp("models", "fail", f"校验未通过 {len(bad)} 个文件（重跑可续传）")
        return False
    step("model", f"模型下载完成，sha256 完整性校验全部通过"
                  f"（{total_files} 文件 · {total_bytes / 1024 ** 3:.2f} GB）")
    comp("models", "ok", f"已就绪（{total_files} 文件 · "
                         f"{total_bytes / 1024 ** 3:.2f} GB）")
    return True


def predownload_torch(root, threads=_DL_THREADS_DEFAULT):
    """GPU 机器在装依赖前预下载 CUDA torch/torchvision wheel（约 3GB）到
    runtime/wheel_cache/，随后 finalize_torch 直接离线安装（免联网、免镜像慢速）。
    无 GPU / 引擎缺失 / 下载失败 → 返回 None（回退联网安装）。"""
    if base is None:
        return None
    try:
        gpu = base.detect_gpu()
    except Exception:
        gpu = None
    if not gpu:
        comp("cuda", "wait", "未检测到独显（将使用 CPU 模式，跳过预下载）")
        return None
    try:
        import fastdl
    except ImportError:
        return None
    cu = base.pick_cuda(gpu.get("driver_cuda", ""))
    comp("cuda", "installing",
         f"检测到 {gpu.get('name') or 'NVIDIA 显卡'}（将装 CUDA 版）")
    step("torch", f"检测到 {gpu.get('name')} → 预下载 CUDA {cu} torch/torchvision wheel"
                  f"（多源竞速，约 3GB）...")
    _pause_gate()

    cache = os.path.join(root, "runtime", "wheel_cache")
    os.makedirs(cache, exist_ok=True)
    mirrors = [t.format(cu=cu) for t in base.PYTORCH_INDEXES]

    def url_of(src, key):
        return src + "/" + base.TORCH_WHEELS[key].format(cu=cu).replace("+", "%2B")

    def on_event(kind, *a):
        if kind == "race":
            step("torch", "竞速择优 %s → %s" % (a[0], a[1]))
        elif kind == "switch":
            step("torch", "换源续传：%s → %s" % (a[1], a[2]))
        elif kind == "retry":
            step("torch", "第 %d 轮重试 %d 个 wheel ..." % (a[1], a[0]))

    dl = fastdl.Downloader(
        mirrors, url_of, threads=threads, seg_size=32 << 20,
        race_min=200 << 20, stall=30.0, on_event=on_event,
        pause_check=_pause_gate)
    for key in base.TORCH_WHEELS:
        dl.add(key, os.path.join(cache, base.TORCH_WHEELS[key].format(cu=cu)))

    # 心跳：字段与 [mbeat] 一致，GUI 驱动 torch 预下载活动行
    _hb_stop = threading.Event()

    def _heartbeat():
        while not _hb_stop.wait(3.0):
            if PAUSE_FILE and os.path.isfile(PAUSE_FILE):
                continue
            names = ",".join(dl.active_names()[:2])
            got = dl.counter.bytes
            total_b = sum(f.size or 0 for f in dl._files)  # HEAD 探测后有值
            step("theat", "%d/%d|%.4f|%.2f|%.2f|%.1f|%s" % (
                len(dl.files_ok()), len(base.TORCH_WHEELS),
                min(got / max(total_b, 1), 1.0),
                got / 1024 ** 3, total_b / 1024 ** 3,
                dl.counter.speed(), names))

    threading.Thread(target=_heartbeat, daemon=True).start()
    try:
        ok_keys, fail_keys = dl.run_with_retry(rounds=2)
    finally:
        _hb_stop.set()

    if fail_keys:
        step("torch", f"警告：{len(fail_keys)} 个 wheel 预下载失败"
                      f"（{', '.join(fail_keys)}），稍后回退联网安装")
        comp("cuda", "wait", "torch 预下载未完成（将联网安装）")
        return None
    step("torch", "CUDA torch wheel 预下载完成 → 依赖安装后将离线装载（不再联网）")
    return cache


def copy_runtime_files(src, root):
    """把应用主体从资源源复制到安装目录。
    形态：onedir 启动器（WebUI 字节码内嵌其 _internal，源码不落盘）+ 卸载器。"""
    step("copy", "复制主程序文件 ...")
    comp("app", "installing", "正在复制主程序文件 …")
    _pause_gate()
    app_src = os.path.join(src, "MinerU文档解析")
    app_dst = os.path.join(root, "MinerU文档解析")
    if not os.path.isdir(app_src):
        comp("app", "fail", "安装包缺少主程序目录")
        raise RuntimeError("安装包缺少主程序目录（MinerU文档解析）")
    shutil.copytree(app_src, app_dst,
                    ignore=shutil.ignore_patterns("__pycache__", "logs", "old"))
    uninstaller = os.path.join(src, "卸载MinerU.exe")
    if os.path.isfile(uninstaller):
        shutil.copy2(uninstaller, os.path.join(root, "卸载MinerU.exe"))
    guide = os.path.join(src, "使用说明.html")
    if os.path.isfile(guide):
        shutil.copy2(guide, os.path.join(root, "使用说明.html"))
    n = sum(len(fns) for _, _, fns in os.walk(app_dst))
    comp("app", "ok", f"已就绪（{n} 个文件）")
    step("copy", "主程序文件复制完成")


def write_install_manifest(root):
    """写入安装清单（版本 + 逐文件 sha256），供远程对比修复/升级（P4）使用。"""
    entries = {}
    for rel in ("MinerU文档解析", "使用说明.html", "卸载MinerU.exe"):
        p = os.path.join(root, rel)
        if os.path.isfile(p):
            entries[rel] = _file_sha256(p)
        elif os.path.isdir(p):
            for dp, _dns, fns in os.walk(p):
                for fn in fns:
                    fp = os.path.join(dp, fn)
                    key = os.path.relpath(fp, root).replace(os.sep, "/")
                    entries[key] = _file_sha256(fp)
    manifest = {
        "version": APP_VERSION,
        "created": time.strftime("%Y-%m-%d %H:%M:%S"),
        "files": entries,
    }
    try:
        with open(os.path.join(root, ".install_manifest.json"), "w", encoding="utf-8") as f:
            json.dump(manifest, f, ensure_ascii=False, indent=1)
        step("manifest", f"安装清单已生成（{len(entries)} 个文件 · 版本 {APP_VERSION}）")
    except OSError as e:
        step("manifest", f"警告：安装清单写入失败：{e}")


def ensure_venv_deps(root, mirror, local_torch_dir):
    """创建 venv、装依赖、GPU 决策、写配置；复用 install_mineru_uv.Installer。"""
    if base is None:
        step("deps", "错误：缺少 install_mineru_uv.py，无法继续")
        comp("venv", "fail", "安装脚本缺失")
        return False
    diag = base.Diagnostics(root)
    ins = base.Installer(root, mirror, diag, local_torch_dir=local_torch_dir)
    _pause_gate()
    step("venv", "创建虚拟环境 ...")
    comp("venv", "installing", "创建虚拟环境 …")
    if not ins.ensure_venv():
        step("venv", "错误：创建虚拟环境失败")
        comp("venv", "fail", "虚拟环境创建失败")
        return False
    _pause_gate()
    step("deps", "安装依赖（镜像加速，首次较慢）...")
    comp("venv", "installing", "下载并安装依赖（约 110 个包）…")
    if not ins.install_deps():
        step("deps", "错误：依赖安装失败")
        comp("venv", "fail", "依赖安装失败")
        return False
    comp("venv", "ok", "环境与依赖已就绪")
    _pause_gate()
    step("gpu", "GPU 探测与 CUDA torch 决策 ...")
    comp("cuda", "installing", "探测显卡与 CUDA 决策 …")
    ok_torch, tinfo = ins.finalize_torch()
    step("cfg", "生成 mineru.json ...")
    ins.write_config()
    try:
        diag.write()
    except Exception:
        pass
    dev = "cuda" if (tinfo and tinfo.get("avail")) else "cpu"
    if dev == "cuda":
        comp("cuda", "ok", f"CUDA 加速已启用（{tinfo.get('dev', 'NVIDIA')} 卡）")
    else:
        comp("cuda", "ok", "CPU 模式（未检测到可用 GPU）")
    step("gpu", f"设备模式: {dev}")
    return True


def _desktop_dir():
    """真实桌面目录。优先读注册表 User Shell Folders\Desktop——桌面可能被
    OneDrive 或用户重定向到 D: 等其它位置（%USERPROFILE%\\Desktop 会不存在）；
    读取失败回退默认拼接。"""
    try:
        import winreg
        with winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                r"Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders") as k:
            val, _ = winreg.QueryValueEx(k, "Desktop")
        if val:
            p = os.path.expandvars(val)
            if os.path.isdir(p):
                return p
    except Exception:
        pass
    return os.path.join(os.path.expanduser("~"), "Desktop")


def create_shortcut(root):
    """生成桌面快捷方式，指向应用主程序 exe（图标内嵌于 exe）。"""
    exe = os.path.join(root, "MinerU文档解析", "MinerU文档解析.exe")
    if not os.path.isfile(exe):
        step("shortcut", "警告：未找到主程序 exe，跳过快捷方式")
        comp("shortcut", "fail", "未找到主程序 exe")
        return False
    comp("shortcut", "installing", "正在创建桌面快捷方式 …")
    icon = exe  # 图标已内嵌于 exe 资源
    desktop = _desktop_dir()
    lnk = os.path.join(desktop, "MinerU 文档解析.lnk")
    ps = (
        "$ws = New-Object -ComObject WScript.Shell;"
        f"$sc = $ws.CreateShortcut({lnk!r});"
        f"$sc.TargetPath = {exe!r};"
        f"$sc.WorkingDirectory = {os.path.dirname(exe)!r};"
        f"$sc.IconLocation = {icon!r};"
        "$sc.Description = 'MinerU 文档解析';"
        "$sc.Save()"
    )
    try:
        # errors="replace"：PowerShell 出错时输出本地编码中文，强制 UTF-8 的环境下
        # 解码会抛 UnicodeDecodeError，替换字符兜底保证快捷方式逻辑不受影响
        r = subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                       capture_output=True, text=True, timeout=60,
                       errors="replace", creationflags=_NO_WINDOW)
        ok = r.returncode == 0 and os.path.isfile(lnk)
        step("shortcut", "桌面快捷方式已创建" if ok else "快捷方式创建失败")
        comp("shortcut", "ok" if ok else "fail",
             "已创建" if ok else "创建失败（可手动启动 exe）")
        return ok
    except Exception as e:
        step("shortcut", f"快捷方式创建失败: {e}")
        comp("shortcut", "fail", f"创建失败: {e}")
        return False


def _write_config_extra(root, extra):
    """读-改-写 mineru.json，合并 extra 键（如 download-threads）。"""
    path = os.path.join(root, "mineru.json")
    cfg = {}
    try:
        with open(path, encoding="utf-8") as f:
            cfg = json.load(f)
    except Exception:
        cfg = {}
    cfg.update(extra)
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
    except OSError:
        pass


def main():
    global PAUSE_FILE
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", required=True)
    ap.add_argument("--src", required=True)
    ap.add_argument("--mirror", default=None)
    ap.add_argument("--local-torch-dir", default=None)
    ap.add_argument("--result", default=None)
    ap.add_argument("--skip-model", action="store_true")
    ap.add_argument("--no-shortcut", action="store_true")
    ap.add_argument("--dl-threads", type=int, default=_DL_THREADS_DEFAULT,
                    help="下载线程数（默认 16，范围 4-64）")
    ap.add_argument("--pause-file", default=None,
                    help="暂停标志文件：存在即在各检查点暂停，删除后继续")
    args = ap.parse_args()

    root = os.path.abspath(args.root)
    src = os.path.abspath(args.src)
    threads = min(64, max(4, args.dl_threads or _DL_THREADS_DEFAULT))
    os.makedirs(root, exist_ok=True)
    PAUSE_FILE = os.path.abspath(args.pause_file) if args.pause_file else None
    result = {"ok": False, "steps": [], "error": ""}
    torch_cache = None

    try:
        precheck(root)
        copy_runtime_files(src, root)
        result["steps"].append("copy")
        _write_state(root, result["steps"])

        # GPU 机器先多源竞速预下载 CUDA torch wheel（~3GB），依赖装完即离线装载
        torch_cache = predownload_torch(root, threads)
        if not args.local_torch_dir and torch_cache:
            args.local_torch_dir = torch_cache

        if not ensure_venv_deps(root, args.mirror, args.local_torch_dir):
            raise RuntimeError("环境安装失败")
        result["steps"].append("deps")
        _write_state(root, result["steps"])
        _write_config_extra(root, {"download-threads": threads})

        vpy = os.path.join(root, "runtime", "venv", "Scripts", "python.exe")
        if not args.skip_model:
            if not download_models(root, vpy, threads=threads):
                raise RuntimeError("模型下载失败")
        result["steps"].append("model")
        _write_state(root, result["steps"])

        if not args.no_shortcut:
            result["shortcut_ok"] = create_shortcut(root)
        result["steps"].append("shortcut")

        write_install_manifest(root)
        result["ok"] = True
        step("done", "安装完成！")
    except Exception as e:  # noqa: BLE001
        result["error"] = str(e)
        step("error", f"安装失败: {e}")
    finally:
        if args.result:
            with open(args.result, "w", encoding="utf-8") as f:
                json.dump(result, f, ensure_ascii=False, indent=2)
        # 成功后清理 torch 预下载缓存（~3GB）；失败保留供重跑续传
        if result.get("ok") and torch_cache:
            shutil.rmtree(torch_cache, ignore_errors=True)
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
