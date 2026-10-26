@echo off
setlocal
title Trip Planner React
cd /d "%~dp0"
pwsh.exe -NoProfile -File "%~dp0start-react.ps1"
set "TRIP_EXIT_CODE=%errorlevel%"
if not "%TRIP_EXIT_CODE%"=="0" pause
exit /b %TRIP_EXIT_CODE%
