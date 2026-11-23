@echo off
setlocal
pushd "%~dp0" || exit /b 1
set "runtime=.venv\Scripts\python.exe"
set "ready=.venv\codeweft-web.ready"

if /i "%~1"=="--setup" goto setup
if not exist "%runtime%" (
    if not exist "web\dist\index.html" goto setup
    python -c "import uvicorn; from codeagent.web.api import create_app" >nul 2>&1 || goto setup
    set "runtime=python"
    goto serve
)
if not exist "%ready%" goto setup
if exist "web\dist\index.html" goto serve

:setup
if exist "%ready%" del "%ready%" || goto failed
if not exist ".venv\Scripts\pip.exe" (
    echo Creating Python virtual environment...
    python -m venv .venv || goto failed
)
"%runtime%" -m pip install -e ".[web]" || goto failed
call npm.cmd --prefix web ci || goto failed
call npm.cmd --prefix web run build || goto failed
echo ready>"%ready%" || goto failed

:serve
echo Open http://127.0.0.1:8765 when the server is ready. Press Ctrl+C to stop.
"%runtime%" -m codeagent.web.cli --workspace . --port 8765 || goto failed
popd
exit /b 0

:failed
echo CodeWeft could not start. Check the error above.
pause
popd
exit /b 1
