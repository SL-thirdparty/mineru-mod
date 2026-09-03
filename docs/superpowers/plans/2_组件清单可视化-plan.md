# P2 组件清单可视化 执行计划

> 依据：`docs/superpowers/planning/交付精简与升级体系-master-plan.md` P2 节
> 代码级别：contract（AI 内联执行）
> 状态：实施中（2026-09-03）

## 目标

用户点击「开始安装」后立即看到**组件清单**：系统已有什么、缺什么、什么正在装。
安装全程每个组件的状态变化实时可见，支持展开查看明细（依赖逐包进度 / 模型文件级状态）。

## 组件模型（7 项）

| id       | 显示名称           | 预检逻辑（点击开始后立即）                              | 安装动作来源                   |
| -------- | -------------- | ----------------------------------------------- | ------------------------ |
| python   | Python 3.11 运行时 | GUI `_find_python()`（探测已有 3.11 → ok；否则自动下载）     | GUI `_download_python()` |
| uv       | 安装引擎 uv        | `base.find_uv()` 本机查找（不安装，缺失回退 pip）             | 无（环境状态展示）               |
| app      | 应用主程序          | `<root>\MinerU文档解析\MinerU文档解析.exe` 存在 → ok        | `copy_runtime_files`     |
| venv     | 运行环境与依赖        | venv 完整且上次依赖装完（`.install_state.json` 含 deps）→ ok | `ensure_venv` + `install_deps` |
| cuda     | GPU 加速          | nvidia-smi 可用 → ok（CUDA 版）；无独显 → ok（CPU 模式）     | `finalize_torch`         |
| models   | 解析模型           | 40 文件存在+大小匹配计数 n/40（全在 → ok 断点续传）             | `download_models`        |
| shortcut | 桌面快捷方式         | 桌面 lnk 存在 → ok                                   | `create_shortcut`        |

状态集合：`wait`（待安装）/ `installing`（安装中）/ `ok`（已就绪）/ `fail`（失败）。

## 2.1 `[comp]` 事件协议（install_flow.py）

### 契约

```
行格式：[comp] <id>|<status>|<detail>
解析：GUI _run 的既有 `[tag] rest` 正则天然兼容（tag=comp）
分发：q.put(("comp", rest)) → _poll → _handle_comp(rest)
```

### 新增函数

```python
def comp(cid, status, detail):
    """输出组件状态事件。"""
    step("comp", f"{cid}|{status}|{detail}")

def precheck(root):
    """安装开始时输出全部组件预检状态（断点续传可见）。
    返回 dict 供 main() 复用（如 models 已就绪计数）。"""
    # uv: base.find_uv() → ok "已检测（高速安装引擎）" / ok "未检测到，将使用 pip（较慢）"
    # app: exe 存在 → ok "已存在（N 个文件）" / wait "待复制"
    # venv: 完整+state含deps → ok "依赖已就绪（跳过）"
    #        完整但无完成标记 → wait "已存在，需校验补装依赖"
    #        不存在 → wait "待创建（含约 110 个依赖包）"
    # cuda: nvidia-smi → ok "检测到 NVIDIA 显卡（安装 CUDA 版）" / ok "未检测到独显（CPU 模式）"
    # models: 存在+大小匹配 → 全 40 → ok "40/40 已就绪（断点续传）"
    #                        部分 → wait "n/40 已就绪，还需下载 40-n"
    # shortcut: lnk 存在 → ok / wait "待创建"
```

### main() 流程钩子

| 流程点                | 钩子                                                     |
| ------------------ | ------------------------------------------------------ |
| main() 开头          | `precheck(root)`                                        |
| copy_runtime_files 前/后 | `comp("app","installing","正在复制主程序文件…")` → `ok "N 个文件"` |
| ensure_venv 前      | `comp("venv","installing","创建虚拟环境…")`                  |
| install_deps 期间    | GUI 端由 `[pkg]` 事件驱动 venv 详情（后端不发高频 comp）              |
| deps 完成后           | `comp("venv","ok","依赖安装完成（N 个包）")` / fail             |
| finalize_torch 前/后 | `comp("cuda","installing","探测显卡与 CUDA 决策…")` → ok       |
| download_models 前/后 | `comp("models","installing","n/40 · 已下载 x.xx GB")` → ok / fail（详情由 GUI 从 `[mbeat]` 刷新） |
| create_shortcut 前/后 | `comp("shortcut","installing",…)` → ok / fail           |

