@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"
set "VW_PY="
set "VW_ARGS="
if defined VIDEO_WATCH_PYTHON (
  if not exist "%VIDEO_WATCH_PYTHON%" (
    echo [找不到 Python] VIDEO_WATCH_PYTHON 指向的解释器不存在：%VIDEO_WATCH_PYTHON%
    goto missing
  )
  set "VW_PY=%VIDEO_WATCH_PYTHON%"
  goto launch
)
if exist ".venv\Scripts\python.exe" (
  set "VW_PY=%CD%\.venv\Scripts\python.exe"
  goto launch
)
if exist "venv\Scripts\python.exe" (
  set "VW_PY=%CD%\venv\Scripts\python.exe"
  goto launch
)
where py.exe >nul 2>&1
if not errorlevel 1 (
  py -3 -c "import sys" >nul 2>&1
  if not errorlevel 1 (
    set "VW_PY=py"
    set "VW_ARGS=-3"
    goto launch
  )
)
where python.exe >nul 2>&1
if not errorlevel 1 (
  python -c "import sys" >nul 2>&1
  if not errorlevel 1 (
    set "VW_PY=python"
    goto launch
  )
)
:missing
echo [找不到 Python] 请安装 Python 3.10+，或把 VIDEO_WATCH_PYTHON 设置为 python.exe 的完整路径。
pause
exit /b 2
:launch
"%VW_PY%" %VW_ARGS% -u scripts\launch_webui.py %*
set "VW_EXIT=%ERRORLEVEL%"
if not "%VW_EXIT%"=="0" pause
exit /b %VW_EXIT%
