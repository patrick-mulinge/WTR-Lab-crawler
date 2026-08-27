@echo off
setlocal EnableExtensions
title WTR-Lab Local Worker
cd /d "%~dp0"

REM Same idea as bat + ps1: batch cds here and hands off to PowerShell.
REM PowerShell runs from the marker below in-memory (no temp .ps1 file).
set "WTR_PROJECT_ROOT=%CD%"
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$raw = Get-Content -LiteralPath '%~f0' -Raw -Encoding UTF8;" ^
  "$tag = '__WTR_PS_BODY_BEGIN__';" ^
  "$idx = $raw.LastIndexOf($tag);" ^
  "if ($idx -lt 0) { Write-Host 'Embedded PowerShell marker missing in bat.' -ForegroundColor Red; exit 1 };" ^
  "Invoke-Expression $raw.Substring($idx + $tag.Length);"

set "ERR=%ERRORLEVEL%"
if not "%ERR%"=="0" (
  echo.
  echo Script exited with code %ERR%.
  pause
)
endlocal
exit /b %ERR%

__WTR_PS_BODY_BEGIN__
# WTR-Lab local worker - setup once, then start app.py
# Same logic as start-windows.ps1; folder comes from WTR_PROJECT_ROOT.

$ErrorActionPreference = "Stop"

