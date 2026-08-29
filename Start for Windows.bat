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

# Pinned official installer used only if winget is missing / fails.
# 3.12 is widely compatible with SeleniumBase / this worker.
$PythonVersion = "3.12.10"
$PythonWingetIds = @(
    "Python.Python.3.12",
    "Python.Python.3.13",
    "Python.Python.3.11"
)

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

function Refresh-SessionPath {
    $machine = [Environment]::GetEnvironmentVariable("Path", "Machine")
    $user    = [Environment]::GetEnvironmentVariable("Path", "User")
    $parts = @()
    if ($machine) { $parts += $machine }
    if ($user)    { $parts += $user }
    if ($parts.Count -gt 0) {
        $env:Path = ($parts -join ";")
    }
}

function Invoke-PythonCmd {
    param(
        [Parameter(Mandatory = $true)]
        [string[]]$Command,
        [Parameter(ValueFromRemainingArguments = $true)]
        [string[]]$Rest
    )
    $exe = $Command[0]
    $argsList = New-Object System.Collections.Generic.List[string]
    if ($Command.Count -gt 1) {
        foreach ($item in $Command[1..($Command.Count - 1)]) {
            [void]$argsList.Add($item)
        }
    }
    if ($Rest) {
        foreach ($item in $Rest) { [void]$argsList.Add($item) }
    }
    & $exe @argsList
}

function Test-PythonVersionOk {
    param([string[]]$Command)
    if (-not $Command -or $Command.Count -eq 0) { return $false }
    $exe = $Command[0]
    if ($exe -match '[\\/]' -and -not (Test-Path -LiteralPath $exe)) { return $false }
    # Skip the Microsoft Store stub (opens the Store instead of Python).
    if ($exe -match '\\WindowsApps\\python(\.exe)?$') { return $false }
    try {
        $oldEap = $ErrorActionPreference
        $ErrorActionPreference = "Continue"
        Invoke-PythonCmd $Command -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)" 2>$null | Out-Null
        $ok = ($LASTEXITCODE -eq 0)
        $ErrorActionPreference = $oldEap
        return $ok
    } catch {
        return $false
    }
}

function Get-PythonInstallPaths {
    $globs = @(
        "$env:LocalAppData\Programs\Python\Python3*\python.exe",
        "$env:ProgramFiles\Python3*\python.exe",
        "${env:ProgramFiles(x86)}\Python3*\python.exe"
    )
    $found = @()
    foreach ($g in $globs) {
        $found += @(Get-Item -Path $g -ErrorAction SilentlyContinue | Select-Object -ExpandProperty FullName)
    }
    return $found
}

function Find-PythonCommand {
    Refresh-SessionPath

    foreach ($path in (Get-PythonInstallPaths)) {
        if (Test-PythonVersionOk @($path)) { return @($path) }
    }

    try {
        $py = Get-Command py -ErrorAction Stop
        if (Test-PythonVersionOk @($py.Source, "-3")) { return @($py.Source, "-3") }
    } catch { }

    try {
        $cmd = Get-Command python -ErrorAction Stop
        if ($cmd.Source -notmatch '\\WindowsApps\\' -and (Test-PythonVersionOk @($cmd.Source))) {
            return @($cmd.Source)
        }
    } catch { }

    try {
        $cmd = Get-Command python3 -ErrorAction Stop
        if ($cmd.Source -notmatch '\\WindowsApps\\' -and (Test-PythonVersionOk @($cmd.Source))) {
            return @($cmd.Source)
        }
    } catch { }

    return $null
}

function Get-PythonArchSuffix {
    $arch = $env:PROCESSOR_ARCHITECTURE
    if ($arch -eq "ARM64") { return "arm64" }
    return "amd64"
}

