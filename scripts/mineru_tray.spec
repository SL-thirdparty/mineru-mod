# -*- mode: python ; coding: utf-8 -*-
# MinerU 托盘启动器打包配置（onedir / 窗口模式）
# 构建命令（工作目录须为项目根）：
#   runtime\venv\Scripts\python.exe -m PyInstaller --clean --noconfirm --distpath release --workpath .tmp\pyinstaller\build scripts\mineru_tray.spec

import os

# spec 位于 scripts/，上推一级得到项目根
ROOT = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(SPEC)), ".."))

a = Analysis(
    [os.path.join(ROOT, "src", "tray", "mineru_tray.py")],
    pathex=[ROOT],
    binaries=[],
    datas=[],
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
    excludes=["tkinter"],
    noarchive=False,
    optimize=0,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="mineru_tray",
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
    name="mineru_tray",
)
