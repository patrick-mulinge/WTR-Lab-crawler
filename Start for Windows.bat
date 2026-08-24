@echo off
setlocal EnableExtensions
title WTR-Lab Local Worker
cd /d "%~dp0"

REM Double-click entry point. Uses PowerShell for prompts and setup.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0start-windows.ps1"
set "ERR=%ERRORLEVEL%"
if not "%ERR%"=="0" (
  echo.
  echo Script exited with code %ERR%.
  pause
)
endlocal
exit /b %ERR%
