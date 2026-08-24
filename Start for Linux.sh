#!/usr/bin/env bash
# WTR-Lab local worker — setup once, then start app.py
# Usage: chmod +x "Start for Linux.sh" && ./"Start for Linux.sh"

set -euo pipefail
cd "$(dirname "$0")"

VENV_PY="./.venv/bin/python"
ENV_FILE="./.env"
REQ_FILE="./requirements.txt"
APP_FILE="./app.py"
MARKER="./data/.setup_done"
# Optional GitHub zip URL if the folder is incomplete:
GITHUB_ZIP_URL=""

info()  { echo "[*] $*"; }
ok()    { echo "[+] $*"; }
warn()  { echo "[!] $*"; }
err()   { echo "[x] $*" >&2; }

project_ok() { [[ -f "$APP_FILE" && -f "$REQ_FILE" ]]; }

ensure_project() {
  if project_ok; then return; fi
  warn "app.py or requirements.txt missing."
  if [[ -z "$GITHUB_ZIP_URL" ]]; then
    err "No GitHub URL configured. Copy the full project folder and retry."
    exit 1
  fi
  info "Downloading from GitHub..."
  tmpzip="$(mktemp)"
  tmpdir="$(mktemp -d)"
  curl -fsSL "$GITHUB_ZIP_URL" -o "$tmpzip"
  unzip -q "$tmpzip" -d "$tmpdir"
  inner="$(find "$tmpdir" -mindepth 1 -maxdepth 1 -type d | head -1)"
  cp -R "$inner"/* .
  rm -rf "$tmpzip" "$tmpdir"
  project_ok || { err "Download incomplete."; exit 1; }
  ok "Project files ready."
}

chrome_installed() {
  command -v google-chrome >/dev/null 2>&1 \
    || command -v google-chrome-stable >/dev/null 2>&1 \
    || command -v chromium >/dev/null 2>&1 \
    || command -v chromium-browser >/dev/null 2>&1 \
    || [[ -d "/Applications/Google Chrome.app" ]]
}

ensure_chrome() {
  if chrome_installed; then
    ok "Chrome/Chromium found."
    return
  fi
  warn "Chrome not found. Please install Google Chrome, then re-run."
  exit 1
}

ensure_python() {
  if command -v python3 >/dev/null 2>&1; then
    ok "Python: $(python3 --version)"
    return
  fi
  err "Python 3.10+ required."
  exit 1
}

ensure_venv() {
  if [[ -x "$VENV_PY" ]]; then
    ok "Virtual environment exists."
    return
  fi
  info "Creating .venv ..."
  python3 -m venv .venv
  ok "venv created."
}

ensure_deps() {
  info "Installing Python packages..."
  "$VENV_PY" -m pip install --upgrade pip
  "$VENV_PY" -m pip install -r "$REQ_FILE"
  ok "Dependencies installed."
}

ensure_env() {
  if [[ -f "$ENV_FILE" ]] && grep -qE '^BOT_TOKEN=.+' "$ENV_FILE" \
     && ! grep -q 'your_token_here' "$ENV_FILE"; then
    ok ".env already configured."
    return
  fi
  echo
  echo "=== Configure your bot ==="
  echo "Create a bot with @BotFather, then paste the token."
  echo "Press Enter on optional fields to leave them empty."
  echo
  while true; do
    read -r -p "BOT_TOKEN (required): " token
    token="$(echo "$token" | xargs)"
    [[ -n "$token" ]] && break
    warn "Token is required."
  done
  echo
  echo "ALLOWED_USER_IDS — numeric Telegram id(s). Enter = open to anyone who finds the bot."
  read -r -p "ALLOWED_USER_IDS (optional): " allowed
  echo
  read -r -p "CHAPTER_CAP [0=unlimited]: " cap
  cap="${cap:-0}"
  read -r -p "DAILY_TASK_LIMIT [0]: " daily
  daily="${daily:-0}"
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
  ok ".env written."
}

mkdir -p data
ensure_project

if [[ ! -f "$MARKER" || ! -x "$VENV_PY" || ! -f "$ENV_FILE" ]]; then
  info "First-time setup..."
  ensure_python
  ensure_chrome
  ensure_venv
  ensure_deps
  ensure_env
  date -Iseconds > "$MARKER"
  ok "Setup complete."
else
  ok "Setup already done — starting worker."
fi

ensure_chrome

echo
info "Starting app.py (Ctrl+C to stop)..."
echo "Close other Chrome windows first if you can."
echo
exec "$VENV_PY" "$APP_FILE"
