@echo off
setlocal
title MinerU Installer
REM Self-contained installer (pure ASCII). Needs NO companion .py file.
REM Just copy THIS file next to models_cache, run it, done.
set "BD=%~dp0"

rem ---------- 1) locate a usable Python ----------
set "PY="
where py >nul 2>nul
if %errorlevel%==0 set "PY=py -3.11"
if not defined PY (
    where python >nul 2>nul
    if %errorlevel%==0 set "PY=python"
)
if not defined PY (
    echo [ERROR] Python 3.11 not found.
    echo Install it and tick "Add Python to PATH":
    echo   https://www.python.org/downloads/release/python-3119/
    echo China mirror:
    echo   https://mirrors.huaweicloud.com/python/3.11.9/python-3.11.9-amd64.exe
    goto END
)

rem ---------- 2) optional PyPI mirror ----------
set "MIRROR="
if "%1"=="--mirror" if not "%2"=="" set "MIRROR=%2"

%PY% --version
if errorlevel 1 goto ERR

set "VPY=%BD%venv\Scripts\python.exe"

rem ---------- 3) create venv ----------
if not exist "%BD%venv" goto make_venv
if exist "%VPY%" if exist "%BD%venv\Lib\site-packages" goto venv_ready
echo   [*] Found incomplete old venv, removing to rebuild ...
rmdir /s /q "%BD%venv"
:make_venv
echo [1/4] Creating virtual env: %BD%venv
%PY% -m venv "%BD%venv"
if errorlevel 1 goto ERR
:venv_ready
rem ---- mirror-first pip with backup sources (fail-safe fallback to official) ----
if defined MIRROR (
    set "PIP_INDEX_URL=%MIRROR%"
    set "PIP_EXTRA_INDEX_URL=https://mirrors.aliyun.com/pypi/simple/ https://pypi.org/simple"
) else (
    set "PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple"
    set "PIP_EXTRA_INDEX_URL=https://mirrors.aliyun.com/pypi/simple/ https://pypi.mirrors.ustc.edu.cn/simple https://pypi.org/simple"
)

echo [2/4] venv ready. Upgrading pip (mirror-first) ...
"%VPY%" -m pip install --upgrade pip --timeout 60 --retries 3
if errorlevel 1 goto TRY_OFFICIAL

echo [3/4] Installing MinerU (mirror-first) ...
"%VPY%" -m pip install "mineru[core]" --timeout 60 --retries 3
if errorlevel 1 goto TRY_OFFICIAL

echo     Installing tray deps ...
"%VPY%" -m pip install pywin32 pystray --timeout 60 --retries 3
if errorlevel 1 goto TRY_OFFICIAL
goto PIP_OK

:TRY_OFFICIAL
echo   [*] Mirror sources unavailable. Retrying with official index ...
set "PIP_INDEX_URL="
set "PIP_EXTRA_INDEX_URL="
"%VPY%" -m pip install --upgrade pip
if errorlevel 1 goto ERR
"%VPY%" -m pip install "mineru[core]"
if errorlevel 1 goto ERR
"%VPY%" -m pip install pywin32 pystray
if errorlevel 1 goto ERR

:PIP_OK
echo   [*] Dependencies ready (mineru + tray deps).

rem ---------- 5) generate mineru.json (locate models_cache) ----------
echo [4/4] Writing mineru.json for this folder ...
set "MLROOT=%BD%"
"%VPY%" -c "import os,json;root=os.environ['MLROOT'];cache=os.path.join(root,'models_cache');kit=os.path.join(cache,'models','OpenDataLab--PDF-Extract-Kit-1.0','snapshots','master');base=kit if os.path.isdir(kit) else cache;open(os.path.join(root,'mineru.json'),'w').write(json.dumps({'models-dir':{'pipeline':base.replace(os.sep,'/')},'model-source':'modelscope'},indent=2));print('mineru.json ->',base)"
if errorlevel 1 goto ERR

echo.
echo Installation finished.
echo Usage: double-click MinerU_Tray\MinerU_Tray.exe
goto END

:ERR
echo.
echo [ERROR] A step failed. Review the messages above.

:END
echo.
pause
endlocal