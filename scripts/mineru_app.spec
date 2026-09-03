# -*- mode: python ; coding: utf-8 -*-
# MinerU 应用主程序打包配置（onedir / 窗口模式）
# 产物：release/MinerU文档解析/ —— 托盘启动器 exe + _internal
#   _internal/webui/app.pyc    WebUI 编译字节码（源码不落盘）
#   _internal/webui/static/    浏览器端静态资源
# 构建命令（工作目录须为项目根）：
#   runtime\venv\Scripts\python.exe -m PyInstaller --clean --noconfirm --distpath release --workpath .tmp\pyinstaller\app-build scripts\mineru_app.spec

import os
import py_compile

# spec 位于 scripts/，上推一级得到项目根
ROOT = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(SPEC)), ".."))

# 字节码暂存目录（项目 .tmp/ 内，随 --clean 清理；.gitignore 已忽略）
STAGING = os.path.join(ROOT, ".tmp", "pyinstaller", "app-staging")


def _webui_datas():
    """编译应用源码 → .pyc 打入 _MEIPASS（源码不落盘）：
      webui/app.pyc          WebUI 入口（托盘拉起）
      updater.pyc            远程修复/升级器（托盘「检查更新」菜单以 venv python 拉起）
      fastdl.pyc             多源下载引擎（updater 依赖，与 updater 同级可 import）
    构建解释器即 runtime/venv（3.11），与目标 venv 同 minor 版本，字节码兼容。"""
    os.makedirs(STAGING, exist_ok=True)
    d = []
    for src, rel in (
        (os.path.join(ROOT, "src", "webui", "app.py"), "webui/app.pyc"),
        (os.path.join(ROOT, "src", "installer", "updater.py"), "updater.pyc"),
        (os.path.join(ROOT, "scripts", "fastdl.py"), "fastdl.pyc"),
    ):
        pyc = os.path.join(STAGING, os.path.basename(rel))
        py_compile.compile(src, cfile=pyc, dfile=os.path.basename(rel),
                           doraise=True, optimize=0)
        d.append((pyc, os.path.dirname(rel) or "."))
    static = os.path.join(ROOT, "src", "webui", "static")
    if os.path.isdir(static):
        d.append((static, os.path.join("webui", "static")))
    return d


a = Analysis(
    [os.path.join(ROOT, "src", "tray", "mineru_tray.py")],
    pathex=[ROOT],
    binaries=[],
    datas=_webui_datas(),
    hiddenimports=[
        "pystray",
        "pystray._win32",
        "PIL",
        "PIL.Image",
        "PIL.ImageDraw",
        "PIL.ImageFont",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    # numpy 是 PyInstaller 分析 PIL 时顺带打入的（托盘仅用 pystray+PIL 基础功能，
    # WebUI 运行于目标机 venv 不依赖 _internal）；排除后 dist 最大文件 <12MB，
    # 三个镜像（含 jsdelivr 20MB 上限）均稳定可用
    excludes=["tkinter", "numpy"],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="MinerU文档解析",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=[os.path.join(ROOT, "src", "tray", "icon.ico")],
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="MinerU文档解析",
)
