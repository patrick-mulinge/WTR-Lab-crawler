@echo off
setlocal EnableExtensions
title WTR-Lab Local Worker
cd /d "%~dp0"

REM ============================================================
REM WTR-Lab Local Worker
REM ============================================================
REM
REM This BAT contains the PowerShell setup logic below.
REM
REM Direct downloads use Windows curl.exe:
REM   - Python installer
REM   - Google Chrome installer
REM   - GitHub project ZIP
REM
REM pip continues to use pip for Python packages.
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
# Setup once, then start app.py
# ============================================================

$ErrorActionPreference = "Stop"


# ============================================================
# Project root
# ============================================================

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
# Download URLs
# ============================================================

$GithubZipUrl = "https://github.com/patrick-mulinge/WTR-Lab-crawler/archive/refs/heads/main.zip"

$ChromeUrl = "https://dl.google.com/chrome/install/ChromeStandaloneSetup64.exe"

$PythonVersion = "3.12.10"


# ============================================================
# Output helpers
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

    # Windows 10/11 normally has curl here.
    $systemCurl = Join-Path `
        $env:SystemRoot `
        "System32\curl.exe"


    if (Test-Path -LiteralPath $systemCurl) {
        return $systemCurl
    }


    # Fallback: search PATH.
    try {

        $cmd = Get-Command `
            curl.exe `
            -ErrorAction Stop

        if ($cmd.Source) {
            return $cmd.Source
        }
    }
    catch { }


    Write-Err "Windows curl.exe was not found."

    Write-Host ""
    Write-Host "This script requires curl.exe."
    Write-Host "On normal Windows 10/11 installations it is included."
    Write-Host ""

    exit 1
}


$Curl = Get-CurlPath


# ============================================================
# Generic curl downloader
# ============================================================

