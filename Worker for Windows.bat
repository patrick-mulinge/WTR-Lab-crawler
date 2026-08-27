@echo off
setlocal EnableExtensions
title WTR-Lab Worker Only (no Telegram polling)
cd /d "%~dp0"

REM Double-click entry: worker-only mode (processes SQLite queue, no bot polling).
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0worker.ps1"
set "ERR=%ERRORLEVEL%"
if not "%ERR%"=="0" (
  echo.
  echo Script exited with code %ERR%.
  pause
)
endlocal
exit /b %ERR%
