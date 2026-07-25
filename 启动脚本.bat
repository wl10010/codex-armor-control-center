@echo off
chcp 65001 >nul
setlocal EnableExtensions DisableDelayedExpansion

set "SCRIPT_DIR=%~dp0"
set "GUI_SCRIPT=%SCRIPT_DIR%codex56-control-center.py"
set "LAUNCHER_PATH=%~f0"
set "PYTHON_EXE="
set "PYTHON_ARGS="

if not exist "%GUI_SCRIPT%" (
    echo [ERROR] GUI script was not found:
    echo "%GUI_SCRIPT%"
    pause
    exit /b 1
)

where.exe python.exe >nul 2>&1
if errorlevel 1 goto check_py_launcher
python.exe -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>&1
if not errorlevel 1 (
    set "PYTHON_EXE=python.exe"
    goto python_ready
)

:check_py_launcher
where.exe py.exe >nul 2>&1
if errorlevel 1 goto python_missing
py.exe -3 -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>&1
if errorlevel 1 goto python_missing
set "PYTHON_EXE=py.exe"
set "PYTHON_ARGS=-3"
goto python_ready

:python_missing
call :show_ai_prompt "Python 3.10 或更高版本"
exit /b 1

:python_ready
"%PYTHON_EXE%" %PYTHON_ARGS% -c "import tkinter" >nul 2>&1
if errorlevel 1 (
    call :show_ai_prompt "Python 图形组件 Tkinter"
    exit /b 1
)

echo [Environment]
"%PYTHON_EXE%" %PYTHON_ARGS% --version
echo [Starting] "%GUI_SCRIPT%"

pushd "%SCRIPT_DIR%"
"%PYTHON_EXE%" %PYTHON_ARGS% "%GUI_SCRIPT%"
set "EXIT_CODE=%ERRORLEVEL%"
popd

if not "%EXIT_CODE%"=="0" (
    echo.
    echo [ERROR] GUI exited with code %EXIT_CODE%.
    pause
)

exit /b %EXIT_CODE%

:show_ai_prompt
set "MISSING_COMPONENT=%~1"
powershell.exe -NoProfile -STA -Command "Add-Type -AssemblyName System.Windows.Forms; $item=$env:MISSING_COMPONENT; $launcher=$env:LAUNCHER_PATH; $prompt='请帮我在这台 Windows 电脑上安装或修复 '+$item+'。安装完成后，请运行 '+[char]34+$launcher+[char]34+' 重新打开软件。'; [System.Windows.Forms.Clipboard]::SetText($prompt); [void][System.Windows.Forms.MessageBox]::Show('缺少 '+$item+'。安装提示词已复制，请打开 Codex 直接粘贴发送。','需要配置运行环境',[System.Windows.Forms.MessageBoxButtons]::OK,[System.Windows.Forms.MessageBoxIcon]::Information)"
if errorlevel 1 (
    echo [ERROR] Missing %MISSING_COMPONENT%.
    echo Ask Codex to install it, then run "%LAUNCHER_PATH%".
    pause
)
exit /b 0
