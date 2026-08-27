#!/usr/bin/env bash
# WTR-Lab local worker — automatic Linux setup + start app.py
#
# Usage:
#   chmod +x "Start for Linux.sh"
#   ./"Start for Linux.sh"
#
# Designed for:
#   Ubuntu / Linux Mint / Debian-based distributions
#
# IMPORTANT:
#   Run this script WITHOUT sudo.
#   It will request sudo only when installing system packages.

set -euo pipefail
cd "$(dirname "$0")"

VENV_PY="./.venv/bin/python"
ENV_FILE="./.env"
REQ_FILE="./requirements.txt"
APP_FILE="./app.py"
MARKER="./data/.setup_done"

# Optional GitHub zip URL if the project folder is incomplete.
# Leave empty to disable automatic project download.
GITHUB_ZIP_URL="https://github.com/patrick-mulinge/WTR-Lab-crawler/archive/refs/heads/main.zip"

info()  { echo "[*] $*"; }
ok()    { echo "[+] $*"; }
warn()  { echo "[!] $*"; }
err()   { echo "[x] $*" >&2; }


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
# OPERATING SYSTEM
# =========================================================

check_os() {
  if [[ ! -f /etc/os-release ]]; then
    err "Cannot determine Linux distribution."
    exit 1
  fi

  # shellcheck disable=SC1091
  source /etc/os-release

  case "${ID:-}" in
    ubuntu|linuxmint|debian|pop|elementary|zorin)
      ok "Supported Linux distribution: ${PRETTY_NAME:-$ID}"
      ;;
    *)
      warn "This script was designed for Ubuntu/Linux Mint/Debian."
      warn "Detected: ${PRETTY_NAME:-unknown}"

      read -r -p "Continue anyway? [y/N]: " answer

      if [[ ! "$answer" =~ ^[Yy]$ ]]; then
        exit 1
      fi
      ;;
  esac
}


# =========================================================
# SUDO
# =========================================================

