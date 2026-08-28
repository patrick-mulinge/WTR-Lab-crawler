#!/usr/bin/env bash
# WTR-Lab local worker — Oracle Linux / RHEL / Rocky / Alma (dnf only)
#
# Usage:
#   chmod +x "Start for Linux.sh"
#   ./"Start for Linux.sh"
#
# Supported:
#   - Oracle Linux 8/9 (dnf)
#   - RHEL / Rocky / Alma / CentOS Stream (dnf)
#
# IMPORTANT:
#   Run this script WITHOUT sudo.
#   It will request sudo only when installing system packages.
#
# Non-interactive / headless VPS:
#   Export BOT_TOKEN (and optional settings) before running, or pre-create .env.
#   Example:
#     export BOT_TOKEN="123456:ABC..."
#     export ALLOWED_USER_IDS="111111111"
#     export HEADLESS=1
#     ./"Start for Linux.sh"

set -euo pipefail
cd "$(dirname "$0")"

VENV_PY="./.venv/bin/python"
ENV_FILE="./.env"
REQ_FILE="./requirements.txt"
APP_FILE="./app.py"
MARKER="./data/.setup_done"

# Optional GitHub zip URL if the project folder is incomplete.
GITHUB_ZIP_URL=""

IS_INTERACTIVE=0

info()  { echo "[*] $*"; }
ok()    { echo "[+] $*"; }
warn()  { echo "[!] $*"; }
err()   { echo "[x] $*" >&2; }


# =========================================================
# INTERACTIVE / TTY DETECTION
# =========================================================

detect_interactive() {
  if [[ -t 0 ]] && [[ -t 1 ]]; then
    IS_INTERACTIVE=1
  else
    IS_INTERACTIVE=0
  fi
}


prompt_or_env() {
  local var="$1"
  local prompt="$2"
  local default="${3-}"
  local current="${!var-}"

  if [[ -n "$current" ]]; then
    printf -v "$var" '%s' "$current"
    return
  fi

  if [[ "$IS_INTERACTIVE" -eq 1 ]]; then
    local answer=""
    if [[ -n "$default" ]]; then
      read -r -p "$prompt [$default]: " answer || true
      answer="${answer:-$default}"
    else
      read -r -p "$prompt: " answer || true
    fi
    printf -v "$var" '%s' "$(echo "$answer" | xargs)"
  else
    printf -v "$var" '%s' "$default"
  fi
}


require_token_noninteractive() {
  if [[ "$IS_INTERACTIVE" -eq 1 ]]; then
    return
  fi
  if [[ -f "$ENV_FILE" ]] && grep -qE '^BOT_TOKEN=.+' "$ENV_FILE" \
      && ! grep -q 'your_token_here' "$ENV_FILE"; then
    return
  fi
  if [[ -n "${BOT_TOKEN:-}" ]]; then
    return
  fi
  err "Non-interactive session (no TTY) and BOT_TOKEN is not set."
  err "Either:"
  err "  1) export BOT_TOKEN='your_token_from_BotFather'"
  err "  2) create .env with BOT_TOKEN=... before running"
  err "  3) run this script from an interactive SSH session"
  exit 1
}


# =========================================================
# PROJECT CHECK
# =========================================================

project_ok() {
  [[ -f "$APP_FILE" && -f "$REQ_FILE" ]]
}


