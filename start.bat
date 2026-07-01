@echo off
setlocal enabledelayedexpansion

REM ============================================================
REM  pmCrypto one-click launcher
REM  Double-click to: start the backend + open the dashboard
REM  (kept ASCII-only on purpose to avoid Windows codepage issues)
REM ============================================================

cd /d "%~dp0"

echo.
echo ==========================================
echo   pmCrypto quant system - launcher
echo ==========================================
echo.

REM ---- 1. Pick Python interpreter ----
REM Prefer 'python' (project deps are installed in that 3.12 env).
REM 'py' launcher may point to another version without deps.
where python >nul 2>nul
if %errorlevel%==0 (
    set "PY=python"
) else (
    where py >nul 2>nul
    if !errorlevel!==0 (
        set "PY=py"
    ) else (
        echo [ERROR] Python not found. Install Python 3.10+ and add to PATH.
        pause
        exit /b 1
    )
)
echo [1/4] Using Python: !PY!

REM ---- 2. Check core dependencies ----
!PY! -c "import fastapi, uvicorn, polymarket" >nul 2>nul
if !errorlevel! neq 0 (
    echo [2/4] Dependencies missing, installing requirements.txt ...
    !PY! -m pip install -r requirements.txt
    if !errorlevel! neq 0 (
        echo [ERROR] Install failed. Run manually: !PY! -m pip install -r requirements.txt
        pause
        exit /b 1
    )
) else (
    echo [2/4] Dependencies OK.
)

REM ---- 3. Free port 8000 if a stale process holds it ----
echo [3/4] Checking port 8000 ...
for /f "tokens=5" %%p in ('netstat -aon ^| findstr ":8000 " ^| findstr "LISTENING"') do (
    echo       Killing stale PID %%p ...
    taskkill /F /PID %%p >nul 2>nul
)

REM ---- 4. Launch backend (own minimized window) + open browser ----
echo [4/4] Starting system and opening dashboard ...
start "pmCrypto" /min !PY! main.py

REM Wait for the web panel to come up (~8s)
timeout /t 8 /nobreak >nul

start "" "http://127.0.0.1:8000"

echo.
echo ==========================================
echo   Started. Dashboard: http://127.0.0.1:8000
echo.
echo   - System runs in a minimized "pmCrypto" window
echo   - Do all operations in the browser dashboard
echo   - To STOP: close that "pmCrypto" window
echo ==========================================
echo.
echo You can close THIS window (backend keeps running).
pause