ensure_sudo() {

  if [[ $EUID -eq 0 ]]; then
    warn "Do not run this script with sudo."
    echo
    echo "Run it normally:"
    echo
    echo '  ./"Start for Linux.sh"'
    echo
    warn "The script will request sudo when required."
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
# APT PACKAGE INSTALLER
# =========================================================

APT_UPDATED=0


apt_install() {

  local packages=("$@")
  local missing=()

  for package in "${packages[@]}"; do

    if dpkg -s "$package" >/dev/null 2>&1; then
      ok "$package already installed."
    else
      missing+=("$package")
    fi

  done

  if [[ ${#missing[@]} -eq 0 ]]; then
    return
  fi

  if [[ $APT_UPDATED -eq 0 ]]; then
    info "Updating package lists..."
    sudo apt-get update
    APT_UPDATED=1
  fi

  info "Installing: ${missing[*]}"

  sudo apt-get install -y "${missing[@]}"
}


# =========================================================
# BASIC TOOLS
# =========================================================

ensure_basic_tools() {

  info "Checking required system tools..."

  apt_install \
    ca-certificates \
    curl \
    wget \
    unzip \
    gnupg \
    software-properties-common

  ok "Basic system tools ready."
}


# =========================================================
# PYTHON
# =========================================================

ensure_python() {

  if command -v python3 >/dev/null 2>&1; then

    PY_VERSION="$(
      python3 -c \
      'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")'
    )"

    PY_MAJOR="${PY_VERSION%%.*}"
    PY_MINOR="${PY_VERSION##*.}"

    if [[ "$PY_MAJOR" -gt 3 ]] || {
      [[ "$PY_MAJOR" -eq 3 ]] &&
      [[ "$PY_MINOR" -ge 10 ]]
    }; then

      ok "Python: $(python3 --version)"

    else

      warn "Python $PY_VERSION found, but Python 3.10+ is required."

      apt_install \
        python3 \
        python3-venv \
        python3-pip \
        python3-dev

    fi

  else

    warn "Python 3 not found."
    info "Installing Python..."

    apt_install \
      python3 \
      python3-venv \
      python3-pip \
      python3-dev

    ok "Python installed: $(python3 --version)"

  fi


  # Ensure venv support exists.

  if ! python3 -m venv --help >/dev/null 2>&1; then

    info "Installing Python virtual-environment support..."

    apt_install \
      python3-venv

  fi


  # Ensure pip is available globally.

  if ! python3 -m pip --version >/dev/null 2>&1; then

    info "Installing Python pip..."

    apt_install \
      python3-pip

  fi


  # Ensure development headers are available.
  # Some Python packages require them during installation.

  if ! dpkg -s python3-dev >/dev/null 2>&1; then

    info "Installing Python development packages..."

    apt_install \
      python3-dev

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


ensure_chrome() {

  if chrome_installed; then

    CHROME_CMD="$(
      command -v google-chrome ||
      command -v google-chrome-stable
    )"

    ok "Google Chrome found: $CHROME_CMD"
    return

  fi


  warn "Google Chrome not found."
  info "Installing Google Chrome..."


  TMP_DEB="$(mktemp --suffix=.deb)"


  cleanup_chrome() {
    rm -f "$TMP_DEB"
  }


  trap cleanup_chrome EXIT


  info "Downloading Google Chrome..."

  curl -fL \
    "https://dl.google.com/linux/direct/google-chrome-stable_current_amd64.deb" \
    -o "$TMP_DEB"


  info "Installing Google Chrome..."

  sudo apt-get install -y "$TMP_DEB"


  rm -f "$TMP_DEB"

  trap - EXIT


  if chrome_installed; then

    CHROME_CMD="$(
      command -v google-chrome ||
      command -v google-chrome-stable
    )"

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

  # First check whether the venv is actually usable.
  # Merely having .venv/bin/python is not enough.
  # This specifically fixes:
  #
  #   No module named pip
  #

  if [[ -x "$VENV_PY" ]] &&
     "$VENV_PY" -m pip --version >/dev/null 2>&1; then

    ok "Virtual environment exists and pip is available."
    return

  fi


  # If .venv exists but is broken/incomplete, remove it.

  if [[ -d ".venv" ]]; then

    warn "Existing virtual environment is incomplete or missing pip."
    info "Recreating .venv..."

    rm -rf .venv

  else

    info "Creating virtual environment..."

  fi


  # Make sure the venv package exists.

  if ! python3 -m venv --help >/dev/null 2>&1; then

    info "Installing python3-venv..."

    apt_install python3-venv

  fi


  python3 -m venv .venv


  if [[ ! -x "$VENV_PY" ]]; then

    err "Failed to create .venv."
    exit 1

  fi


  # Verify pip.

  if ! "$VENV_PY" -m pip --version >/dev/null 2>&1; then

    warn "pip was not included in the new virtual environment."

    info "Installing pip support..."

    apt_install \
      python3-pip \
      python3-venv

    # Recreate once more after installing the packages.

    rm -rf .venv

    python3 -m venv .venv

  fi


  # Final verification.

  if ! "$VENV_PY" -m pip --version >/dev/null 2>&1; then

    err "Could not install pip into the virtual environment."
    err "Python virtual environment setup failed."
    exit 1

  fi


  ok "Virtual environment created with pip."
}


# =========================================================
# PYTHON DEPENDENCIES
# =========================================================

ensure_deps() {

  info "Installing / updating Python packages..."
  info "This may take a few minutes."

  "$VENV_PY" -m pip install --upgrade pip

  "$VENV_PY" -m pip install -r "$REQ_FILE"

  ok "Python dependencies installed."
}


# =========================================================
# ENVIRONMENT FILE
# =========================================================

ensure_env() {

  if [[ -f "$ENV_FILE" ]] &&
     grep -qE '^BOT_TOKEN=.+' "$ENV_FILE" &&
     ! grep -q 'your_token_here' "$ENV_FILE"; then

    ok ".env already configured."
    return

  fi


  if [[ -f "$ENV_FILE" ]]; then
    warn ".env exists but BOT_TOKEN looks empty."
    warn "Reconfiguring .env..."
  fi


  echo
  echo "=========================================="
  echo "          Configure your bot"
  echo "=========================================="
  echo

  echo "Create a bot with @BotFather in Telegram,"
  echo "then paste the bot token below."
  echo


  while true; do

    read -r -p "BOT_TOKEN (required): " token

    token="$(echo "$token" | xargs)"

    if [[ -n "$token" ]]; then
      break
    fi

    warn "Token is required."

  done


  echo
  echo "ALLOWED_USER_IDS — numeric Telegram ID(s)."
  echo "Comma-separated if there are multiple."
  echo "Leave empty to allow anyone who finds the bot."

  read -r -p "ALLOWED_USER_IDS (optional): " allowed


  echo
  echo "CHAPTER_CAP — maximum chapters per download."
  echo "0 = unlimited."

  read -r -p "CHAPTER_CAP [0]: " cap
  cap="${cap:-0}"

  if [[ ! "$cap" =~ ^[0-9]+$ ]]; then
    cap="0"
  fi


  echo
  echo "DAILY_TASK_LIMIT — maximum tasks per user per day."
  echo "0 = unlimited."

  read -r -p "DAILY_TASK_LIMIT [0]: " daily
  daily="${daily:-0}"

  if [[ ! "$daily" =~ ^[0-9]+$ ]]; then
    daily="0"
  fi


  echo
  echo "Chapter delay in seconds."
  echo "Defaults of 10-18 are safer against Cloudflare."

  read -r -p "CHAPTER_THROTTLE_MIN [10]: " tmin
  tmin="${tmin:-10}"

  read -r -p "CHAPTER_THROTTLE_MAX [18]: " tmax
  tmax="${tmax:-18}"


  cat > "$ENV_FILE" <<EOF
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
EOF


  chmod 600 "$ENV_FILE"

  ok ".env written."
}


# =========================================================
# DATA DIRECTORY
# =========================================================

ensure_data_dir() {

  if [[ ! -d "data" ]]; then

    info "Creating data directory..."

    mkdir -p data

  fi
}


# =========================================================
# SETUP MARKER
# =========================================================

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

    CURRENT_USER="$(id -un)"
    CURRENT_GROUP="$(id -gn)"

    # This protects against a previous accidental
    # "sudo ./Start for Linux.sh".

    for item in .venv data "$ENV_FILE"; do

      if [[ -e "$item" ]]; then

        sudo chown -R \
          "$CURRENT_USER:$CURRENT_GROUP" \
          "$item" \
          2>/dev/null || true

      fi

    done

  fi
}


# =========================================================
# STOP OTHER CHROME PROCESSES
# =========================================================

stop_chrome_if_needed() {

  echo
  warn "Close other Chrome windows before the worker starts."
  echo "The worker uses its own profile:"
  echo "  data/chrome-profile"
  echo

  read -r -p "Try to close all Chrome processes now? (y/N): " answer

  if [[ "$answer" =~ ^[Yy]$ ]]; then

    pkill -x chrome 2>/dev/null || true
    pkill -x google-chrome 2>/dev/null || true
    pkill -x google-chrome-stable 2>/dev/null || true

    sleep 2

    ok "Chrome processes signaled to close."

  fi
}


# =========================================================
# MAIN
# =========================================================

mkdir -p data


echo
echo "=========================================="
echo "        WTR-Lab Local Worker"
echo "=========================================="
echo


check_os

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

  # Still make sure Chrome exists.

  ensure_chrome

  # Verify the virtual environment.

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
echo "Log into WTR-Lab in the Chrome window if asked."
echo "Leave the mouse alone if a Turnstile challenge is being auto-solved."
echo
echo "Press Ctrl+C to stop."
echo


exec "$VENV_PY" "$APP_FILE"
