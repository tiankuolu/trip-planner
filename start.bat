@echo off
echo ==========================================
echo   HelloAgents Trip Planner
echo ==========================================

:: 启动后端
echo [1/2] Starting backend...
start "Backend" cmd /k "cd /d %~dp0backend && uvicorn app.api.main:app --reload --host 0.0.0.0 --port 8000"

:: 稍等一下后端启动
timeout /t 3 /nobreak >nul

:: 启动前端
echo [2/2] Starting frontend...
start "Frontend" cmd /k "cd /d %~dp0frontend && npm run dev"

echo.
echo Done!
echo   Backend:  http://localhost:8000/docs
echo   Frontend: http://localhost:5173
echo.
pause
