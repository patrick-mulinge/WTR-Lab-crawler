# WTR-Lab worker-only launcher (no Telegram polling)
# Double-click "Worker for Windows.bat" or run: powershell -File worker.ps1
#
# Does NOT run first-time setup. Use "Start for Windows.bat" once so that
# .venv, .env, and dependencies exist. This script only starts worker.py.

$ErrorActionPreference = "Stop"
Set-Location -Path $PSScriptRoot

$VenvPython = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
$EnvFile    = Join-Path $PSScriptRoot ".env"
$WorkerFile = Join-Path $PSScriptRoot "worker.py"
$AppFile    = Join-Path $PSScriptRoot "app.py"

function Write-Info($msg)  { Write-Host "[*] $msg" -ForegroundColor Cyan }
function Write-Ok($msg)    { Write-Host "[+] $msg" -ForegroundColor Green }
function Write-Warn($msg)  { Write-Host "[!] $msg" -ForegroundColor Yellow }
function Write-Err($msg)   { Write-Host "[x] $msg" -ForegroundColor Red }

Write-Host ""
Write-Host "========================================" -ForegroundColor Cyan
Write-Host "  WTR-Lab Worker Only" -ForegroundColor Cyan
Write-Host "  (no Telegram polling)" -ForegroundColor Cyan
Write-Host "========================================" -ForegroundColor Cyan
Write-Host ""

if (-not (Test-Path $AppFile)) {
    Write-Err "app.py not found. Run full setup from Start for Windows.bat first."
    exit 1
}
if (-not (Test-Path $WorkerFile)) {
    Write-Err "worker.py not found in this folder."
    exit 1
}
if (-not (Test-Path $VenvPython)) {
    Write-Err ".venv not found. Run Start for Windows.bat once to set up."
    exit 1
}
if (-not (Test-Path $EnvFile)) {
    Write-Err ".env not found. Run Start for Windows.bat once to configure BOT_TOKEN."
    exit 1
}

$tokenLine = Get-Content $EnvFile -ErrorAction SilentlyContinue |
    Where-Object { $_ -match '^\s*BOT_TOKEN\s*=' } |
    Select-Object -First 1
if (-not $tokenLine -or $tokenLine -match 'BOT_TOKEN\s*=\s*$' -or $tokenLine -match 'your_token_here') {
    Write-Warn "BOT_TOKEN looks empty - progress/EPUB sends will fail."
}

Write-Warn "This mode does NOT poll Telegram."
Write-Host "  - Users cannot /download or queue new tasks while only this is running."
Write-Host "  - Existing pending tasks in data\worker.sqlite3 will still be processed."
Write-Host "  - Progress and finished EPUBs are still sent with the bot token."
Write-Host ""
Write-Host "To accept new chat tasks, stop this and run Start for Windows.bat (app.py)."
Write-Host ""

Write-Warn "Close other Chrome windows if the profile is locked."
$ans = Read-Host "Try to close all Chrome processes now? (y/N)"
if ($ans -match '^[Yy]') {
    Get-Process chrome -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue
    Start-Sleep -Seconds 2
    Write-Ok "Chrome processes signaled to close."
}

Write-Host ""
Write-Info "Starting worker.py ..."
Write-Host "Log into WTR-Lab in the Chrome window if asked."
Write-Host "Press Ctrl+C in this window to stop."
Write-Host ""

& $VenvPython $WorkerFile
$exitCode = $LASTEXITCODE

Write-Host ""
if ($exitCode -ne 0) {
    Write-Warn "worker.py exited with code $exitCode"
} else {
    Write-Ok "Stopped."
}
exit $exitCode
