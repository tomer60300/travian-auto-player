@echo off
:: Travian Auto Player — one-click setup and start
:: Usage: double-click this file or run `start.bat` from the project root

echo.
echo  ============================================
echo   Travian Auto Player — Setup ^& Start
echo  ============================================
echo.

cd /d "%~dp0"

:: 1. Install Python dependencies
echo [1/3] Installing Python dependencies...
pip install -e ".[web]" --quiet 2>nul
if errorlevel 1 (
    echo        pip install failed — trying with --user flag...
    pip install -e ".[web]" --quiet --user 2>nul
)
echo        Done.

:: 2. Build frontend
echo [2/3] Building frontend...
cd frontend
if not exist node_modules (
    echo        Installing npm packages...
    call npm install --silent 2>nul
)
call npm run build --silent 2>nul
cd ..
echo        Done.

:: 3. Start server
echo [3/3] Starting server on http://localhost:8001
echo.
echo  ============================================
echo   Open http://localhost:8001 in your browser
echo  ============================================
echo.
python -m uvicorn travian_api.web.app:app --host 0.0.0.0 --port 8001
pause
