@echo off
echo ========================================================
echo   Installing Cognetra Central Server dependencies...
echo ========================================================
echo.

cd /d %~dp0central_server

IF NOT EXIST .venv (
    py -3.12 -m venv .venv 2>nul || python -m venv .venv
    echo Created Python venv.
)

call .venv\Scripts\activate.bat
pip install --upgrade pip -q
pip install -r requirements.txt --prefer-binary

echo.
echo [OK] Server setup complete.
echo   Run: cd central_server ^&^& .venv\Scripts\activate ^&^& python app.py
echo.
pause
