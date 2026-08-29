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

## 目录结构

- `webui/`：Web 前端 + 后端服务
- `tray/`：系统托盘程序（mineru_tray）
- `install_mineru_uv.bat` / `install_mineru_uv.py` / `install_new_machine.bat`：安装脚本
- `mineru_tray.spec`：PyInstaller 打包配置

## 使用

1. 运行安装脚本完成依赖与模型准备（国内网络建议配合镜像源）
2. 启动 WebUI，浏览器访问本机端口
3. 拖拽 / 选择文件 → 配置解析参数 → 开始解析

## 说明

- 本仓库为**私有**，默认不公开；如要公开需单独决定并记录理由。
- 本仓库为 MinerU 开源项目的修改版（二次开发），上游：<https://github.com/opendatalab/MinerU>。
