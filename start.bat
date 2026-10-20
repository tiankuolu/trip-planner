@echo off
setlocal
title Trip Planner
cd /d %~dp0

echo ==========================================
echo   Trip Planner (FastAPI + Vue3)
echo ==========================================

rem ---- Python: 优先 python，缺失则用 py 启动器 ----
where python >nul 2>nul
if errorlevel 1 (
    where py >nul 2>nul
    if errorlevel 1 (
        echo [ERROR] Python not found. Install Python 3.10+ first.
        pause
        exit /b 1
    )
    set "PY_CMD=py -3.11"
) else (
    set "PY_CMD=python"
)

%PY_CMD% -c "import fastapi, uvicorn, openai, httpx" >nul 2>nul
if errorlevel 1 (
    echo [..] Installing backend dependencies, please wait...
    %PY_CMD% -m pip install -r backend\requirements.txt
    if errorlevel 1 (
        echo [ERROR] Backend dependency install failed.
        pause
        exit /b 1
    )
)

rem ---- 环境变量文件 ----
if not exist backend\.env (
    echo [WARN] backend\.env not found, copying from backend\.env.example
    copy backend\.env.example backend\.env >nul
    echo        Fill AMAP_API_KEY / LLM_API_KEY in backend\.env, then run again.
    pause
    exit /b 1
)

where node >nul 2>nul
if errorlevel 1 (
    echo [ERROR] Node.js not found. Install Node 18+ first.
    pause
    exit /b 1
)

if not exist frontend\node_modules (
    echo [..] Installing frontend dependencies, please wait...
    pushd frontend
    call npm install
    if errorlevel 1 (
        popd
        echo [ERROR] Frontend dependency install failed.
        pause
        exit /b 1
    )
    popd
)

if not exist frontend\.env (
    echo [WARN] frontend\.env not found, copying from frontend\.env.example
    copy frontend\.env.example frontend\.env >nul
    echo        Fill VITE_AMAP_WEB_JS_KEY in frontend\.env (map will not render without it).
)

echo [1/2] Starting backend (http://localhost:8000/docs)...
start "TripPlanner-Backend" cmd /k "cd /d %~dp0backend && %PY_CMD% run.py"

ping -n 5 127.0.0.1 >nul

echo [2/2] Starting frontend (http://localhost:5300)...
start "TripPlanner-Frontend" cmd /k "cd /d %~dp0frontend && npm run dev"

ping -n 5 127.0.0.1 >nul
start http://localhost:5300

echo.
echo Done! Close the two console windows to stop the servers.
echo Health check: http://localhost:8000/api/trip/health
echo.
pause
