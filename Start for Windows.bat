@echo off
setlocal EnableExtensions
title WTR-Lab Local Worker
cd /d "%~dp0"

REM ============================================================
REM WTR-Lab Local Worker
REM Chrome install uses the official Enterprise MSI (reliable in
REM Windows Sandbox / WDAGUtilityAccount). The consumer EXE
REM installer often returns 0 without actually installing.
REM ============================================================

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

# ============================================================
# WTR-Lab local worker
# ============================================================

$ErrorActionPreference = "Stop"

$ProjectRoot = $env:WTR_PROJECT_ROOT

if (-not $ProjectRoot) {
    $ProjectRoot = (Get-Location).Path
}

$ProjectRoot = $ProjectRoot.TrimEnd("\")

Set-Location -LiteralPath $ProjectRoot


# ============================================================
# Paths
# ============================================================

$VenvPython = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
$VenvPip    = Join-Path $ProjectRoot ".venv\Scripts\pip.exe"
$EnvFile    = Join-Path $ProjectRoot ".env"
$ReqFile    = Join-Path $ProjectRoot "requirements.txt"
$AppFile    = Join-Path $ProjectRoot "app.py"
$Marker     = Join-Path $ProjectRoot "data\.setup_done"


# ============================================================
# URLs
# ============================================================

$GithubZipUrl = "https://github.com/patrick-mulinge/WTR-Lab-crawler/archive/refs/heads/main.zip"

# Official 64-bit Enterprise MSI. The consumer ChromeStandaloneSetup64.exe
# often exits 0 in Windows Sandbox without writing chrome.exe.
$ChromeMsiUrl = "https://dl.google.com/dl/chrome/install/googlechromestandaloneenterprise64.msi"

$PythonVersion = "3.12.10"


# ============================================================
# Output functions
# ============================================================

function Write-Info($msg) {
    Write-Host "[*] $msg" -ForegroundColor Cyan
}

function Write-Ok($msg) {
    Write-Host "[+] $msg" -ForegroundColor Green
}

function Write-Warn($msg) {
    Write-Host "[!] $msg" -ForegroundColor Yellow
}

function Write-Err($msg) {
    Write-Host "[x] $msg" -ForegroundColor Red
}


# ============================================================
# Find curl.exe
# ============================================================

function Get-CurlPath {

    $systemCurl = Join-Path $env:SystemRoot "System32\curl.exe"

    if (Test-Path -LiteralPath $systemCurl) {
        return $systemCurl
    }

    try {
        $curl = Get-Command curl.exe -ErrorAction Stop

        if ($curl.Source) {
            return $curl.Source
        }
    }
    catch {
    }

    Write-Err "curl.exe was not found."

    Write-Host ""
    Write-Host "Windows 10/11 normally includes curl.exe."
    Write-Host "Please make sure C:\Windows\System32 is available."
    Write-Host ""

    exit 1
}


$Curl = Get-CurlPath


# ============================================================
# Download using curl
# ============================================================

function Download-With-Curl {

    param(
        [Parameter(Mandatory = $true)]
        [string]$Url,

        [Parameter(Mandatory = $true)]
        [string]$OutputFile,

        [Parameter(Mandatory = $true)]
        [string]$Description
    )


    Write-Info "Downloading $Description..."
    Write-Host "    $Url"
    Write-Host ""


    if (Test-Path -LiteralPath $OutputFile) {

        Remove-Item `
            -LiteralPath $OutputFile `
            -Force `
            -ErrorAction SilentlyContinue
    }


    try {

        & $Curl `
            -L `
            --fail `
            --show-error `
            --retry 3 `
            --retry-delay 2 `
            --connect-timeout 30 `
            -o $OutputFile `
            $Url


        $exitCode = $LASTEXITCODE


        if ($exitCode -ne 0) {

            throw "curl exited with code $exitCode"
        }
    }
    catch {

        Write-Err "$Description download failed: $_"

        if (Test-Path -LiteralPath $OutputFile) {

            Remove-Item `
                -LiteralPath $OutputFile `
                -Force `
                -ErrorAction SilentlyContinue
        }

        return $false
    }


    if (-not (Test-Path -LiteralPath $OutputFile)) {

        Write-Err "$Description download produced no file."

        return $false
    }


    $size = (
        Get-Item `
            -LiteralPath $OutputFile
    ).Length


    if ($size -lt 1KB) {

        Write-Err "$Description download appears invalid."

        Remove-Item `
            -LiteralPath $OutputFile `
            -Force `
            -ErrorAction SilentlyContinue

        return $false
    }


    $sizeMB = [math]::Round(
        $size / 1MB,
        1
    )


    Write-Ok "$Description downloaded: $sizeMB MB"

    return $true
}


