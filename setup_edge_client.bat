@echo off
echo ========================================================
echo   Installing Cognetra Edge Client dependencies...
echo ========================================================
echo.

cd /d %~dp0edge_client

IF NOT EXIST .venv (
    py -3.12 -m venv .venv 2>nul || python -m venv .venv
    echo Created Python venv.
)

call .venv\Scripts\activate.bat
pip install --upgrade pip -q
pip install -r requirements.txt --prefer-binary

echo.
echo [OK] Edge client setup complete.
echo   Add videos to: edge_client\videos\
echo   Run: cd edge_client ^&^& .venv\Scripts\activate ^&^& python main.py
echo.
pause
