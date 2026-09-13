@echo off
chcp 65001 >nul
cd /d "%~dp0"
rem Prefer the py launcher: a winget/python.org install often doesn't put python.exe on PATH.
set "PY="
py -3 -c "import sys" >nul 2>nul && set "PY=py -3"
if not defined PY python -c "import sys" >nul 2>nul && set "PY=python"
if not defined PY (
  echo Python nenalezen - spust nejdriv setup.bat
  pause
  exit /b 1
)
%PY% tsync.py --app toca sync
echo.
pause