# ============================================================
# Project check
# ============================================================

function Test-ProjectComplete {

    return (
        (Test-Path -LiteralPath $AppFile) -and
        (Test-Path -LiteralPath $ReqFile)
    )
}


# ============================================================
# Download project from GitHub
# ============================================================

function Ensure-ProjectFiles {

    if (Test-ProjectComplete) {
        return
    }


    Write-Warn "app.py or requirements.txt is missing in this folder."
    Write-Host "    Folder: $ProjectRoot"


    if (-not $GithubZipUrl) {

        Write-Err "Cannot auto-download (no GitHub URL configured)."

        Write-Host "Copy the full project folder here, then run again."

        exit 1
    }


    Write-Info "Downloading project from GitHub..."


    $tmpZip = Join-Path `
        $env:TEMP `
        "wtrlab-standalone.zip"


    $tmpDir = Join-Path `
        $env:TEMP `
        "wtrlab-standalone-extract"


    try {

        $ok = Download-With-Curl `
            -Url $GithubZipUrl `
            -OutputFile $tmpZip `
            -Description "WTR-Lab project"


        if (-not $ok) {

            exit 1
        }


        if (Test-Path -LiteralPath $tmpDir) {

            Remove-Item `
                -LiteralPath $tmpDir `
                -Recurse `
                -Force
        }


        Expand-Archive `
            -Path $tmpZip `
            -DestinationPath $tmpDir `
            -Force


        $inner = (
            Get-ChildItem `
                -LiteralPath $tmpDir |
            Where-Object {
                $_.PSIsContainer
            } |
            Select-Object -First 1
        )


        if (-not $inner) {

            throw "Empty archive"
        }


        Get-ChildItem `
            -LiteralPath $inner.FullName `
            -Force |
        ForEach-Object {

            Copy-Item `
                -LiteralPath $_.FullName `
                -Destination (
                    Join-Path $ProjectRoot $_.Name
                ) `
                -Recurse `
                -Force
        }


        Write-Ok "Project files downloaded."
    }
    catch {

        Write-Err "Download failed: $_"

        exit 1
    }
    finally {

        if (Test-Path -LiteralPath $tmpZip) {

            Remove-Item `
                -LiteralPath $tmpZip `
                -Force `
                -ErrorAction SilentlyContinue
        }


        if (Test-Path -LiteralPath $tmpDir) {

            Remove-Item `
                -LiteralPath $tmpDir `
                -Recurse `
                -Force `
                -ErrorAction SilentlyContinue
        }
    }


    if (-not (Test-ProjectComplete)) {

        Write-Err "Still missing app.py after download."

        Write-Host "    Expected: $AppFile"

        exit 1
    }
}


# ============================================================
# Chrome detection
# ============================================================

function Get-ChromeExePath {

    $candidates = @(
        "${env:ProgramFiles}\Google\Chrome\Application\chrome.exe",
        "${env:ProgramFiles(x86)}\Google\Chrome\Application\chrome.exe",
        "$env:LOCALAPPDATA\Google\Chrome\Application\chrome.exe"
    )


    foreach ($p in $candidates) {

        if (Test-Path -LiteralPath $p) {

            return $p
        }
    }


    $regKeys = @(
        "HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\chrome.exe",
        "HKLM:\SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\App Paths\chrome.exe",
        "HKCU:\SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\chrome.exe"
    )


    foreach ($key in $regKeys) {

        try {

            $item = Get-ItemProperty `
                -LiteralPath $key `
                -ErrorAction Stop


            if ($item.'(default)' -and (Test-Path -LiteralPath $item.'(default)')) {

                return $item.'(default)'
            }


            if ($item.Path) {

                $joined = Join-Path $item.Path "chrome.exe"

                if (Test-Path -LiteralPath $joined) {

                    return $joined
                }
            }
        }
        catch {
        }
    }


    $searchRoots = @(
        "${env:ProgramFiles}\Google\Chrome",
        "${env:ProgramFiles(x86)}\Google\Chrome",
        "$env:LOCALAPPDATA\Google\Chrome"
    )


    foreach ($root in $searchRoots) {

        if (-not (Test-Path -LiteralPath $root)) {

            continue
        }


        $found = Get-ChildItem `
            -LiteralPath $root `
            -Recurse `
            -Filter "chrome.exe" `
            -ErrorAction SilentlyContinue |
            Select-Object -First 1


        if ($found) {

            return $found.FullName
        }
    }


    try {

        $chrome = Get-Command `
            chrome.exe `
            -ErrorAction Stop


        if (
            $chrome.Source -and
            (Test-Path -LiteralPath $chrome.Source)
        ) {

            return $chrome.Source
        }
    }
    catch {
    }


    return $null
}


function Test-ChromeInstalled {

    $path = Get-ChromeExePath

    return [bool]$path
}


# ============================================================
# Install Chrome via official Enterprise MSI
#
# Uses curl.exe + msiexec.
# Does NOT use winget.
# Does NOT use the consumer ChromeStandaloneSetup64.exe
# (that installer often reports success without installing
# in Windows Sandbox).
# ============================================================

function Install-Chrome {

    $ChromeMsi = Join-Path `
        $env:TEMP `
        "googlechromestandaloneenterprise64.msi"


    Write-Info "Downloading official Google Chrome Enterprise MSI..."

    Write-Host "    The installer is approximately 160 MB."
    Write-Host "    This method works in Windows Sandbox."
    Write-Host ""


    $ok = Download-With-Curl `
        -Url $ChromeMsiUrl `
        -OutputFile $ChromeMsi `
        -Description "Google Chrome MSI"


    if (-not $ok) {

        Write-Err "Chrome download failed."

        Write-Host ""
        Write-Host "Install Chrome manually from:"
        Write-Host "https://www.google.com/chrome/"
        Write-Host ""

        exit 1
    }


    Write-Info "Installing Google Chrome (silent MSI)..."


    try {

        $log = Join-Path $env:TEMP "wtrlab-chrome-install.log"

        $process = Start-Process `
            -FilePath "msiexec.exe" `
            -ArgumentList @(
                "/i",
                "`"$ChromeMsi`"",
                "/qn",
                "/norestart",
                "/l*v",
                "`"$log`""
            ) `
            -Wait `
            -PassThru


        Write-Host `
            "    Chrome MSI exit code: $($process.ExitCode)"


        # 0 = success, 3010 = success reboot required
        if (
            $process.ExitCode -ne 0 -and
            $process.ExitCode -ne 3010
        ) {

            Write-Warn "MSI returned a non-success code."

            if (Test-Path -LiteralPath $log) {

                Write-Host "    Log: $log"
            }
        }
    }
    catch {

        Write-Err "Chrome installation failed: $_"

        Remove-Item `
            -LiteralPath $ChromeMsi `
            -Force `
            -ErrorAction SilentlyContinue

        exit 1
    }


    # MSI can finish before chrome.exe is fully visible.
    $attempts = @(3, 5, 8, 10)

    foreach ($wait in $attempts) {

        Start-Sleep -Seconds $wait

        Refresh-SessionPath

        if (Test-ChromeInstalled) {

            $exe = Get-ChromeExePath

            Write-Ok "Chrome installed successfully."
            Write-Host "    $exe"

            Remove-Item `
                -LiteralPath $ChromeMsi `
                -Force `
                -ErrorAction SilentlyContinue

            return
        }

        Write-Info "Waiting for chrome.exe to appear..."
    }


    Write-Err "Chrome installation did not complete."

    Write-Host ""
    Write-Host "Install Chrome manually from:"
    Write-Host "https://www.google.com/chrome/"
    Write-Host ""
    Write-Host "Or run this in PowerShell, then re-run this script:"
    Write-Host '  msiexec /i "%TEMP%\googlechromestandaloneenterprise64.msi" /qn /norestart'
    Write-Host ""


    Remove-Item `
        -LiteralPath $ChromeMsi `
        -Force `
        -ErrorAction SilentlyContinue


    exit 1
}


# ============================================================
# Ensure Chrome
# ============================================================

function Ensure-Chrome {

    if (Test-ChromeInstalled) {

        $exe = Get-ChromeExePath

        Write-Ok "Google Chrome found."
        Write-Host "    $exe"

        return
    }


    Write-Warn "Google Chrome was not found."


    $ans = Read-Host `
        "Install Chrome now? (Y/n)"


    if ($ans -match '^[Nn]') {

        Write-Err `
            "Chrome is required. Install it and re-run."

        exit 1
    }


    Install-Chrome
}


# ============================================================
# Refresh PATH
# ============================================================

function Refresh-SessionPath {

    $machine = [Environment]::GetEnvironmentVariable(
        "Path",
        "Machine"
    )


    $user = [Environment]::GetEnvironmentVariable(
        "Path",
        "User"
    )


    $parts = @()


    if ($machine) {
        $parts += $machine
    }


    if ($user) {
        $parts += $user
    }


    if ($parts.Count -gt 0) {

        $env:Path = (
            $parts -join ";"
        )
    }
}


# ============================================================
# PYTHON DETECTION
#
# IMPORTANT:
# We directly execute python.exe --version.
#
# This avoids the previous bug where your working Python 3.12.10
# was incorrectly reported as failing the version check.
# ============================================================

function Test-PythonVersionOk {

    param(
        [Parameter(Mandatory = $true)]
        [string]$PythonExe
    )


    if (-not $PythonExe) {

        return $false
    }


    if (-not (Test-Path -LiteralPath $PythonExe)) {

        return $false
    }


    try {

        $versionOutput = & $PythonExe --version 2>&1


        if (-not $versionOutput) {

            return $false
        }


        $versionText = $versionOutput.ToString().Trim()


        if (
            $versionText -match `
            '^Python\s+(\d+)\.(\d+)\.(\d+)'
        ) {

            $major = [int]$Matches[1]
            $minor = [int]$Matches[2]


            if (
                ($major -gt 3) -or
                (
                    ($major -eq 3) -and
                    ($minor -ge 10)
                )
            ) {

                return $true
            }
        }


        return $false
    }
    catch {

        return $false
    }
}


# ============================================================
# Find installed Python
# ============================================================

function Find-PythonCommand {

    Refresh-SessionPath


    $knownPaths = @(
        "$env:LOCALAPPDATA\Programs\Python\Python312\python.exe",
        "$env:LOCALAPPDATA\Programs\Python\Python311\python.exe",
        "$env:LOCALAPPDATA\Programs\Python\Python310\python.exe"
    )


    foreach ($path in $knownPaths) {

        if (
            Test-PythonVersionOk `
                -PythonExe $path
        ) {

            return $path
        }
    }


    $patterns = @(
        "$env:LOCALAPPDATA\Programs\Python\Python3*\python.exe",
        "$env:ProgramFiles\Python3*\python.exe",
        "${env:ProgramFiles(x86)}\Python3*\python.exe"
    )


    foreach ($pattern in $patterns) {

        $matches = @(
            Get-Item `
                -Path $pattern `
                -ErrorAction SilentlyContinue
        )


        foreach ($item in $matches) {

            if (
                Test-PythonVersionOk `
                    -PythonExe $item.FullName
            ) {

                return $item.FullName
            }
        }
    }


    try {

        $cmd = Get-Command `
            python.exe `
            -ErrorAction Stop


        $path = $cmd.Source


        if (
            $path -and
            ($path -notmatch '\\WindowsApps\\')
        ) {

            if (
                Test-PythonVersionOk `
                    -PythonExe $path
            ) {

                return $path
            }
        }
    }
    catch {
    }


    try {

        $cmd = Get-Command `
            python3.exe `
            -ErrorAction Stop


        $path = $cmd.Source


        if (
            $path -and
            ($path -notmatch '\\WindowsApps\\')
        ) {

            if (
                Test-PythonVersionOk `
                    -PythonExe $path
            ) {

                return $path
            }
        }
    }
    catch {
    }


    try {

        $py = Get-Command `
            py.exe `
            -ErrorAction Stop


        $output = & $py.Source -3 --version 2>&1


        if (
            $output -match `
            '^Python\s+3\.(\d+)\.'
        ) {

            $minor = [int]$Matches[1]


            if ($minor -ge 10) {

                $realPython = & $py.Source -3 -c `
                    "import sys; print(sys.executable)" `
                    2>$null


                if (
                    $realPython -and
                    (Test-Path -LiteralPath $realPython)
                ) {

                    return $realPython.Trim()
                }
            }
        }
    }
    catch {
    }


    return $null
}


# ============================================================
# Python architecture
# ============================================================

function Get-PythonArchSuffix {

    $arch = $env:PROCESSOR_ARCHITECTURE


    if ($arch -eq "ARM64") {

        return "arm64"
    }


    return "amd64"
}


# ============================================================
# Install Python
#
# Uses curl.exe.
# ============================================================

function Install-PythonViaOfficialInstaller {

    $suffix = Get-PythonArchSuffix


    $fileName = `
        "python-$PythonVersion-$suffix.exe"


    $url = `
        "https://www.python.org/ftp/python/$PythonVersion/$fileName"


    $tmp = Join-Path `
        $env:TEMP `
        $fileName


    Write-Info `
        "Downloading official Python $PythonVersion ($suffix)..."


    Write-Host `
        "    $url"


    $downloaded = Download-With-Curl `
        -Url $url `
        -OutputFile $tmp `
        -Description "Python $PythonVersion installer"


    if (-not $downloaded) {

        return $false
    }


    Write-Info `
        "Running silent Python installer (Add to PATH, pip, py launcher)..."


    Write-Host `
        "    If Windows shows a permission/consent prompt, accept it."


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

        $p = Start-Process `
            -FilePath $tmp `
            -ArgumentList (
                $common + @(
                    "InstallAllUsers=0"
                )
            ) `
            -Wait `
            -PassThru


        Refresh-SessionPath


        if (
            $p.ExitCode -eq 0 -or
            $p.ExitCode -eq 3010
        ) {

            Start-Sleep -Seconds 2


            $found = Find-PythonCommand


            if ($found) {

                if ($p.ExitCode -eq 3010) {

                    Write-Warn `
                        "Python installed but Windows requested a reboot."
                }


                return $true
            }
        }


        Write-Warn `
            "Per-user install did not register Python."


        Write-Warn `
            "Retrying for all users..."


        $p = Start-Process `
            -FilePath $tmp `
            -ArgumentList (
                $common + @(
                    "InstallAllUsers=1"
                )
            ) `
            -Wait `
            -PassThru


        Refresh-SessionPath


        if (
            $p.ExitCode -eq 0 -or
            $p.ExitCode -eq 3010
        ) {

            Start-Sleep -Seconds 2


            $found = Find-PythonCommand


            if ($found) {

                if ($p.ExitCode -eq 3010) {

                    Write-Warn `
                        "Python installed but Windows requested a reboot."
                }


                return $true
            }
        }


        Write-Warn `
            "Installer exit code: $($p.ExitCode)"


        if ($p.ExitCode -eq 1603) {

            Write-Warn `
                "Python installer returned error 1603."


            Write-Host ""
            Write-Host `
                "This can occur when an existing Python installation conflicts"
            Write-Host `
                "with the requested installation or Windows installer state."
        }


        return $false
    }
    catch {

        Write-Warn `
            "Official Python installer failed: $_"


        return $false
    }
    finally {

        Remove-Item `
            -LiteralPath $tmp `
            -Force `
            -ErrorAction SilentlyContinue
    }
}


# ============================================================
# Ensure Python
# ============================================================

function Ensure-Python {

    Write-Info `
        "Checking for an existing Python installation..."


    $found = Find-PythonCommand


    if ($found) {

        Write-Host `
            "    Detected Python: $found"


        Write-Ok `
            "Python found: $found"


        try {

            $version = & $found --version 2>&1


            if ($version) {

                Write-Ok `
                    "$version"
            }
        }
        catch {
        }


        return
    }


    Write-Warn `
        "Python 3.10+ was not found. Installing automatically..."


    $ok = Install-PythonViaOfficialInstaller


    if ($ok) {

        Refresh-SessionPath


        $found = Find-PythonCommand


        if ($found) {

            Write-Ok `
                "Python is ready: $found"

            return
        }
    }


    Write-Err `
        "Automatic Python install did not finish."


    Write-Host ""


    Write-Host `
        "=== Diagnostics ===" `
        -ForegroundColor Magenta


    Write-Host `
        "Candidate install paths checked:"


    foreach ($g in @(
        "$env:LOCALAPPDATA\Programs\Python\Python3*\python.exe",
        "$env:ProgramFiles\Python3*\python.exe",
        "${env:ProgramFiles(x86)}\Python3*\python.exe"
    )) {

        Write-Host `
            "  glob: $g"


        $matches = @(
            Get-Item `
                -Path $g `
                -ErrorAction SilentlyContinue
        )


        if ($matches.Count -eq 0) {

            Write-Host `
                "    -> no matches"
        }
        else {

            foreach ($m in $matches) {

                Write-Host `
                    "    -> found: $($m.FullName)"


                try {

                    $v = & $m.FullName --version 2>&1


                    Write-Host `
                        "       $v"
                }
                catch {
                }
            }
        }
    }


    Write-Host ""


    foreach ($cmdName in @(
        "python",
        "python3",
        "py"
    )) {

        try {

            $c = Get-Command `
                $cmdName `
                -ErrorAction Stop


            Write-Host `
                "  '$cmdName' on PATH -> $($c.Source)"
        }
        catch {

            Write-Host `
                "  '$cmdName' on PATH -> not found"
        }
    }


    Write-Host `
        "===================="


    Write-Host ""


    Write-Host `
        "Download: https://www.python.org/downloads/"


    Write-Host `
        "During setup, enable: Add python.exe to PATH"


    Write-Host `
        "Then close this window and run the script again."


    Start-Process `
        "https://www.python.org/downloads/"


    exit 1
}


# ============================================================
# Get Python command
# ============================================================

function Get-PythonCmd {

    $found = Find-PythonCommand


    if ($found) {

        return $found
    }


    Write-Err `
        "Need Python 3.10 or newer."


    exit 1
}


# ============================================================
# Virtual environment
# ============================================================

function Ensure-Venv {

    if (
        Test-Path `
            -LiteralPath $VenvPython
    ) {

        Write-Ok `
            "Virtual environment already exists."

        return
    }


    Write-Info `
        "Creating virtual environment (.venv)..."


    $py = Get-PythonCmd


    & $py `
        -m `
        venv `
        .venv


    if ($LASTEXITCODE -ne 0) {

        Write-Err `
            "Failed to create .venv."

        exit 1
    }


    if (
        -not (
            Test-Path `
                -LiteralPath $VenvPython
        )
    ) {

        Write-Err `
            "Failed to create .venv."

        exit 1
    }


    Write-Ok `
        "Virtual environment created."
}


# ============================================================
# Dependencies
# ============================================================

function Ensure-Dependencies {

    Write-Info `
        "Installing / updating Python packages (may take a few minutes)..."


    & $VenvPython `
        -m `
        pip `
        install `
        --upgrade `
        pip


    if ($LASTEXITCODE -ne 0) {

        Write-Err `
            "Failed to upgrade pip."

        exit 1
    }


    & $VenvPip `
        install `
        -r `
        $ReqFile


    if ($LASTEXITCODE -ne 0) {

        Write-Err `
            "pip install failed."

        exit 1
    }


    Write-Ok `
        "Dependencies installed."
}


# ============================================================
# Read environment value
# ============================================================

function Read-EnvDefault(
    [string]$prompt,
    [string]$default = ""
) {

    if ($default -ne "") {

        $line = Read-Host `
            "$prompt [$default]"


        if (
            [string]::IsNullOrWhiteSpace($line)
        ) {

            return $default
        }


        return $line.Trim()
    }


    $line = Read-Host `
        $prompt


    return $line.Trim()
}


# ============================================================
# .env configuration
# ============================================================

function Ensure-EnvFile {

    if (
        Test-Path `
            -LiteralPath $EnvFile
    ) {

        $tokenLine = (
            Get-Content `
                $EnvFile `
                -ErrorAction SilentlyContinue |
            Where-Object {
                $_ -match '^\s*BOT_TOKEN\s*='
            } |
            Select-Object -First 1
        )


        if (
            $tokenLine -and
            $tokenLine -notmatch 'BOT_TOKEN\s*=\s*$' -and
            $tokenLine -notmatch 'your_token_here'
        ) {

            Write-Ok `
                ".env already configured."

            return
        }


        Write-Warn `
            ".env exists but BOT_TOKEN looks empty - reconfiguring."
    }


    Write-Host ""


    Write-Host `
        "=== Configure your bot ===" `
        -ForegroundColor Magenta


    Write-Host `
        "Create a bot with @BotFather in Telegram, then paste the token."


    Write-Host `
        "Press Enter on optional fields to leave them empty."


    Write-Host ""


    do {

        $token = Read-Host `
            "BOT_TOKEN (required)"


        $token = $token.Trim()


        if (-not $token) {

            Write-Warn `
                "Token is required."
        }

    } while (-not $token)


    Write-Host ""


    Write-Host `
        "ALLOWED_USER_IDS - your numeric Telegram id(s), comma-separated."


    Write-Host `
        "  Leave empty (press Enter) to allow anyone who can message the bot."


    Write-Host `
        "  Get an id from @userinfobot if you want a hard lock."


    $allowed = Read-Host `
        "ALLOWED_USER_IDS (optional, Enter = open)"


    $allowed = $allowed.Trim()


    Write-Host ""


    Write-Host `
        "CHAPTER_CAP - max chapters per download. 0 = unlimited (recommended)."


    $cap = Read-EnvDefault `
        "CHAPTER_CAP" `
        "0"


    if (
        $cap -notmatch '^\d+$'
    ) {

        $cap = "0"
    }


    Write-Host ""


    Write-Host `
        "DAILY_TASK_LIMIT - max tasks per user per day. 0 = unlimited."


    $daily = Read-EnvDefault `
        "DAILY_TASK_LIMIT" `
        "0"


    if (
        $daily -notmatch '^\d+$'
    ) {

        $daily = "0"
    }


    Write-Host ""


    Write-Host `
        "Chapter delay (seconds). Defaults 10-18 are safer against Cloudflare."


    $tmin = Read-EnvDefault `
        "CHAPTER_THROTTLE_MIN" `
        "10"


    $tmax = Read-EnvDefault `
        "CHAPTER_THROTTLE_MAX" `
        "18"


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


    Set-Content `
        -Path $EnvFile `
        -Value $content `
        -Encoding UTF8


    Write-Ok `
        ".env written."
}


# ============================================================
# Data directory
# ============================================================

function Ensure-DataDir {

    $data = Join-Path `
        $ProjectRoot `
        "data"


    if (
        -not (
            Test-Path `
                -LiteralPath $data
        )
    ) {

        New-Item `
            -ItemType Directory `
            -Path $data |
        Out-Null
    }
}


# ============================================================
# Setup marker
# ============================================================

function Mark-SetupDone {

    Ensure-DataDir


    Set-Content `
        -Path $Marker `
        -Value (
            Get-Date -Format "o"
        ) `
        -Encoding UTF8
}


function Test-SetupDone {

    return (
        (Test-Path -LiteralPath $Marker) -and
        (Test-Path -LiteralPath $VenvPython) -and
        (Test-Path -LiteralPath $EnvFile)
    )
}


# ============================================================
# Chrome process handling
# ============================================================

function Stop-ChromeIfNeeded {

    Write-Host ""


    Write-Warn `
        "Close other Chrome windows before the worker starts."


    Write-Host `
        "The worker uses its own profile under data\chrome-profile."


    $ans = Read-Host `
        "Try to close all Chrome processes now? (y/N)"


    if ($ans -match '^[Yy]') {

        Get-Process `
            chrome `
            -ErrorAction SilentlyContinue |
        Stop-Process `
            -Force `
            -ErrorAction SilentlyContinue


        Start-Sleep `
            -Seconds 2


        Write-Ok `
            "Chrome processes signaled to close."
    }
}


# ============================================================
# MAIN
# ============================================================

Write-Host ""


Write-Host `
    "========================================" `
    -ForegroundColor Cyan


Write-Host `
    "  WTR-Lab Local Worker" `
    -ForegroundColor Cyan


Write-Host `
    "========================================" `
    -ForegroundColor Cyan


Write-Host ""


Ensure-ProjectFiles

Ensure-DataDir


$needsSetup = -not (
    Test-SetupDone
)


if ($needsSetup) {

    Write-Info `
        "First-time (or incomplete) setup..."


    Ensure-Python

    Ensure-Chrome

    Ensure-Venv

    Ensure-Dependencies

    Ensure-EnvFile

    Mark-SetupDone


    Write-Ok `
        "Setup complete."
}
else {

    Write-Ok `
        "Setup already done - starting worker."
}


if (
    -not (
        Test-ChromeInstalled
    )
) {

    Write-Warn `
        "Chrome missing since last setup."


    Ensure-Chrome
}


Stop-ChromeIfNeeded


Write-Host ""


Write-Info `
    "Starting app.py ..."


Write-Host `
    "Log into WTR-Lab in the Chrome window if asked."


Write-Host `
    "Leave the mouse alone if a Turnstile challenge is being auto-solved."


Write-Host `
    "Press Ctrl+C in this window to stop."


Write-Host ""


& $VenvPython `
    $AppFile


$exitCode = $LASTEXITCODE


Write-Host ""


if ($exitCode -ne 0) {

    Write-Warn `
        "app.py exited with code $exitCode"
}
else {

    Write-Ok `
        "Stopped."
}


exit $exitCode
