# P3 多源竞速下载 执行计划

> 依据：`docs/superpowers/planning/交付精简与升级体系-master-plan.md` P3 节（D3/D4 已拍板）
> 代码级别：contract（AI 内联执行）
> 状态：待实施（2026-09-03）

## 目标

安装全程（依赖 / torch 大件 / 模型）不再被单一慢源卡死：

- 安装开始时**并发测速**（~5s）选最快源作为主源
- 大文件**多源竞速**择优（>10MB 文件并发试 1MB）
- 下载中**停滞 >30s 自动换源**（段级轮转 + 段内断点续传）
- **全慢兜底**：速度榜非空时恒有最快者兜底；测速全失败回退固定优先序（D4：任何时刻至少一条活跃下载流）
- **线程数可配置**：默认 16、范围 4-64，全局统一调度（文件 × 分段），GUI 可设、持久化

## 模块布局

新引擎 `scripts/fastdl.py`（纯标准库，独立可测）：

- 与 `install_mineru_uv.py` 同目录 → 打包后同在 `_MEIPASS/scripts/`，三方共用：
  - `install_flow.py`（模型下载改造）
  - `install_mineru_uv.py`（pip/uv 镜像测速排序、torch 预下载调用）
  - `download_torch_wheels.py`（重写为引擎薄封装，保留 CLI）
- 打包：`installer_spec` datas 追加 `(scripts/fastdl.py, "scripts")`

## 3.1 源测速器

### 契约

```python
def probe(candidates, url_of, probe_bytes=1<<20, window=5.0, timeout=8):
    """并发测速：每源一个线程读 url_of(src) 最多 probe_bytes 或 window 秒。

    返回 [(name, mbps)] 按速度降序；失败源 mbps=0.0 保留在末尾（回退候选）；
    全部不可达返回 []（调用方回退固定优先序，绝不因此中止——D4 兜底）。
    无 Range 依赖：普通 GET 读流即停。"""
```

- PyPI 镜像探测 URL：`{src}/simple/torch/`（大 HTML，天然吞吐样本）
- 模型源探测 URL：取首个大模型文件的 Range 前缀
- torch 轮子探测 URL：轮子真实 URL 的 `Range: bytes=0-`（引擎内）

### 集成点

`install_mineru_uv.Installer` 新增 `probe_mirrors()`：`install_deps()` 开头调用
→ `mirror_sources()` 返回按测速降序（用户 `--mirror` 仍置顶，实测 403 的源自然沉底）。
测速行经 `[pkg]` 事件前缀输出（GUI 活动行可见"测速 aliyun 23.4 MB/s …"）。

## 3.2 通用多源下载器

### 契约

```python
class Downloader:
    """全局线程池（文件×分段统一调度）+ 逐源轮转 + 段级断点续传。

    sources: 有序源名列表（速度榜顺序）；url_of(src, key) -> 下载 URL
    threads: 全局池大小（4-64，D3）；seg_size: 大于此值切段
    race_min: 大于此值的文件先做 2-3 源 1MB 竞速择优
    stall: 单源无进展秒数（默认 30）
    """
    def __init__(self, sources, url_of, threads=16, seg_size=32<<20,
                 race_min=10<<20, stall=30.0, on_event=None): ...
    def add(self, key, dest, size, sha=None): ...   # size 未知时引擎先 HEAD
    def run(self): ...   # -> (ok_keys, fail_keys)
```

### 调度模型（防死锁的扁平任务）

```
全局池 N 线程，任务三类（同池，无嵌套等待）：
  小文件任务：整文件下载，逐源轮转
  竞速任务（≥race_min 且多源）：自起 2-3 个短命裸线程试 1MB → 胜者源
              → 立即提交该文件全部分段任务后返回（不等待）
  分段任务：Range 续传（part 文件大小即断点），
            当前源停滞 stall 秒 → 换下一源从断点续传
            文件最后一段完成者负责合并 + sha256 校验（原子计数）
失败文件串行重试一轮（沿用现有行为）；跨运行续传：已完文件按 sha 跳过，
未完文件以 .parts 目录残留段文件续传。
```