### GUI 端 python 组件

`_run()` 里 `_find_python()` 命中 → `q.put(("comp", "python|ok|系统 Python 3.11"))`；
未命中 → `q.put(("comp", "python|installing|未找到，自动下载安装中…"))`，下载成功后更新 ok。

## 2.2 GUI 组件面板

### 新文件 `src/installer/comp_panel.py`

```python
class CompPanel(tk.Frame):
    """组件清单面板：计数条 + 7 行组件卡片 + 可展开明细。"""

    COMPS = [(id, 名称, MDL2图标), ...]   # 上表 7 项固定顺序

    # 公开接口（GUI 事件驱动）
    def set_comp(cid, status, detail)    # 更新行状态徽章/详情/计数条
    def set_pkg_feed(name, size, idx, total)   # [pkg] → venv 明细区（展开时可见）
    def set_model_feed(done, total, got_gb, total_gb, speed, names)  # [mbeat] → models 明细
    def pulse()                          # installing 行呼吸动画（主 anim_tick 调用）
    def reset()                          # 重新安装时清零
```

布局契约（匹配现有青墨渐变卡片风格）：
- 头部：`组件清单` 标题 + 右侧计数 Pill（`7 项 · 已就绪 M · 安装中 K · 待安装 L`，fail 变红）
- 每行（高 30px）：状态圆点（绿=ok / 青=installing / 灰=wait / 红=fail）+ 名称 + Pill 徽章 + 右对齐详情（muted 色，超长省略）
- 点击行：展开/折叠明细子面板（venv → 最近包下载列表；models → 文件计数+GB+速度+当前文件）
- 明细数据即使折叠也持续更新（展开即可见最新值）
- installing 行：圆点呼吸动画（pulse）

### installer_gui.py 改动

- `_build_ui`：进度卡片内步骤条与活动行之间插入 `CompPanel`（grid row 紧凑排布）
- `_poll`：新增 `("comp", rest)` → `self._handle_comp(rest)` 解析 `id|status|detail` → `panel.set_comp`
- `_handle_pkg`：额外调用 `panel.set_pkg_feed(...)`（venv 明细 + 组件行详情 "第 n/N 个包"）
- `_handle_mbeat`：额外调用 `panel.set_model_feed(...)`
- `_anim_tick`：调用 `panel.pulse()`
- `start()`：调用 `panel.reset()`
- 窗口自适应：`_apply_adaptive_geometry` 已有 reqheight 机制，面板自然参与

### 打包

`comp_panel.py` 与入口同目录，PyInstaller modulegraph 自动收集；spec `hiddenimports` 显式加 `"comp_panel"` 兜底。

## 2.3 验证

1. **单测**（`tests/test_comp_events.py`，pytest，纯逻辑无 tk）：
   - `precheck`：临时 root 三态（空目录 / 半成品 venv / 完整态）→ 断言各组件 status 与 detail 关键词
   - `_handle_comp` 解析：合法/非法行（缺字段、未知 id）不抛异常
   - 计数聚合：构造 7 组件混合状态 → 已就绪/安装中/待安装计数正确
2. **GUI 事件流转**：实例化面板（tk 可用时）注入事件序列 → 断言行状态与计数条文本；无显示环境跳过（CI 用）
3. **真实界面**：本机跑安装器 → `.tmp/p2_app` 安装 → 全程截图核对各状态流转（wait→installing→ok）
4. **打包冒烟**：重打 exe → 启动验证 comp_panel 被正确收集（无 ImportError）

## 顺序与风险

顺序：2.1 → 2.2 → 2.3（协议先行，UI 消费）。

| 风险                      | 缓解                                   |
| ------------------------- | ------------------------------------ |
| 面板挤占日志区高度（7 行 ≈ 250px） | 行高 30px 紧凑 + 窗口自适应逻辑已存在；日志区 weight=1 可压缩 |
| 预检 sha256 全量校验耗时       | 预检只查存在性+大小（秒级）；完整 sha256 留给下载阶段     |
| nvidia-smi 探测慢（timeout） | 复用 install_mineru_uv 现有多路探测，限 timeout |
| 打包后 ImportError         | hiddenimports 显式列出 + 打包冒烟验证          |
