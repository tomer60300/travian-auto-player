@echo off
setlocal
:: Travian Auto Player - one-click setup and start
:: Usage: double-click this file or run start.bat from the project root

echo.
echo  ============================================
echo   Travian Auto Player - Setup ^& Start
echo  ============================================
echo.

cd /d "%~dp0"

:: Preflight: require Python and Node on PATH
where python >nul 2>&1
if errorlevel 1 (
    echo ERROR: 'python' is not on PATH. Install Python 3.11+ from https://www.python.org/downloads/
    goto :fail
)
where npm >nul 2>&1
if errorlevel 1 (
    echo ERROR: 'npm' is not on PATH. Install Node.js 18+ from https://nodejs.org/
    goto :fail
)

:: 1. Create/activate venv and install Python dependencies
echo [1/3] Installing Python dependencies...
if not exist ".venv\Scripts\python.exe" (
    echo        Creating virtual environment at .venv ...
    python -m venv .venv
    if errorlevel 1 (
        echo ERROR: failed to create virtual environment.
        goto :fail
    )
)
call ".venv\Scripts\activate.bat"
python -m pip install --upgrade pip
if errorlevel 1 (
    echo ERROR: failed to upgrade pip in .venv
    goto :fail
)
python -m pip install -e ".[web]"
if errorlevel 1 (
    echo ERROR: pip install failed. See errors above.
    goto :fail
)
echo        Done.

:: 2. Build frontend
echo [2/3] Building frontend...
pushd frontend
if errorlevel 1 (
    echo ERROR: frontend directory not found.
    goto :fail
)
:: Always sync npm packages: after a pull, an existing node_modules can be
:: stale against the new package-lock.json, and npm install is fast when
:: everything is already current.
echo        Installing npm packages...
call npm install
if errorlevel 1 (
    popd
    echo ERROR: npm install failed.
    goto :fail
)
call npm run build
if errorlevel 1 (
    popd
    echo ERROR: frontend build failed.
    goto :fail
)
popd
echo        Done.

:: 3. Start server
echo [3/3] Starting server on http://localhost:8001
echo.
echo  ============================================
echo   Open http://localhost:8001 in your browser
echo  ============================================
echo.
python -m uvicorn travian_api.web.app:app --host 0.0.0.0 --port 8001
goto :end

:fail
echo.
echo Setup failed. Fix the error above and re-run start.bat.
pause
exit /b 1

:end
pause
endlocal