ensure_project() {
  if project_ok; then
    ok "Project files found."
    return
  fi

  warn "app.py or requirements.txt is missing."

  if [[ -z "$GITHUB_ZIP_URL" ]]; then
    err "No GitHub URL configured."
    err "Copy the complete project folder here and run again."
    exit 1
  fi

  info "Downloading project from GitHub..."

  tmpzip="$(mktemp)"
  tmpdir="$(mktemp -d)"

  cleanup_project() {
    rm -rf "$tmpzip" "$tmpdir"
  }
  trap cleanup_project EXIT

  curl -fsSL "$GITHUB_ZIP_URL" -o "$tmpzip"
  unzip -q "$tmpzip" -d "$tmpdir"

  inner="$(find "$tmpdir" -mindepth 1 -maxdepth 1 -type d | head -1)"
  if [[ -z "$inner" ]]; then
    err "Downloaded archive is empty or invalid."
    exit 1
  fi

  cp -R "$inner"/* .

  if ! project_ok; then
    err "Download incomplete. app.py or requirements.txt is still missing."
    exit 1
  fi

  trap - EXIT
  cleanup_project
  ok "Project files downloaded."
}


# =========================================================
# OPERATING SYSTEM CHECK (dnf only)
# =========================================================

detect_os() {
  if [[ ! -f /etc/os-release ]]; then
    err "Cannot determine Linux distribution (/etc/os-release missing)."
    exit 1
  fi

  # shellcheck disable=SC1091
  source /etc/os-release

  if ! command -v dnf >/dev/null 2>&1; then
    err "This script requires dnf (Oracle Linux / RHEL / Rocky / Alma)."
    err "Detected package manager is missing or not dnf."
    exit 1
  fi

  ok "OS: ${PRETTY_NAME:-$ID}  (dnf)"
}


# =========================================================
# SUDO
# =========================================================

ensure_sudo() {
  if [[ $EUID -eq 0 ]]; then
    warn "Do not run this script with sudo / as root."
    echo
    echo "Run it normally as a regular user:"
    echo
    echo '  ./"Start for Linux.sh"'
    echo
    warn "The script will request sudo only when installing packages."
    exit 1
  fi

  if ! command -v sudo >/dev/null 2>&1; then
    err "sudo is required to install system packages."
    exit 1
  fi

  info "Checking sudo access..."
  sudo -v
  ok "sudo access confirmed."
}


# =========================================================
# PACKAGE INSTALLERS (dnf only)
# =========================================================

PKG_UPDATED=0

pkg_installed() {
  rpm -q "$1" >/dev/null 2>&1
}


pkg_install() {
  local packages=("$@")
  local missing=()

  for package in "${packages[@]}"; do
    if pkg_installed "$package"; then
      ok "$package already installed."
    else
      missing+=("$package")
    fi
  done

  if [[ ${#missing[@]} -eq 0 ]]; then
    return
  fi

  info "Installing: ${missing[*]}"

  if [[ $PKG_UPDATED -eq 0 ]]; then
    sudo dnf -y makecache || true
    PKG_UPDATED=1
  fi
  sudo dnf install -y "${missing[@]}"
}


# =========================================================
# BASIC TOOLS
# =========================================================

ensure_basic_tools() {
  info "Checking required system tools..."
  pkg_install ca-certificates curl wget unzip gnupg2
  ok "Basic system tools ready."
}


# =========================================================
# PYTHON
# =========================================================

python_version_ok() {
  command -v python3 >/dev/null 2>&1 || return 1
  python3 -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)' 2>/dev/null
}


ensure_python() {
  if python_version_ok; then
    ok "Python: $(python3 --version)"
  else
    warn "Python 3.10+ not found (or too old). Installing..."

    # OL9 / RHEL9: python3 is usually recent enough.
    # OL8 may need module streams.
    pkg_install python3 python3-pip python3-devel || true

    if ! python_version_ok; then
      info "Trying Python module streams (Oracle Linux 8 style)..."
      sudo dnf module enable -y python39 2>/dev/null || true
      sudo dnf module enable -y python3.11 2>/dev/null || true
      sudo dnf module enable -y python3.12 2>/dev/null || true
      sudo dnf install -y python39 python39-pip python39-devel 2>/dev/null || true
      sudo dnf install -y python3.11 python3.11-pip python3.11-devel 2>/dev/null || true
      sudo dnf install -y python3.12 python3.12-pip python3.12-devel 2>/dev/null || true

      for cand in python3.12 python3.11 python3.10 python3.9 python3; do
        if command -v "$cand" >/dev/null 2>&1; then
          if "$cand" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 9) else 1)' 2>/dev/null; then
            export WTR_PYTHON_BIN="$cand"
            break
          fi
        fi
      done
    fi

    # Build deps some pip wheels need
    pkg_install gcc gcc-c++ make libffi-devel openssl-devel 2>/dev/null || true
  fi

  if [[ -z "${WTR_PYTHON_BIN:-}" ]]; then
    if command -v python3 >/dev/null 2>&1; then
      WTR_PYTHON_BIN="python3"
    else
      err "python3 is not available after install attempts."
      exit 1
    fi
  fi

  local ver
  ver="$("$WTR_PYTHON_BIN" -c 'import sys; print("%d.%d" % sys.version_info[:2])')"
  ok "Using $WTR_PYTHON_BIN ($ver) for the virtual environment."

  if ! "$WTR_PYTHON_BIN" -m venv --help >/dev/null 2>&1; then
    info "Ensuring venv module is available..."
    pkg_install python3 || true
  fi

  if ! "$WTR_PYTHON_BIN" -m venv --help >/dev/null 2>&1; then
    err "Python venv module is not available ($WTR_PYTHON_BIN -m venv failed)."
    err "On Oracle Linux 8 try: sudo dnf module install -y python39"
    exit 1
  fi

  if ! "$WTR_PYTHON_BIN" -m pip --version >/dev/null 2>&1; then
    pkg_install python3-pip || true
  fi

  ok "Python environment ready."
}


# =========================================================
# GOOGLE CHROME
# =========================================================

chrome_installed() {
  command -v google-chrome >/dev/null 2>&1 \
    || command -v google-chrome-stable >/dev/null 2>&1
}


chrome_arch() {
  local m
  m="$(uname -m)"
  case "$m" in
    x86_64|amd64) echo "x86_64" ;;
    aarch64|arm64) echo "aarch64" ;;
    *) echo "$m" ;;
  esac
}


ensure_chrome() {
  if chrome_installed; then
    CHROME_CMD="$(command -v google-chrome || command -v google-chrome-stable)"
    ok "Google Chrome found: $CHROME_CMD"
    return
  fi

  local arch
  arch="$(chrome_arch)"
  if [[ "$arch" != "x86_64" ]]; then
    err "Google Chrome official packages are only published for x86_64."
    err "This VPS architecture is: $arch"
    err "Use an x86_64 (AMD/Intel) shape, or install Chromium another way."
    exit 1
  fi

  warn "Google Chrome not found."
  info "Installing Google Chrome..."

  local tmp_rpm
  tmp_rpm="$(mktemp --suffix=.rpm)"
  cleanup_chrome_rpm() { rm -f "$tmp_rpm"; }
  trap cleanup_chrome_rpm EXIT

  info "Downloading Google Chrome RPM..."
  curl -fL \
    "https://dl.google.com/linux/direct/google-chrome-stable_current_x86_64.rpm" \
    -o "$tmp_rpm"

  info "Installing Google Chrome (dnf will pull dependencies)..."
  sudo dnf install -y "$tmp_rpm"

  rm -f "$tmp_rpm"
  trap - EXIT

  if chrome_installed; then
    CHROME_CMD="$(command -v google-chrome || command -v google-chrome-stable)"
    ok "Google Chrome installed: $CHROME_CMD"
  else
    err "Google Chrome installation failed."
    exit 1
  fi
}


# =========================================================
# VIRTUAL ENVIRONMENT
# =========================================================

ensure_venv() {
  if [[ -x "$VENV_PY" ]] && \
     "$VENV_PY" -m pip --version >/dev/null 2>&1; then
    ok "Virtual environment exists and pip is available."
    return
  fi

  if [[ -d ".venv" ]]; then
    warn "Existing virtual environment is incomplete or missing pip."
    info "Recreating .venv..."
    rm -rf .venv
  else
    info "Creating virtual environment..."
  fi

  local pybin="${WTR_PYTHON_BIN:-python3}"
  "$pybin" -m venv .venv

  if [[ ! -x "$VENV_PY" ]]; then
    err "Failed to create .venv with $pybin."
    exit 1
  fi

  if ! "$VENV_PY" -m pip --version >/dev/null 2>&1; then
    warn "pip missing inside venv — bootstrapping with ensurepip..."
    "$VENV_PY" -m ensurepip --upgrade 2>/dev/null || true
  fi

  if ! "$VENV_PY" -m pip --version >/dev/null 2>&1; then
    err "Could not install pip into the virtual environment."
    exit 1
  fi

  ok "Virtual environment created with pip."
}


# =========================================================
# PYTHON DEPENDENCIES
# =========================================================

ensure_deps() {
  info "Installing / updating Python packages..."
  info "This may take a few minutes (seleniumbase pulls ChromeDriver tooling)."

  "$VENV_PY" -m pip install --upgrade pip setuptools wheel
  "$VENV_PY" -m pip install -r "$REQ_FILE"

  ok "Python dependencies installed."
}


# =========================================================
# ENVIRONMENT FILE
# =========================================================

ensure_env() {
  if [[ -f "$ENV_FILE" ]] && \
     grep -qE '^BOT_TOKEN=.+' "$ENV_FILE" && \
     ! grep -q 'your_token_here' "$ENV_FILE"; then
    ok ".env already configured."
    return
  fi

  require_token_noninteractive

  if [[ -f "$ENV_FILE" ]]; then
    warn ".env exists but BOT_TOKEN looks empty / placeholder."
    warn "Reconfiguring .env..."
  fi

  local token="${BOT_TOKEN:-}"
  local allowed="${ALLOWED_USER_IDS:-}"
  local admin_ids="${ADMIN_USER_IDS:-}"
  local admin_chat="${ADMIN_CHAT_ID:-}"
  local cap="${CHAPTER_CAP:-0}"
  local daily="${DAILY_TASK_LIMIT:-0}"
  local tmin="${CHAPTER_THROTTLE_MIN:-10}"
  local tmax="${CHAPTER_THROTTLE_MAX:-18}"
  local headless="${HEADLESS:-1}"

  if [[ "$IS_INTERACTIVE" -eq 1 ]]; then
    echo
    echo "=========================================="
    echo "          Configure your bot"
    echo "=========================================="
    echo
    echo "Create a bot with @BotFather in Telegram,"
    echo "then paste the bot token below."
    echo

    while true; do
      prompt_or_env token "BOT_TOKEN (required)" ""
      if [[ -n "$token" ]]; then
        break
      fi
      warn "Token is required."
    done

    echo
    echo "ALLOWED_USER_IDS — numeric Telegram ID(s)."
    echo "Comma-separated if there are multiple."
    echo "Leave empty to allow anyone who finds the bot."
    prompt_or_env allowed "ALLOWED_USER_IDS (optional)" ""

    echo
    echo "ADMIN_USER_IDS — immune to chapter/daily limits (optional)."
    prompt_or_env admin_ids "ADMIN_USER_IDS (optional)" ""

    echo
    echo "ADMIN_CHAT_ID — receives copies of finished books (optional)."
    prompt_or_env admin_chat "ADMIN_CHAT_ID (optional)" ""

    echo
    echo "CHAPTER_CAP — maximum chapters per download (0 = unlimited)."
    prompt_or_env cap "CHAPTER_CAP" "0"
    [[ "$cap" =~ ^[0-9]+$ ]] || cap="0"

    echo
    echo "DAILY_TASK_LIMIT — max new tasks per user per day (0 = unlimited)."
    prompt_or_env daily "DAILY_TASK_LIMIT" "0"
    [[ "$daily" =~ ^[0-9]+$ ]] || daily="0"

    echo
    echo "Chapter delay in seconds (defaults 10–18 are safer vs Cloudflare)."
    prompt_or_env tmin "CHAPTER_THROTTLE_MIN" "10"
    prompt_or_env tmax "CHAPTER_THROTTLE_MAX" "18"

    echo
    echo "HEADLESS — 1 = no Chrome window (recommended on a VPS),"
    echo "           0 = visible window (needs display / VNC for Turnstile)."
    prompt_or_env headless "HEADLESS" "1"
  else
    info "Non-interactive mode: writing .env from environment variables / defaults."
    if [[ -z "$token" ]]; then
      err "BOT_TOKEN is empty in non-interactive mode."
      exit 1
    fi
  fi

  cat > "$ENV_FILE" <<EOF
BOT_TOKEN=$token
ALLOWED_USER_IDS=$allowed
ADMIN_USER_IDS=$admin_ids
OUTPUT_GROUPS=
ADMIN_CHAT_ID=$admin_chat
CHAPTER_CAP=$cap
DAILY_TASK_LIMIT=$daily
CHAPTER_THROTTLE_MIN=$tmin
CHAPTER_THROTTLE_MAX=$tmax
PROGRESS_UPDATE_SECONDS=25
CHROME_PROFILE_DIR=data/chrome-profile
HEADLESS=$headless
EOF

  chmod 600 "$ENV_FILE"
  ok ".env written (HEADLESS=$headless)."
}


# =========================================================
# DATA DIRECTORY / MARKER
# =========================================================

ensure_data_dir() {
  mkdir -p data
}


mark_setup_done() {
  ensure_data_dir
  date -Iseconds > "$MARKER"
}


test_setup_done() {
  [[ -f "$MARKER" ]] &&
  [[ -x "$VENV_PY" ]] &&
  [[ -f "$ENV_FILE" ]] &&
  "$VENV_PY" -m pip --version >/dev/null 2>&1
}


# =========================================================
# FIX PERMISSIONS
# =========================================================

fix_project_permissions() {
  if [[ "$EUID" -ne 0 ]]; then
    local CURRENT_USER CURRENT_GROUP
    CURRENT_USER="$(id -un)"
    CURRENT_GROUP="$(id -gn)"

    for item in .venv data "$ENV_FILE"; do
      if [[ -e "$item" ]]; then
        sudo chown -R "$CURRENT_USER:$CURRENT_GROUP" "$item" 2>/dev/null || true
      fi
    done
  fi
}


# =========================================================
# STOP OTHER CHROME PROCESSES
# =========================================================

stop_chrome_if_needed() {
  echo
  warn "Other Chrome processes can lock the profile or confuse chromedriver."
  echo "Worker profile path: data/chrome-profile"
  echo

  local answer="n"
  if [[ "$IS_INTERACTIVE" -eq 1 ]]; then
    read -r -p "Try to close Chrome processes now? (y/N): " answer || true
  else
    info "Non-interactive: skipping Chrome kill prompt."
  fi

  if [[ "${answer:-}" =~ ^[Yy]$ ]]; then
    pkill -x chrome 2>/dev/null || true
    pkill -x google-chrome 2>/dev/null || true
    pkill -x google-chrome-stable 2>/dev/null || true
    pkill -f "chrome-profile" 2>/dev/null || true
    sleep 2
    ok "Chrome processes signaled to close."
  fi
}


# =========================================================
# MAIN
# =========================================================

mkdir -p data
detect_interactive

echo
echo "=========================================="
echo "        WTR-Lab Local Worker (dnf)"
echo "=========================================="
echo

if [[ "$IS_INTERACTIVE" -eq 1 ]]; then
  ok "Interactive terminal detected."
else
  warn "No TTY — running non-interactively."
  warn "BOT_TOKEN must already be set in the environment or in .env."
fi

detect_os
ensure_sudo
ensure_basic_tools
ensure_project
ensure_data_dir

# ---------------------------------------------------------
# FIRST-TIME / INCOMPLETE SETUP
# ---------------------------------------------------------

if ! test_setup_done; then
  info "First-time (or incomplete) setup..."

  ensure_python
  ensure_chrome
  ensure_venv
  ensure_deps
  ensure_env
  mark_setup_done
  fix_project_permissions
  ok "Setup complete."
else
  ok "Setup already done — starting worker."

  ensure_chrome

  if ! "$VENV_PY" -m pip --version >/dev/null 2>&1; then
    warn "Virtual environment is no longer usable."
    ensure_python
    ensure_venv
    ensure_deps
  fi

  fix_project_permissions
fi

# ---------------------------------------------------------
# FINAL CHECKS
# ---------------------------------------------------------

ensure_chrome

if [[ ! -x "$VENV_PY" ]]; then
  err "Virtual environment Python not found."
  ensure_python
  ensure_venv
  ensure_deps
fi

if [[ ! -f "$ENV_FILE" ]]; then
  warn ".env file is missing."
  ensure_env
fi

# ---------------------------------------------------------
# START APP
# ---------------------------------------------------------

stop_chrome_if_needed

echo
echo "=========================================="
echo "          Starting WTR-Lab Worker"
echo "=========================================="
echo

info "Starting app.py..."
echo
if grep -qE '^HEADLESS=0' "$ENV_FILE" 2>/dev/null; then
  echo "HEADLESS=0 — a Chrome window will open (needs display/VNC)."
  echo "Log into WTR-Lab there if asked. Leave the mouse alone during Turnstile."
else
  echo "HEADLESS=1 — no Chrome window (good for a VPS)."
  echo "If Cloudflare blocks downloads, use /login in Telegram or set HEADLESS=0 + VNC."
fi
echo
echo "Press Ctrl+C to stop."
echo

exec "$VENV_PY" "$APP_FILE"
