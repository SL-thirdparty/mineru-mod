# -*- mode: python ; coding: utf-8 -*-
# MinerU 一键安装器打包配置（onefile / 窗口模式）
# 构建命令（工作目录须为项目根）：
#   runtime\venv\Scripts\python.exe -m PyInstaller --clean --noconfirm --distpath release --workpath .tmp\pyinstaller\installer-build scripts\installer_spec.pyinstaller.spec
# 资源（release/MinerU文档解析 应用主体、卸载器、scripts 安装脚本）被打入 _MEIPASS，
# 安装时复制到目标目录。构建前需先产出 release/MinerU文档解析/ 与 release/卸载MinerU.exe。

import os

# spec 位于 scripts/，上推一级得到项目根
ROOT = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(SPEC)), ".."))

def _datas():
    d = []
    # 窗口图标（运行时 iconbitmap 用）
    p_icon = os.path.join(ROOT, "src", "tray", "icon.ico")
    if os.path.isfile(p_icon):
        d.append((p_icon, "."))
    # 应用主程序（onedir：托盘启动器 + 内嵌 WebUI 字节码）
    p_app = os.path.join(ROOT, "release", "MinerU文档解析")
    if os.path.isdir(p_app):
        d.append((p_app, "MinerU文档解析"))
    # 卸载器（安装时复制到安装根）
    p_un = os.path.join(ROOT, "release", "卸载MinerU.exe")
    if os.path.isfile(p_un):
        d.append((p_un, "."))
    for name in ("install_mineru_uv.py", "download_torch_wheels.py"):
        p = os.path.join(ROOT, "scripts", name)
        if os.path.isfile(p):
            # 注意：datas 的 dest 恒为目录，文件以原名放入 → _MEIPASS\scripts\<name>
            d.append((p, "scripts"))
    # 安装流程入口（被 installer_gui 以子进程调用，须随包分发到 _MEIPASS 根）
    p_flow = os.path.join(ROOT, "src", "installer", "install_flow.py")
    if os.path.isfile(p_flow):
        d.append((p_flow, "."))
    # 修复/升级器 + 多源下载引擎（修复模式以已装 venv python 拉起，_MEIPASS 临时目录不落安装目录）
    p_updater = os.path.join(ROOT, "src", "installer", "updater.py")
    if os.path.isfile(p_updater):
        d.append((p_updater, "."))
    p_fastdl = os.path.join(ROOT, "scripts", "fastdl.py")
    if os.path.isfile(p_fastdl):
        d.append((p_fastdl, "."))
    # 使用说明（安装完成后复制到安装目录并打开）
    p_guide = os.path.join(ROOT, "release", "使用说明.html")
    if os.path.isfile(p_guide):
        d.append((p_guide, "."))
    # 构建信息（版本带构建时间戳）：install_flow 安装时写入本地清单版本，
    # 与 publish_dist 的 dist manifest 版本同源，保证「新装即最新」判定一致
    p_build = os.path.join(ROOT, "release", "build_info.json")
    if os.path.isfile(p_build):
        d.append((p_build, "."))
    return d

a = Analysis(
    [os.path.join(ROOT, "src", "installer", "installer_gui.py")],
    pathex=[ROOT, os.path.join(ROOT, "src", "installer")],
    binaries=[],
    datas=_datas(),
    hiddenimports=["comp_panel"],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["tkinter.test", "test"],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="MinerU安装",
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