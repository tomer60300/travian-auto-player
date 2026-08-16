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
    echo ERROR: 'npm' is not on PATH. Install Node.js 20.19+ or 22.12+ from https://nodejs.org/
    goto :fail
)
where node >nul 2>&1
if errorlevel 1 (
    echo ERROR: 'node' is not on PATH. Install Node.js 20.19+ or 22.12+ from https://nodejs.org/
    goto :fail
)
:: Vite 8 (the locked frontend toolchain) requires Node ^20.19.0 or >=22.12.0.
:: The check must match that range exactly: a major-only check let Node
:: 20.0-20.18 and Node 21 pass preflight and then fail halfway through the
:: frontend build.
set NODE_MAJOR=
set NODE_MINOR=
for /f "tokens=1,2 delims=v." %%a in ('node -v') do (set NODE_MAJOR=%%a& set NODE_MINOR=%%b)
if "%NODE_MAJOR%"=="" (
    echo ERROR: could not read the Node version from 'node -v'.
    goto :fail
)
set NODE_OK=1
if %NODE_MAJOR% LSS 20 set NODE_OK=0
if %NODE_MAJOR% EQU 20 if %NODE_MINOR% LSS 19 set NODE_OK=0
if %NODE_MAJOR% EQU 21 set NODE_OK=0
if %NODE_MAJOR% EQU 22 if %NODE_MINOR% LSS 12 set NODE_OK=0
if %NODE_OK% EQU 0 (
    echo ERROR: Node %NODE_MAJOR%.%NODE_MINOR% cannot build the frontend. It needs Node 20.19+ or 22.12+.
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