function Install-PythonViaWinget {
    try {
        $null = Get-Command winget -ErrorAction Stop
    } catch {
        Write-Warn "winget is not available."
        return $false
    }

    foreach ($id in $PythonWingetIds) {
        Write-Info "Trying winget install $id ..."
        try {
            $oldEap = $ErrorActionPreference
            $ErrorActionPreference = "Continue"
            & winget install -e --id $id --accept-package-agreements --accept-source-agreements --disable-interactivity
            $code = $LASTEXITCODE
            $ErrorActionPreference = $oldEap
            # 0 = installed, -1978335189 / 0x8A15002B often means already installed
            if ($code -eq 0 -or $code -eq -1978335189) {
                Refresh-SessionPath
                if (Find-PythonCommand) { return $true }
            }
        } catch {
            Write-Warn "winget $id failed: $_"
        }
    }
    return $false
}

function Install-PythonViaOfficialInstaller {
    $suffix = Get-PythonArchSuffix
    $fileName = "python-$PythonVersion-$suffix.exe"
    $url = "https://www.python.org/ftp/python/$PythonVersion/$fileName"
    $tmp = Join-Path $env:TEMP $fileName

    Write-Info "Downloading official Python $PythonVersion ($suffix)..."
    Write-Host "    $url"
    try {
        Invoke-WebRequest -Uri $url -OutFile $tmp -UseBasicParsing
    } catch {
        Write-Warn "Download failed: $_"
        return $false
    }

    if (-not (Test-Path -LiteralPath $tmp) -or ((Get-Item $tmp).Length -lt 1MB)) {
        Write-Warn "Downloaded installer looks invalid."
        return $false
    }

    Write-Info "Running silent Python installer (Add to PATH, pip, py launcher)..."
    Write-Host "    Windows may show a UAC prompt — accept it."

    $common = @(
        "/quiet",
        "PrependPath=1",
        "Include_pip=1",
        "Include_launcher=1",
        "Include_test=0",
        "Include_doc=0",
        "SimpleInstall=1"
    )

    try {
        $p = Start-Process -FilePath $tmp -ArgumentList ($common + @("InstallAllUsers=0")) -Wait -PassThru
        Refresh-SessionPath
        if ($p.ExitCode -eq 0 -and (Find-PythonCommand)) { return $true }

        Write-Warn "Per-user install did not register Python. Retrying for all users..."
        $p = Start-Process -FilePath $tmp -ArgumentList ($common + @("InstallAllUsers=1")) -Wait -PassThru
        Refresh-SessionPath
        if ($p.ExitCode -eq 0 -and (Find-PythonCommand)) { return $true }

        Write-Warn "Installer exit code: $($p.ExitCode)"
        return $false
    } catch {
        Write-Warn "Official installer failed: $_"
        return $false
    } finally {
        Remove-Item -LiteralPath $tmp -Force -ErrorAction SilentlyContinue
    }
}

function Ensure-Python {
    $found = Find-PythonCommand
    if ($found) {
        $label = ($found -join " ")
        Write-Ok "Python found: $label"
        try {
            $ver = Invoke-PythonCmd $found --version 2>&1
            if ($ver) { Write-Ok "$ver" }
        } catch { }
        return
    }

    Write-Warn "Python 3.10+ was not found. Installing automatically..."

    $ok = $false
    if (Install-PythonViaWinget) {
        $ok = $true
        Write-Ok "Python installed via winget."
    } elseif (Install-PythonViaOfficialInstaller) {
        $ok = $true
        Write-Ok "Python installed via official installer."
    }

    Refresh-SessionPath
    $found = Find-PythonCommand
    if ($found) {
        Write-Ok "Python is ready: $($found -join ' ')"
        return
    }

    Write-Err "Automatic Python install did not finish."
    Write-Host "Download: https://www.python.org/downloads/"
    Write-Host "During setup, enable: Add python.exe to PATH"
    Write-Host "Then close this window and run the script again."
    Start-Process "https://www.python.org/downloads/"
    exit 1
}

function Get-PythonCmd {
    $found = Find-PythonCommand
    if ($found) { return $found }
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
    Invoke-PythonCmd $py -m venv .venv
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