function Download-With-Curl {

    param(
        [Parameter(Mandatory = $true)]
        [string]$Url,

        [Parameter(Mandatory = $true)]
        [string]$OutputFile,

        [string]$Description = "file"
    )


    Write-Info "Downloading $Description..."
    Write-Host "    $Url"
    Write-Host ""


    # Remove incomplete previous download.
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
            --connect-timeout 20 `
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

        Write-Err "$Description download appears to be empty or invalid."

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
# Project files
# ============================================================

function Test-ProjectComplete {

    return (
        (Test-Path -LiteralPath $AppFile) -and
        (Test-Path -LiteralPath $ReqFile)
    )
}


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


    $tmpZip = Join-Path `
        $env:TEMP `
        "wtrlab-standalone.zip"


    $tmpDir = Join-Path `
        $env:TEMP `
        "wtrlab-standalone-extract"


    try {

        # ====================================================
        # GitHub ZIP now uses curl.exe
        # ====================================================

        $downloaded = Download-With-Curl `
            -Url $GithubZipUrl `
            -OutputFile $tmpZip `
            -Description "WTR-Lab project from GitHub"


        if (-not $downloaded) {

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

        Write-Err "Project setup failed: $_"

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

function Test-ChromeInstalled {

    $paths = @(
        "${env:ProgramFiles}\Google\Chrome\Application\chrome.exe",
        "${env:ProgramFiles(x86)}\Google\Chrome\Application\chrome.exe",
        "$env:LOCALAPPDATA\Google\Chrome\Application\chrome.exe"
    )


    foreach ($p in $paths) {

        if (Test-Path -LiteralPath $p) {

            return $true
        }
    }


    try {

        $null = Get-Command `
            chrome `
            -ErrorAction Stop

        return $true
    }
    catch { }


    return $false
}


# ============================================================
# Install Chrome
#
# No winget.
# No Invoke-WebRequest.
#
# Uses direct official Google Chrome download via curl.exe.
# ============================================================

function Install-Chrome {

    $ChromeInstaller = Join-Path `
        $env:TEMP `
        "ChromeStandaloneSetup64.exe"


    Write-Info "Downloading official Google Chrome installer..."

    Write-Host "    This installer is approximately 155 MB."
    Write-Host ""


    $downloaded = Download-With-Curl `
        -Url $ChromeUrl `
        -OutputFile $ChromeInstaller `
        -Description "Google Chrome installer"


    if (-not $downloaded) {

        Write-Err "Chrome download failed."

        Write-Host ""
        Write-Host "You can install Chrome manually from:"
        Write-Host "https://www.google.com/chrome/"
        Write-Host ""

        Start-Process `
            "https://www.google.com/chrome/"

        exit 1
    }


    # ========================================================
    # Install Chrome
    # ========================================================

    Write-Info "Installing Google Chrome..."


    try {

        $process = Start-Process `
            -FilePath $ChromeInstaller `
            -ArgumentList "/silent", "/install" `
            -Wait `
            -PassThru


        Write-Host `
            "    Chrome installer exit code: $($process.ExitCode)"
    }
    catch {

        Write-Err "Chrome installation failed: $_"


        Remove-Item `
            -LiteralPath $ChromeInstaller `
            -Force `
            -ErrorAction SilentlyContinue

        exit 1
    }


    # ========================================================
    # Verify installation
    # ========================================================

    Start-Sleep `
        -Seconds 3


    Refresh-SessionPath


    if (Test-ChromeInstalled) {

        Write-Ok "Chrome installed successfully."


        Remove-Item `
            -LiteralPath $ChromeInstaller `
            -Force `
            -ErrorAction SilentlyContinue


        return
    }


    # ========================================================
    # Second check
    # ========================================================

    Write-Warn `
        "Chrome was not detected immediately after installation."


    Write-Info `
        "Checking Chrome installation paths again..."


    Start-Sleep `
        -Seconds 5


    Refresh-SessionPath


    if (Test-ChromeInstalled) {

        Write-Ok "Chrome installed successfully."


        Remove-Item `
            -LiteralPath $ChromeInstaller `
            -Force `
            -ErrorAction SilentlyContinue


        return
    }


    # ========================================================
    # Failure
    # ========================================================

    Write-Err `
        "Chrome installation did not complete."


    Write-Host ""
    Write-Host "Install Chrome manually from:"
    Write-Host "https://www.google.com/chrome/"
    Write-Host ""


    Remove-Item `
        -LiteralPath $ChromeInstaller `
        -Force `
        -ErrorAction SilentlyContinue


    Start-Process `
        "https://www.google.com/chrome/"


    exit 1
}


function Ensure-Chrome {

    if (Test-ChromeInstalled) {

        Write-Ok "Google Chrome found."

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
# Python command execution
# ============================================================

function Invoke-PythonCmd {

    param(
        [Parameter(Mandatory = $true)]
        [string[]]$Command,

        [Parameter(ValueFromRemainingArguments = $true)]
        [string[]]$Rest
    )


    $exe = $Command[0]


    $argsList = New-Object `
        System.Collections.Generic.List[string]


    if ($Command.Count -gt 1) {

        foreach (
            $item in $Command[1..($Command.Count - 1)]
        ) {

            [void]$argsList.Add($item)
        }
    }


    if ($Rest) {

        foreach ($item in $Rest) {

            [void]$argsList.Add($item)
        }
    }


    & $exe @argsList
}


# ============================================================
# Python version check
# ============================================================

function Test-PythonVersionOk {

    param(
        [string[]]$Command
    )


    if (-not $Command -or $Command.Count -eq 0) {

        return $false
    }


    $exe = $Command[0]


    if (
        $exe -match '[\\/]' -and
        -not (Test-Path -LiteralPath $exe)
    ) {

        return $false
    }


    # Ignore Microsoft Store Python stub.
    if (
        $exe -match '\\WindowsApps\\python(\.exe)?$'
    ) {

        return $false
    }


    try {

        $oldEap = $ErrorActionPreference

        $ErrorActionPreference = "Continue"


        Invoke-PythonCmd `
            $Command `
            -c `
            "import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)" `
            2>$null |
            Out-Null


        $ok = (
            $LASTEXITCODE -eq 0
        )


        $ErrorActionPreference = $oldEap


        return $ok
    }
    catch {

        return $false
    }
}