### 事件回调（驱动 GUI/日志）

`on_event(kind, *a)`：`probe`（速度榜）/ `race`（文件,胜者）/
`switch`（文件,旧源→新源）/ `progress`（累计字节）/ `done`（文件,ok）。

### 改造点

| 文件 | 改动 |
| --- | --- |
| `install_flow.download_models` | 用 Downloader 重写：源链 modelscope → hf-mirror（`/resolve/main/<fp>`，sha256 兜底防错文件）；`[mbeat]`/`[model]`/`[comp]` 事件协议不变 |
| `install_flow.main` | venv 前新增 **torch 大件预下载**：`detect_gpu()` 有卡 → 按 `pick_cuda` 用引擎预下载 torch/torchvision wheel 到 `<root>/runtime/wheel_cache/` → 作为 `local_torch_dir` 传入 → `finalize_torch` 离线安装（已是现成逻辑） |
| `download_torch_wheels.py` | 重写为 fastdl 薄封装（保留 CLI 参数） |
| 模型下载线程数 | `--dl-threads` 传入 Downloader |

## 3.3 线程池统一配置

- **安装器 GUI**：安装位置卡片下新增紧凑设置行「下载线程数」Spinbox（4-64，默认 16，提示文案"越大越快，占用越高"）；与安装路径一起持久化到 `%LOCALAPPDATA%\MinerU\installer.json`（`{path, dl_threads}`）
- **install_flow**：`--dl-threads N` 参数 → 引擎池大小；写 `"download-threads": N` 进安装根 `mineru.json`
- **WebUI**：`DEFAULT_CONFIG["download_threads"]=16`；启动时若 `mineru.json` 有 `download-threads` 则作种子值；`/api/config` 白名单 + clamp(4,64)；设置页加字段（P4 更新器消费此值）

## 3.4 uv 收尾

- `install_deps`：测速冠军作 uv/pip `--index-url`（3.1 集成）
- torch：`finalize_torch` 的联网回退顺序不变（预下载命中则根本不联网）；`uv pip install --find-links <wheel_cache>` 走既有 `torch_offline_install`

## 3.5 验证

1. **单测**（`tests/test_fastdl.py`，unittest，纯逻辑 + 本地 HTTP）：
   - probe：正常/慢源/不可达混合 → 速度榜排序与全失败回退
   - Downloader：本地 HTTP 服务模拟（慢源限速 / 停滞源 / 坏 404 源）→ 断言换源、断点续传、全慢兜底
   - 线程数边界：4/16/64 均可构造
2. **真实安装**：`.tmp` 沙箱根跑一次完整 install_flow（模型已就绪断点续传场景）→ 事件流与 GUI 面板核对
3. **GUI**：设置行交互 + 持久化重开生效；重打 exe 冒烟
4. **hf-mirror 可达性**：实测模型文件 200 + sha256 一致（不一致则从源链剔除，保 modelscope 单源 + 引擎轮转）

## 顺序与风险

顺序：3.1（probe）→ 3.2（Downloader）→ 3.3（配置贯通）→ 3.4（uv 集成）→ 3.5（验证）

| 风险 | 缓解 |
| --- | --- |
| hf-mirror 文件路径与 modelscope 不一致 | sha256 校验兜底；不一致自动降级单源 |
| 段级轮转与断点续传冲突（错位写入） | part 文件只在段内追加；合并前大小必须等于段长 |
| 预下载 torch 增加无 GPU 机器耗时 | 无卡跳过（仅 GPU 机器预下载） |
| 测速 5s 白屏期 | 走 [pkg] 事件实时显示每源速度 |
| 线程过高打满磁盘 IO | 上限 64 + 提示文案 |