$ProjectRoot = $env:WTR_PROJECT_ROOT
if (-not $ProjectRoot) { $ProjectRoot = (Get-Location).Path }
$ProjectRoot = $ProjectRoot.TrimEnd("\")
Set-Location -LiteralPath $ProjectRoot

$VenvPython = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
$VenvPip    = Join-Path $ProjectRoot ".venv\Scripts\pip.exe"
$EnvFile    = Join-Path $ProjectRoot ".env"
$ReqFile    = Join-Path $ProjectRoot "requirements.txt"
$AppFile    = Join-Path $ProjectRoot "app.py"
$Marker     = Join-Path $ProjectRoot "data\.setup_done"

$GithubZipUrl = "https://github.com/patrick-mulinge/WTR-Lab-crawler/archive/refs/heads/main.zip"

function Write-Info($msg)  { Write-Host "[*] $msg" -ForegroundColor Cyan }
function Write-Ok($msg)    { Write-Host "[+] $msg" -ForegroundColor Green }
function Write-Warn($msg)  { Write-Host "[!] $msg" -ForegroundColor Yellow }
function Write-Err($msg)   { Write-Host "[x] $msg" -ForegroundColor Red }

function Test-ProjectComplete {
    return (Test-Path -LiteralPath $AppFile) -and (Test-Path -LiteralPath $ReqFile)
}

function Ensure-ProjectFiles {
    if (Test-ProjectComplete) { return }

    Write-Warn "app.py or requirements.txt is missing in this folder."
    Write-Host "    Folder: $ProjectRoot"
    if (-not $GithubZipUrl) {
        Write-Err "Cannot auto-download (no GitHub URL configured)."
        Write-Host "Copy the full project folder here, then run again."
        exit 1
    }

    Write-Info "Downloading project from GitHub..."
    $tmpZip = Join-Path $env:TEMP "wtrlab-standalone.zip"
    $tmpDir = Join-Path $env:TEMP "wtrlab-standalone-extract"
    try {
        Invoke-WebRequest -Uri $GithubZipUrl -OutFile $tmpZip -UseBasicParsing
        if (Test-Path -LiteralPath $tmpDir) { Remove-Item -LiteralPath $tmpDir -Recurse -Force }
        Expand-Archive -Path $tmpZip -DestinationPath $tmpDir -Force

        $inner = Get-ChildItem -LiteralPath $tmpDir | Where-Object { $_.PSIsContainer } | Select-Object -First 1
        if (-not $inner) { throw "Empty archive" }

        Get-ChildItem -LiteralPath $inner.FullName -Force | ForEach-Object {
            Copy-Item -LiteralPath $_.FullName -Destination (Join-Path $ProjectRoot $_.Name) -Recurse -Force
        }
        Write-Ok "Project files downloaded."
    } catch {
        Write-Err "Download failed: $_"
        exit 1
    } finally {
        if (Test-Path -LiteralPath $tmpZip) { Remove-Item -LiteralPath $tmpZip -Force -ErrorAction SilentlyContinue }
        if (Test-Path -LiteralPath $tmpDir) { Remove-Item -LiteralPath $tmpDir -Recurse -Force -ErrorAction SilentlyContinue }
    }

    if (-not (Test-ProjectComplete)) {
        Write-Err "Still missing app.py after download."
        Write-Host "    Expected: $AppFile"
        exit 1
    }
}

function Test-ChromeInstalled {
    $paths = @(
        "${env:ProgramFiles}\Google\Chrome\Application\chrome.exe",
        "${env:ProgramFiles(x86)}\Google\Chrome\Application\chrome.exe",
        "$env:LOCALAPPDATA\Google\Chrome\Application\chrome.exe"
    )
    foreach ($p in $paths) {
        if (Test-Path $p) { return $true }
    }
    try {
        $null = Get-Command chrome -ErrorAction Stop
        return $true
    } catch { }
    return $false
}

function Install-Chrome {
    Write-Info "Trying to install Google Chrome via winget..."
    try {
        $null = Get-Command winget -ErrorAction Stop
        & winget install -e --id Google.Chrome --accept-package-agreements --accept-source-agreements
        if (Test-ChromeInstalled) {
            Write-Ok "Chrome installed."
            return
        }
    } catch {
        Write-Warn "winget install failed or winget not available."
    }

    Write-Warn "Automatic install did not finish. Please install Chrome manually:"
    Write-Host "  https://www.google.com/chrome/"
    Write-Host "Then run this script again."
    Start-Process "https://www.google.com/chrome/"
    exit 1
}

function Ensure-Chrome {
    if (Test-ChromeInstalled) {
        Write-Ok "Google Chrome found."
        return
    }
    Write-Warn "Google Chrome was not found."
    $ans = Read-Host "Install Chrome now? (Y/n)"
    if ($ans -match '^[Nn]') {
        Write-Err "Chrome is required. Install it and re-run."
        exit 1
    }
    Install-Chrome
}

function Ensure-Python {
    try {
        $ver = & python --version 2>&1
        Write-Ok "Python found: $ver"
        return
    } catch { }
    try {
        $ver = & py -3 --version 2>&1
        Write-Ok "Python found via py launcher: $ver"
        return
    } catch { }

    Write-Err "Python 3.10+ is not installed or not on PATH."
    Write-Host "Download: https://www.python.org/downloads/"
    Write-Host "During setup, enable: Add python.exe to PATH"
    Start-Process "https://www.python.org/downloads/"
    exit 1
}

function Get-PythonCmd {
    try {
        & python -c "import sys; assert sys.version_info >= (3, 10)" 2>$null
        if ($LASTEXITCODE -eq 0) { return "python" }
    } catch { }
    try {
        & py -3 -c "import sys; assert sys.version_info >= (3, 10)" 2>$null
        if ($LASTEXITCODE -eq 0) { return "py -3" }
    } catch { }
    Write-Err "Need Python 3.10 or newer."
    exit 1
}

function Ensure-Venv {
    if (Test-Path -LiteralPath $VenvPython) {
        Write-Ok "Virtual environment already exists."
        return
    }
    Write-Info "Creating virtual environment (.venv)..."
    $py = Get-PythonCmd
    if ($py -eq "py -3") {
        & py -3 -m venv .venv
    } else {
        & python -m venv .venv
    }
    if (-not (Test-Path -LiteralPath $VenvPython)) {
        Write-Err "Failed to create .venv"
        exit 1
    }
    Write-Ok "Virtual environment created."
}

function Ensure-Dependencies {
    Write-Info "Installing / updating Python packages (may take a few minutes)..."
    & $VenvPython -m pip install --upgrade pip
    & $VenvPip install -r $ReqFile
    if ($LASTEXITCODE -ne 0) {
        Write-Err "pip install failed."
        exit 1
    }
    Write-Ok "Dependencies installed."
}

function Read-EnvDefault([string]$prompt, [string]$default = "") {
    if ($default -ne "") {
        $line = Read-Host "$prompt [$default]"
        if ([string]::IsNullOrWhiteSpace($line)) { return $default }
        return $line.Trim()
    }
    $line = Read-Host $prompt
    return $line.Trim()
}

function Ensure-EnvFile {
    if (Test-Path -LiteralPath $EnvFile) {
        $tokenLine = Get-Content $EnvFile -ErrorAction SilentlyContinue |
            Where-Object { $_ -match '^\s*BOT_TOKEN\s*=' } |
            Select-Object -First 1
        if ($tokenLine -and $tokenLine -notmatch 'BOT_TOKEN\s*=\s*$' -and $tokenLine -notmatch 'your_token_here') {
            Write-Ok ".env already configured."
            return
        }
        Write-Warn ".env exists but BOT_TOKEN looks empty - reconfiguring."
    }

    Write-Host ""
    Write-Host "=== Configure your bot ===" -ForegroundColor Magenta
    Write-Host "Create a bot with @BotFather in Telegram, then paste the token."
    Write-Host "Press Enter on optional fields to leave them empty."
    Write-Host ""

    do {
        $token = Read-Host "BOT_TOKEN (required)"
        $token = $token.Trim()
        if (-not $token) {
            Write-Warn "Token is required."
        }
    } while (-not $token)

    Write-Host ""
    Write-Host "ALLOWED_USER_IDS - your numeric Telegram id(s), comma-separated."
    Write-Host "  Leave empty (press Enter) to allow anyone who can message the bot."
    Write-Host "  Get an id from @userinfobot if you want a hard lock."
    $allowed = Read-Host "ALLOWED_USER_IDS (optional, Enter = open)"
    $allowed = $allowed.Trim()

    Write-Host ""
    Write-Host "CHAPTER_CAP - max chapters per download. 0 = unlimited (recommended)."
    $cap = Read-EnvDefault "CHAPTER_CAP" "0"
    if ($cap -notmatch '^\d+$') { $cap = "0" }

    Write-Host ""
    Write-Host "DAILY_TASK_LIMIT - max tasks per user per day. 0 = unlimited."
    $daily = Read-EnvDefault "DAILY_TASK_LIMIT" "0"
    if ($daily -notmatch '^\d+$') { $daily = "0" }

    Write-Host ""
    Write-Host "Chapter delay (seconds). Defaults 10-18 are safer against Cloudflare."
    $tmin = Read-EnvDefault "CHAPTER_THROTTLE_MIN" "10"
    $tmax = Read-EnvDefault "CHAPTER_THROTTLE_MAX" "18"

    $content = @"
BOT_TOKEN=$token
ALLOWED_USER_IDS=$allowed
OUTPUT_GROUPS=
ADMIN_CHAT_ID=
CHAPTER_CAP=$cap
DAILY_TASK_LIMIT=$daily
CHAPTER_THROTTLE_MIN=$tmin
CHAPTER_THROTTLE_MAX=$tmax
PROGRESS_UPDATE_SECONDS=25
CHROME_PROFILE_DIR=data/chrome-profile
"@
    Set-Content -Path $EnvFile -Value $content -Encoding UTF8
    Write-Ok ".env written."
}

function Ensure-DataDir {
    $data = Join-Path $ProjectRoot "data"
    if (-not (Test-Path -LiteralPath $data)) {
        New-Item -ItemType Directory -Path $data | Out-Null
    }
}

function Mark-SetupDone {
    Ensure-DataDir
    Set-Content -Path $Marker -Value (Get-Date -Format "o") -Encoding UTF8
}

function Test-SetupDone {
    return (Test-Path -LiteralPath $Marker) -and (Test-Path -LiteralPath $VenvPython) -and (Test-Path -LiteralPath $EnvFile)
}

function Stop-ChromeIfNeeded {
    Write-Host ""
    Write-Warn "Close other Chrome windows before the worker starts."
    Write-Host "The worker uses its own profile under data\chrome-profile."
    $ans = Read-Host "Try to close all Chrome processes now? (y/N)"
    if ($ans -match '^[Yy]') {
        Get-Process chrome -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue
        Start-Sleep -Seconds 2
        Write-Ok "Chrome processes signaled to close."
    }
}

Write-Host ""
Write-Host "========================================" -ForegroundColor Cyan
Write-Host "  WTR-Lab Local Worker" -ForegroundColor Cyan
Write-Host "========================================" -ForegroundColor Cyan
Write-Host ""

Ensure-ProjectFiles
Ensure-DataDir

$needsSetup = -not (Test-SetupDone)

if ($needsSetup) {
    Write-Info "First-time (or incomplete) setup..."
    Ensure-Python
    Ensure-Chrome
    Ensure-Venv
    Ensure-Dependencies
    Ensure-EnvFile
    Mark-SetupDone
    Write-Ok "Setup complete."
} else {
    Write-Ok "Setup already done - starting worker."
}

if (-not (Test-ChromeInstalled)) {
    Write-Warn "Chrome missing since last setup."
    Ensure-Chrome
}

Stop-ChromeIfNeeded

Write-Host ""
Write-Info "Starting app.py ..."
Write-Host "Log into WTR-Lab in the Chrome window if asked."
Write-Host "Leave the mouse alone if a Turnstile challenge is being auto-solved."
Write-Host "Press Ctrl+C in this window to stop."
Write-Host ""

& $VenvPython $AppFile
$exitCode = $LASTEXITCODE

Write-Host ""
if ($exitCode -ne 0) {
    Write-Warn "app.py exited with code $exitCode"
} else {
    Write-Ok "Stopped."
}
exit $exitCode
