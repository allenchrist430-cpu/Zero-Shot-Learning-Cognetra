@echo off
cls

echo ========================================================
echo        COGNETRA 3.0 - SYSTEM LAUNCH
echo   Zero-Shot Accident Detection Platform
echo ========================================================
echo.

REM --- Step 1: Launch Central Server ---
echo [1/2] Starting Central Server on port 9000...
start "COGNETRA SERVER :9000" cmd /k "cd /d %~dp0central_server && call .venv\Scripts\activate.bat && python app.py"

REM --- Wait 6 seconds for server to boot ---
echo Waiting for server initialization...
ping 127.0.0.1 -n 7 >nul

REM --- Step 2: Open Dashboards in Browser ---
echo [*] Opening dashboards in default browser...
start http://127.0.0.1:9000/
ping 127.0.0.1 -n 2 >nul
start http://127.0.0.1:9000/monitor

REM --- Step 3: Launch Edge Client ---
echo [2/2] Starting Edge Client...
start "COGNETRA EDGE CLIENT" cmd /k "cd /d %~dp0edge_client && call .venv\Scripts\activate.bat && python main.py"

echo.
echo ========================================================
echo   [OK] Both processes launched in separate windows!
echo.
echo   Backend Ops Dashboard : http://127.0.0.1:9000/
echo   Frontend Monitor      : http://127.0.0.1:9000/monitor
echo   API Docs              : http://127.0.0.1:9000/docs
echo   Health Check          : http://127.0.0.1:9000/health
echo ========================================================
echo.
pause
