@echo off
rem ============================================================
rem  Stock Advisor one-click start (double-click)
rem  Middleware all on 101 server (.env + config.yaml ready):
rem    PostgreSQL     101.43.25.101:5432
rem    LLM gateway    101.43.25.101:3000
rem    data service   101.43.25.101:8080
rem    searxng        101.43.25.101:8888
rem  Local: nanobot agent (127.0.0.1:8900, git submodule ./nanobot)
rem         + uvicorn (127.0.0.1:8686), startup takes 1~4 min
rem  NOTE: keep this file pure ASCII (no Chinese) -- cmd mis-parses
rem        UTF-8 bytes under the default ANSI codepage.
rem        All goto labels stay at top level (no labels inside
rem        parenthesized blocks; no %var% reads inside blocks).
rem ============================================================
setlocal
chcp 65001 >nul
title Stock Advisor - 101 middleware

cd /d D:\correct_your_life\stock-advisor
if errorlevel 1 (
    echo [ERROR] project dir not found
    pause
    exit /b 1
)

rem ---- nanobot agent (git submodule ./nanobot, 127.0.0.1:8900) ----
rem  Placed BEFORE the "8686 already running" early exit: if the main app
rem  is alive but the agent died, double-click still revives it.
rem  Main-path agent for web chat tab / wechat small talk
rem  (wx_chat.agent_chat); if down, chat falls back to plain LLM.
rem  Must use D:\nanobot\.venv python (deps live there; submodule code
rem  is picked up via cwd). Command is `serve` (OpenAI-compatible /v1,
rem  what wx_chat calls), NOT `gateway` (no HTTP server).
rem  NO_PROXY=* bypasses the local proxy/TUN, else outbound LLM gets 502.
set NANO_DIR=D:\correct_your_life\nanobot
set NANO_PY=D:\nanobot\.venv\Scripts\python.exe
netstat -ano | findstr ":8900 " | findstr "LISTENING" >nul 2>&1
if %errorlevel%==0 (
    echo [OK] nanobot agent already running on :8900
    goto nano_done
)
if not exist "%NANO_DIR%\nanobot\__main__.py" (
    echo [WARN] nanobot submodule missing, skip agent. fix: git submodule update --init nanobot
    goto nano_done
)
if not exist "%NANO_PY%" (
    echo [WARN] %NANO_PY% not found, skip agent.
    goto nano_done
)
echo [START] nanobot agent serve :8900  (log -^> %NANO_DIR%\nanobot_console.log^)
start "nanobot-agent" /min cmd /c "cd /d %NANO_DIR% && set NO_PROXY=*&& %NANO_PY% -m nanobot serve --port 8900 > nanobot_console.log 2>&1"
rem poll :8900 up to ~60s (non-fatal: chat falls back to plain LLM)
set /a ntries=0
:nanopoll
timeout /t 3 >nul
curl -s -o nul -m 3 http://127.0.0.1:8900/v1/models >nul 2>&1
if %errorlevel%==0 goto nano_up
set /a ntries+=1
if %ntries% geq 20 goto nano_slow
goto nanopoll
:nano_up
echo [OK] nanobot agent up on :8900
goto nano_done
:nano_slow
echo [WARN] nanobot :8900 not up after ~60s. chat will fall back to plain LLM. check nanobot_console.log
:nano_done

rem ---- already running: just open the page ----
netstat -ano | findstr ":8686 " | findstr "LISTENING" >nul 2>&1
if %errorlevel%==0 (
    echo [OK] service already running, opening browser...
    start "" http://127.0.0.1:8686/
    timeout /t 3 >nul
    exit /b 0
)

rem ---- find python ----
set PYEXE=python
where python >nul 2>&1
if not %errorlevel%==0 (
    set PYEXE=D:\Python312\python.exe
)

echo [START] python app.py  (middleware = 101.43.25.101)
echo [LOG]   console output -> app_console.log
start "sa-server" /min cmd /c "%PYEXE% app.py > app_console.log 2>&1"

rem ---- poll health check, up to ~8 min (startup varies 58~236s) ----
echo [WAIT] polling http://127.0.0.1:8686/ ...
set /a tries=0
:poll
timeout /t 5 >nul
curl -s -o nul -m 5 http://127.0.0.1:8686/ >nul 2>&1
if %errorlevel%==0 goto up
set /a tries+=1
if %tries% geq 100 goto timeout_fail
echo   ...waiting (%tries%x5s)
goto poll

:up
echo.
echo [OK] started. opening http://127.0.0.1:8686/
start "" http://127.0.0.1:8686/
timeout /t 3 >nul
exit /b 0

:timeout_fail
echo.
echo [FAIL] not up after ~8 min. check app_console.log
start notepad D:\correct_your_life\stock-advisor\app_console.log
pause
exit /b 1
