# -*- mode: python ; coding: utf-8 -*-
# MinerU 卸载器打包配置（onefile / 窗口模式）
# 产物：release/卸载MinerU.exe —— 安装时复制到安装根目录
# 构建命令（工作目录须为项目根）：
#   runtime\venv\Scripts\python.exe -m PyInstaller --clean --noconfirm --distpath release --workpath .tmp\pyinstaller\uninstall-build scripts\uninstaller.spec

import os

ROOT = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(SPEC)), ".."))

a = Analysis(
    [os.path.join(ROOT, "src", "installer", "uninstaller_gui.py")],
    pathex=[ROOT],
    binaries=[],
    datas=[],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
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
    name="卸载MinerU",
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
