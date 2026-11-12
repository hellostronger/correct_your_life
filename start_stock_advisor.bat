@echo off
rem ============================================================
rem  Stock Advisor 一键启动（双击即用）
rem  中间件全部走 101 服务器（.env + config.yaml 已配好）：
rem    PostgreSQL     101.43.25.101:5432
rem    LLM 网关       101.43.25.101:3000
rem    数据服务       101.43.25.101:8080
rem    searxng        101.43.25.101:8888
rem  本机跑 nanobot agent (127.0.0.1:8900, git 子模块 ./nanobot)
rem       + uvicorn (127.0.0.1:8686)，启动需 1~4 分钟
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

rem ---- 已在运行则只打开页面 ----
netstat -ano | findstr ":8686 " | findstr "LISTENING" >nul 2>&1
if %errorlevel%==0 (
    echo [OK] service already running, opening browser...
    start "" http://127.0.0.1:8686/
    timeout /t 3 >nul
    exit /b 0
)

rem ---- 找 python ----
set PYEXE=python
where python >nul 2>&1
if not %errorlevel%==0 (
    set PYEXE=D:\Python312\python.exe
)

rem ---- nanobot agent（git 子模块 ./nanobot，127.0.0.1:8900）----
rem  对话 tab / 微信闲聊的主路 agent（wx_chat.agent_chat）；挂了聊天会回退单轮 LLM。
rem  配置与会话存 ~/.nanobot/（与代码目录无关），跑子模块副本不影响现有记忆。
rem  python 必须用 D:\nanobot\.venv（依赖装在这里，子模块代码经 cwd 生效）；
rem  命令是 serve（OpenAI 兼容 /v1，wx_chat 用），不是 gateway（无 HTTP）；
rem  NO_PROXY=* 绕开本机代理/TUN，否则出向 LLM 调用 502。
set NANO_DIR=D:\correct_your_life\nanobot
set NANO_PY=D:\nanobot\.venv\Scripts\python.exe
netstat -ano | findstr ":8900 " | findstr "LISTENING" >nul 2>&1
if %errorlevel%==0 (
    echo [OK] nanobot agent already running on :8900
) else (
    if not exist "%NANO_DIR%\nanobot\__main__.py" (
        echo [WARN] nanobot submodule missing, skip agent. fix: git submodule update --init nanobot
    ) else if not exist "%NANO_PY%" (
        echo [WARN] %NANO_PY% not found, skip agent.
    ) else (
        echo [START] nanobot agent serve :8900  (log -^> %NANO_DIR%\nanobot_console.log^)
        start "nanobot-agent" /min cmd /c "cd /d %NANO_DIR% && set NO_PROXY=*&& %NANO_PY% -m nanobot serve --port 8900 > nanobot_console.log 2>&1"
        rem 轮询 8900 最多 60s（不阻塞主流程：agent 没起聊天仍有 LLM 回退）
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
    )
)

echo [START] python app.py  (middleware = 101.43.25.101)
echo [LOG]   console output -> app_console.log
start "sa-server" /min cmd /c "%PYEXE% app.py > app_console.log 2>&1"

rem ---- 轮询健康检查，最多约 8 分钟（启动耗时波动 58~236s）----
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
