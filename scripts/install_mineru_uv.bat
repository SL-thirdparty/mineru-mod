@echo off
title MinerU Installer v2 (uv-first / GPU auto)
mode con cols=120 lines=50
setlocal
set "BD=%~dp0"

where py >nul 2>nul
if %errorlevel%==0 goto use_py

where python >nul 2>nul
if %errorlevel%==0 goto use_python

echo [ERROR] Python 3.11 not found. Tick "Add Python to PATH".
echo   https://www.python.org/downloads/release/python-3119/
goto END

:use_py
py -3.11 "%BD%install_mineru_uv.py" %*
goto DONE

:use_python
python "%BD%install_mineru_uv.py" %*
goto DONE

:DONE
echo.
echo Installer finished. Review the messages above, then press any key.

:END
echo.
pause
endlocal