# mineru-mod

MinerU 本地解析工具（WebUI + 系统托盘）修改版。

基于开源项目 [MinerU](https://github.com/opendatalab/MinerU) 二次开发：提供本机网页界面进行 PDF / 图片批量解析，支持任务队列、进度监控、批次管理，并通过系统托盘常驻后台。

## 功能

- 网页批量解析 PDF / 图片（PNG/JPG/JPEG/WEBP/BMP/TIFF/SVG）
- 任务队列：执行过程中可继续添加文件，无需中断；显示 a/N 进度
- 批次管理：输出按批次分文件夹，支持命名 / 重命名，新任务自动切换新批次
- 引擎状态可视化：未启动 / 启动中 / 运行中 / 空闲中（空闲 30s 自动释放）
- 系统托盘常驻，WebUI 与托盘单实例运行
- 解析参数（语言 / 后端 / 精度 / 最大页数等）默认值可在设置中配置并持久化

## 交付形态（开发者 vs 小白分层）

本仓库面向**开发者**。面向小白（使用者）的最终交付物由一键安装器打包生成，**使用者不接触任何源码**：

| 交付物 | 位置 | 面向 | 说明 |
|--------|------|------|------|
| `MinerU安装.exe` | `release/` | 小白 | 一键安装器：引导 Python → 装依赖 → 下载模型 → 生成桌面快捷方式 |
| `使用说明.html` | `release/` | 小白 | 图文使用说明（安装 / 启动 / 常见问题） |
| `mineru_tray\mineru_tray.exe` | `release/` | 小白 | 桌面快捷方式指向的启动器（托盘常驻 + 自动开网页） |
| 本仓库源码 + README | 仓库根 | 开发者 | 维护 / 二次开发使用 |

## 目录结构（代码 / 运行时 / 交付物分层）

```
.
├── src/                # 源码
│   ├── webui/          #   Web 后端 + 前端（app.py + static/）
│   ├── tray/           #   系统托盘启动器（mineru_tray.py + icon.ico）
│   └── installer/      #   一键安装器（installer_gui.py + install_flow.py）✅ 小白分发入口
├── scripts/            # 构建 / 安装脚本
│   ├── install_mineru_uv.py / .bat   # 环境安装核心脚本
│   ├── download_torch_wheels.py      # CUDA torch wheel 多线程下载
│   ├── mineru_tray.spec              # 托盘启动器 PyInstaller 打包配置
│   └── installer_spec.pyinstaller.spec  # 安装器 PyInstaller 打包配置
├── tests/              # 测试（结构 / smoke 回归）
├── release/            # 交付物（打包产物：exe / 使用说明）
├── runtime/            # 运行时，不入库（可整体删除重建）
│   ├── venv/           #   虚拟环境
│   ├── models_cache/   #   模型缓存（pipeline / VLM）
│   ├── torch_wheels/   #   预下载 torch 轮子（离线安装）
│   └── _data/          #   运行数据（uploads / outputs / logs）
└── docs/               # 文档
```

## 使用

### 小白（使用者）：安装 exe

1. 双击 `MinerU安装.exe`，选择安装目录（默认 `C:\MinerU_App`）
2. 点击【开始安装】，等待自动完成（首次约 10~20 分钟，自动装依赖 + 下载模型）
3. 桌面出现「MinerU 文档解析」快捷方式，双击即启动并自动打开网页

> 安装器会自动检测 Python（缺则从国内镜像引导安装），全程无需手动操作，详见 `使用说明.html`。
> 安装全程有实时反馈：当前活动行显示正在下载的包名/大小（第 N/M 个包）、模型下载速度与预计剩余时间、总耗时秒表、进度条与阶段图标动画，不会出现"无提示假死"观感。

### 开发者：源码一键启动（exe）

双击 `release\mineru_tray\mineru_tray.exe`：后台自动拉起解析服务 → 服务就绪后自动打开浏览器网页 → 系统托盘常驻。

- 托盘【打开浏览器界面】：在默认浏览器打开 MinerU 可视化解析界面
- 托盘【重启服务】：停止并重启后台服务
- 托盘【退出并停止服务】：优雅停止后台服务（先释放解析引擎 / GPU 显存再退出）

> exe 需放在项目根附近（会自动向上查找含 `runtime/venv` 与 `src/webui/app.py` 的目录），运行日志生成在 `runtime/_data/logs/` 下。

### 源码运行

1. 运行安装脚本完成依赖与模型准备（国内网络建议配合镜像源）
2. 启动 WebUI，浏览器访问本机端口
3. 拖拽 / 选择文件 → 配置解析参数 → 开始解析

### 重新打包 exe

**托盘启动器**（`release\mineru_tray\mineru_tray.exe`）：
```powershell
runtime\venv\Scripts\python.exe -m PyInstaller --clean --noconfirm --distpath release --workpath .tmp\pyinstaller\build scripts\mineru_tray.spec
```

**一键安装器**（`release\MinerU安装.exe`，自动打包 src/webui、src/tray、release/mineru_tray、scripts 安装脚本 入 _MEIPASS）：
```powershell
runtime\venv\Scripts\python.exe -m PyInstaller --clean --noconfirm --distpath release --workpath .tmp\pyinstaller\installer-build scripts\installer_spec.pyinstaller.spec
```

> 打包安装器前需先构建托盘启动器（安装器会把 `release/mineru_tray/` 拷入目标机）。完整交付命令见脚本内注释。

### 预下载 torch wheel（避免安装时联网慢）

```powershell
runtime\venv\Scripts\python.exe scripts\download_torch_wheels.py --threads 8
```

- 多线程分段下载（HTTP Range 并发）+ 多镜像自动回退（上交大 → 官方 → 阿里云 → 中科大）
- 下载到 `runtime\torch_wheels/`，安装器 `install_mineru_uv.py` 会自动探测并离线优先安装 CUDA torch

## 说明

- 本仓库为**私有**，默认不公开；如要公开需单独决定并记录理由。
- 本仓库为 MinerU 开源项目的修改版（二次开发），上游：<https://github.com/opendatalab/MinerU>。