# ============================================================
# Python installation paths
# ============================================================

function Get-PythonInstallPaths {

    $globs = @(
        "$env:LocalAppData\Programs\Python\Python3*\python.exe",
        "$env:ProgramFiles\Python3*\python.exe",
        "${env:ProgramFiles(x86)}\Python3*\python.exe"
    )


    $found = @()


    foreach ($g in $globs) {

        $found += @(
            Get-Item `
                -Path $g `
                -ErrorAction SilentlyContinue |
            Select-Object `
                -ExpandProperty FullName
        )
    }


    return $found
}


# ============================================================
# Find existing Python
# ============================================================

function Find-PythonCommand {

    Refresh-SessionPath


    # --------------------------------------------------------
    # 1. Standard Python paths
    # --------------------------------------------------------

    foreach ($path in (Get-PythonInstallPaths)) {

        if (
            Test-PythonVersionOk @($path)
        ) {

            return @($path)
        }
    }


    # --------------------------------------------------------
    # 2. Python launcher
    # --------------------------------------------------------

    try {

        $py = Get-Command `
            py `
            -ErrorAction Stop


        if (
            Test-PythonVersionOk @(
                $py.Source,
                "-3"
            )
        ) {

            return @(
                $py.Source,
                "-3"
            )
        }
    }
    catch { }


    # --------------------------------------------------------
    # 3. python
    # --------------------------------------------------------

    try {

        $cmd = Get-Command `
            python `
            -ErrorAction Stop


        if (
            $cmd.Source -notmatch '\\WindowsApps\\' -and
            (
                Test-PythonVersionOk @(
                    $cmd.Source
                )
            )
        ) {

            return @(
                $cmd.Source
            )
        }
    }
    catch { }


    # --------------------------------------------------------
    # 4. python3
    # --------------------------------------------------------

    try {

        $cmd = Get-Command `
            python3 `
            -ErrorAction Stop


        if (
            $cmd.Source -notmatch '\\WindowsApps\\' -and
            (
                Test-PythonVersionOk @(
                    $cmd.Source
                )
            )
        ) {

            return @(
                $cmd.Source
            )
        }
    }
    catch { }


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
# Direct download uses curl.exe.
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


    # ========================================================
    # Python download via curl
    # ========================================================

    $downloaded = Download-With-Curl `
        -Url $url `
        -OutputFile $tmp `
        -Description "Python $PythonVersion installer"


    if (-not $downloaded) {

        Write-Warn `
            "Python download failed."

        return $false
    }


    # ========================================================
    # Run installer
    # ========================================================

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

        # ----------------------------------------------------
        # Per-user installation
        # ----------------------------------------------------

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


        # 3010 = installed successfully but reboot required.
        if (
            (
                $p.ExitCode -eq 0 -or
                $p.ExitCode -eq 3010
            ) -and
            (Find-PythonCommand)
        ) {

            if ($p.ExitCode -eq 3010) {

                Write-Warn `
                    "Python installed but Windows wants a reboot to finish."
            }


            return $true
        }


        # ----------------------------------------------------
        # All-users installation
        # ----------------------------------------------------

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
            (
                $p.ExitCode -eq 0 -or
                $p.ExitCode -eq 3010
            ) -and
            (Find-PythonCommand)
        ) {

            if ($p.ExitCode -eq 3010) {

                Write-Warn `
                    "Python installed but Windows wants a reboot to finish."
            }


            return $true
        }


        Write-Warn `
            "Installer exit code: $($p.ExitCode)"


        if ($p.ExitCode -eq 1601) {

            Write-Err `
                "Windows Installer service is unavailable on this PC (error 1601)."


            Write-Host ""
            Write-Host `
                "This is a Windows problem, not this script."


            Write-Host ""
            Write-Host `
                "Fix as Administrator:"


            Write-Host `
                "    sc config msiserver start= demand"


            Write-Host `
                "    sc start msiserver"


            Write-Host ""


            Write-Host `
                "If that doesn't work:"


            Write-Host `
                "    msiexec.exe /unregister"


            Write-Host `
                "    msiexec.exe /regserver"


            Write-Host ""
        }


        if ($p.ExitCode -eq 1603) {

            Write-Warn `
                "Python installer returned error 1603."
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

    # IMPORTANT:
    # Existing Python is checked FIRST.
    #
    # If Python 3.10+ exists, nothing is downloaded.
    # ========================================================

    Write-Info `
        "Checking for an existing Python installation..."


    $found = Find-PythonCommand


    if ($found) {

        $label = (
            $found -join " "
        )


        Write-Host `
            "    Detected Python: $label" `
            -ForegroundColor DarkGray


        Write-Ok `
            "Python found: $label"


        try {

            $ver = Invoke-PythonCmd `
                $found `
                --version `
                2>&1


            if ($ver) {

                Write-Ok "$ver"
            }
        }
        catch { }


        # CRITICAL:
        # Do not download Python again.
        return
    }


    # ========================================================
    # Python genuinely missing
    # ========================================================

    Write-Warn `
        "Python 3.10+ was not found. Installing automatically..."


    $ok = $false


    if (
        Install-PythonViaOfficialInstaller
    ) {

        $ok = $true


        Write-Ok `
            "Python installed via official installer."
    }


    Refresh-SessionPath


    $found = Find-PythonCommand


    if ($found) {

        Write-Ok `
            "Python is ready: $($found -join ' ')"

        return
    }


    # ========================================================
    # Diagnostics
    # ========================================================

    Write-Err `
        "Automatic Python install did not finish."


    Write-Host ""


    Write-Host `
        "=== Diagnostics ===" `
        -ForegroundColor Magenta


    Write-Host `
        "Candidate install paths checked:"


    foreach ($g in @(
        "$env:LocalAppData\Programs\Python\Python3*\python.exe",
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


                Write-Host `
                    "       version check passed: $(Test-PythonVersionOk @($m.FullName))"
            }
        }
    }


    foreach ($cmdName in @(
        "py",
        "python",
        "python3"
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


    Invoke-PythonCmd `
        $py `
        -m `
        venv `
        .venv


    if (
        -not (
            Test-Path `
                -LiteralPath $VenvPython
        )
    ) {

        Write-Err `
            "Failed to create .venv"

        exit 1
    }


    Write-Ok `
        "Virtual environment created."
}


# ============================================================
# Python dependencies
#
# pip manages these downloads.
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
# Read environment variable with default
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


# ============================================================
# Project
# ============================================================

Ensure-ProjectFiles

Ensure-DataDir


# ============================================================
# Setup detection
# ============================================================

$needsSetup = -not (
    Test-SetupDone
)


if ($needsSetup) {

    Write-Info `
        "First-time (or incomplete) setup..."


    # --------------------------------------------------------
    # Python
    # --------------------------------------------------------

    Ensure-Python


    # --------------------------------------------------------
    # Chrome
    # --------------------------------------------------------

    Ensure-Chrome


    # --------------------------------------------------------
    # Virtual environment
    # --------------------------------------------------------

    Ensure-Venv


    # --------------------------------------------------------
    # Dependencies
    # --------------------------------------------------------

    Ensure-Dependencies


    # --------------------------------------------------------
    # .env
    # --------------------------------------------------------

    Ensure-EnvFile


    # --------------------------------------------------------
    # Mark setup complete
    # --------------------------------------------------------

    Mark-SetupDone


    Write-Ok `
        "Setup complete."
}
else {

    Write-Ok `
        "Setup already done - starting worker."
}


# ============================================================
# Check Chrome every time
# ============================================================

if (
    -not (
        Test-ChromeInstalled
    )
) {

    Write-Warn `
        "Chrome missing since last setup."


    Ensure-Chrome
}


# ============================================================
# Chrome processes
# ============================================================

Stop-ChromeIfNeeded


# ============================================================
# Start worker
# ============================================================

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


# ============================================================
# Run worker
# ============================================================

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
