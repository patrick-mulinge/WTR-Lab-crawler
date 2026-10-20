#!/usr/bin/env bash
# WTR-Lab worker / coordinator — Oracle Linux / RHEL / Rocky / Alma (dnf)
#
# Supports: x86_64 (AMD/Intel micros) and aarch64 (Ampere)
#
# Usage:
#   chmod +x "Start for Linux.sh"
#   ./"Start for Linux.sh"
#
# IMPORTANT: run WITHOUT sudo. Script uses sudo only for packages.
#
# Order:
#   1) Ensure project files
#   2) Write / update .env first (BOT_TOKEN, SERVER_ID, …)
#   3) Install system deps + Chrome/Chromium + venv + Python deps
#   4) Install cloudflared, noVNC, websockify if missing
#   5) Start Xvfb, openbox, x0vncserver, websockify if not running
#   6) Start app.py
#
# SERVER_ID:
#   s1  = coordinator (convention)
#   s2+ = workers

set -euo pipefail
cd "$(dirname "$0")"

VENV_PY="./.venv/bin/python"
ENV_FILE="./.env"
REQ_FILE="./requirements.txt"
APP_FILE="./app.py"
MARKER="./data/.setup_done"
NOVNC_DIR="${HOME}/noVNC"
CLOUDFLARED_BIN_PATH="${HOME}/bin/cloudflared"
DISPLAY_NUM=99
VNC_PORT=5901
NOVNC_PORT=6080

# Multi-worker branch (override if needed)
GITHUB_ZIP_URL="${GITHUB_ZIP_URL:-https://github.com/patrick-mulinge/WTR-Lab-crawler/archive/refs/heads/linuxmultiworker.zip}"

IS_INTERACTIVE=0
ARCH="$(uname -m)"

info()  { echo "[*] $*"; }
ok()    { echo "[+] $*"; }
warn()  { echo "[!] $*"; }
err()   { echo "[x] $*" >&2; }


# =========================================================
# INTERACTIVE / TTY
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
  err "Non-interactive: set BOT_TOKEN in the environment or in .env"
  exit 1
}


# =========================================================
# PROJECT
# =========================================================

project_ok() {
  [[ -f "$APP_FILE" && -f "$REQ_FILE" ]]
}


ensure_project() {
  if project_ok; then
    ok "Project files found."
    return
  fi

  warn "app.py or requirements.txt missing."
  if [[ -z "$GITHUB_ZIP_URL" ]]; then
    err "No GitHub URL. Copy the project folder here and re-run."
    exit 1
  fi

  info "Downloading project from GitHub..."
  tmpzip="$(mktemp)"
  tmpdir="$(mktemp -d)"
  cleanup_project() { rm -rf "$tmpzip" "$tmpdir"; }
  trap cleanup_project EXIT

  curl -fsSL "$GITHUB_ZIP_URL" -o "$tmpzip"
  unzip -q "$tmpzip" -d "$tmpdir"
  inner="$(find "$tmpdir" -mindepth 1 -maxdepth 1 -type d | head -1)"
  if [[ -z "$inner" ]]; then
    err "Empty archive."
    exit 1
  fi
  cp -R "$inner"/* .
  if ! project_ok; then
    err "Download incomplete."
    exit 1
  fi
  trap - EXIT
  cleanup_project
  ok "Project files downloaded."
}


# =========================================================
# OS / SUDO / PACKAGES
# =========================================================

detect_os() {
  if [[ ! -f /etc/os-release ]]; then
    err "Cannot determine OS."
    exit 1
  fi
  # shellcheck disable=SC1091
  source /etc/os-release
  if ! command -v dnf >/dev/null 2>&1; then
    err "This script requires dnf (Oracle Linux / RHEL / Rocky / Alma)."
    exit 1
  fi
  ok "OS: ${PRETTY_NAME:-$ID}  arch=$ARCH  (dnf)"
}


ensure_sudo() {
  if [[ $EUID -eq 0 ]]; then
    warn "Do not run this script as root."
    echo '  ./"Start for Linux.sh"'
    exit 1
  fi
  if ! command -v sudo >/dev/null 2>&1; then
    err "sudo is required."
    exit 1
  fi
  info "Checking sudo..."
  sudo -v
  ok "sudo OK."
}


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


ensure_basic_tools() {
  info "Basic tools..."
  pkg_install ca-certificates curl wget unzip gnupg2 tar
  ok "Basic tools ready."
}


# =========================================================
# .ENV FIRST (before heavy installs)
# =========================================================

env_has_token() {
  [[ -f "$ENV_FILE" ]] \
    && grep -qE '^BOT_TOKEN=.+' "$ENV_FILE" \
    && ! grep -q 'your_token_here' "$ENV_FILE"
}


env_has_server_id() {
  [[ -f "$ENV_FILE" ]] && grep -qE '^SERVER_ID=.+' "$ENV_FILE"
}


# Merge/update a KEY=value in .env without wiping unknown keys
dotenv_set() {
  local key="$1"
  local value="$2"
  local tmp
  tmp="$(mktemp)"
  touch "$ENV_FILE"
  if grep -qE "^${key}=" "$ENV_FILE" 2>/dev/null; then
    # shellcheck disable=SC2002
    cat "$ENV_FILE" | sed -E "s|^${key}=.*|${key}=${value}|" >"$tmp"
    mv "$tmp" "$ENV_FILE"
  else
    echo "${key}=${value}" >>"$ENV_FILE"
    rm -f "$tmp"
  fi
}


ensure_env_first() {
  info "Configuring .env (before installs)..."
  require_token_noninteractive
  mkdir -p data

  local token="${BOT_TOKEN:-}"
  local allowed="${ALLOWED_USER_IDS:-}"
  local admin_ids="${ADMIN_USER_IDS:-}"
  local admin_chat="${ADMIN_CHAT_ID:-}"
  local cap="${CHAPTER_CAP:-0}"
  local daily="${DAILY_TASK_LIMIT:-0}"
  local tmin="${CHAPTER_THROTTLE_MIN:-18}"
  local tmax="${CHAPTER_THROTTLE_MAX:-24}"
  local headless="${HEADLESS:-0}"
  local server_id="${SERVER_ID:-}"
  local db_url="${DATABASE_URL:-}"
  local role_hint=""

  if env_has_token; then
    token="$(grep -E '^BOT_TOKEN=' "$ENV_FILE" | head -1 | cut -d= -f2-)"
  fi
  if env_has_server_id; then
    server_id="$(grep -E '^SERVER_ID=' "$ENV_FILE" | head -1 | cut -d= -f2-)"
  fi

  if [[ "$IS_INTERACTIVE" -eq 1 ]]; then
    echo
    echo "=========================================="
    echo "          Configure .env first"
    echo "=========================================="
    echo

    if [[ -z "$token" ]] || ! env_has_token; then
      while true; do
        prompt_or_env token "BOT_TOKEN (required)" ""
        [[ -n "$token" ]] && break
        warn "Token is required."
      done
    else
      ok "BOT_TOKEN already set in .env"
    fi

    echo
    echo "SERVER_ID — unique id for this machine."
    echo "  s1     = coordinator (Telegram poll + shared DB/NFS host by convention)"
    echo "  s2–s10 = workers (Chrome crawl)"
    if [[ -z "$server_id" ]]; then
      prompt_or_env server_id "SERVER_ID" "s1"
    else
      ok "SERVER_ID already set: $server_id"
      local ans=""
      read -r -p "Keep SERVER_ID=$server_id? [Y/n]: " ans || true
      ans="$(echo "${ans:-Y}" | tr '[:upper:]' '[:lower:]')"
      if [[ "$ans" == "n" || "$ans" == "no" ]]; then
        server_id=""
        prompt_or_env server_id "SERVER_ID" "s1"
      fi
    fi
    server_id="$(echo "$server_id" | tr '[:upper:]' '[:lower:]' | xargs)"
    [[ -n "$server_id" ]] || server_id="s1"

    if [[ "$server_id" == "s1" ]]; then
      role_hint="coordinator"
    else
      role_hint="worker"
    fi
    ok "Role convention: SERVER_ID=$server_id → $role_hint"

    if ! env_has_token || [[ ! -f "$ENV_FILE" ]]; then
      prompt_or_env allowed "ALLOWED_USER_IDS (optional)" ""
      prompt_or_env admin_ids "ADMIN_USER_IDS (optional)" ""
      prompt_or_env admin_chat "ADMIN_CHAT_ID (optional)" ""
      prompt_or_env cap "CHAPTER_CAP" "0"
      [[ "$cap" =~ ^[0-9]+$ ]] || cap="0"
      prompt_or_env daily "DAILY_TASK_LIMIT" "0"
      [[ "$daily" =~ ^[0-9]+$ ]] || daily="0"
      prompt_or_env tmin "CHAPTER_THROTTLE_MIN" "18"
      prompt_or_env tmax "CHAPTER_THROTTLE_MAX" "24"
      echo
      echo "HEADLESS=0 recommended for Turnstile (needs Xvfb + VNC)."
      prompt_or_env headless "HEADLESS" "0"
    fi

    echo
    echo "DATABASE_URL — leave empty for local SQLite (single-node)."
    echo "Multi-worker: postgresql://user:pass@COORD_PRIVATE_IP:5432/wtr"
    prompt_or_env db_url "DATABASE_URL (optional)" "${db_url}"
  else
    info "Non-interactive: using env / existing .env"
    [[ -n "$token" ]] || token="$(grep -E '^BOT_TOKEN=' "$ENV_FILE" 2>/dev/null | head -1 | cut -d= -f2- || true)"
    [[ -n "$server_id" ]] || server_id="$(grep -E '^SERVER_ID=' "$ENV_FILE" 2>/dev/null | head -1 | cut -d= -f2- || true)"
    [[ -n "$server_id" ]] || server_id="s1"
    if [[ -z "$token" ]]; then
      err "BOT_TOKEN required in non-interactive mode."
      exit 1
    fi
  fi

  # Write core keys (create file if missing; update keys if present)
  if [[ ! -f "$ENV_FILE" ]]; then
    cat > "$ENV_FILE" <<EOF
BOT_TOKEN=$token
SERVER_ID=$server_id
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
DATABASE_URL=$db_url
CLOUDFLARED_BIN=$CLOUDFLARED_BIN_PATH
NOVNC_WEB_DIR=$NOVNC_DIR
VNC_PORT=$VNC_PORT
NOVNC_PORT=$NOVNC_PORT
EOF
  else
    dotenv_set BOT_TOKEN "$token"
    dotenv_set SERVER_ID "$server_id"
    dotenv_set HEADLESS "${headless:-0}"
    dotenv_set CHROME_PROFILE_DIR "data/chrome-profile"
    dotenv_set CLOUDFLARED_BIN "$CLOUDFLARED_BIN_PATH"
    dotenv_set NOVNC_WEB_DIR "$NOVNC_DIR"
    dotenv_set VNC_PORT "$VNC_PORT"
    dotenv_set NOVNC_PORT "$NOVNC_PORT"
    if [[ -n "$db_url" ]]; then
      dotenv_set DATABASE_URL "$db_url"
    fi
    if [[ -n "${allowed:-}" ]]; then dotenv_set ALLOWED_USER_IDS "$allowed"; fi
    if [[ -n "${admin_ids:-}" ]]; then dotenv_set ADMIN_USER_IDS "$admin_ids"; fi
    if [[ -n "${admin_chat:-}" ]]; then dotenv_set ADMIN_CHAT_ID "$admin_chat"; fi
  fi

  chmod 600 "$ENV_FILE"
  ok ".env ready (SERVER_ID=$(grep -E '^SERVER_ID=' "$ENV_FILE" | cut -d= -f2-))."
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
    warn "Python 3.10+ missing — installing..."
    pkg_install python3 python3-pip python3-devel || true
    if ! python_version_ok; then
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
    pkg_install gcc gcc-c++ make libffi-devel openssl-devel 2>/dev/null || true
  fi

  if [[ -z "${WTR_PYTHON_BIN:-}" ]]; then
    WTR_PYTHON_BIN="python3"
  fi
  if ! command -v "$WTR_PYTHON_BIN" >/dev/null 2>&1; then
    err "python3 not available."
    exit 1
  fi
  ok "Using $WTR_PYTHON_BIN for venv."

  if ! "$WTR_PYTHON_BIN" -m venv --help >/dev/null 2>&1; then
    pkg_install python3 || true
  fi
  if ! "$WTR_PYTHON_BIN" -m venv --help >/dev/null 2>&1; then
    err "venv module missing."
    exit 1
  fi
}


ensure_venv() {
  if [[ -x "$VENV_PY" ]] && "$VENV_PY" -m pip --version >/dev/null 2>&1; then
    ok "Virtualenv OK."
    return
  fi
  if [[ -d ".venv" ]]; then
    warn "Recreating broken .venv..."
    rm -rf .venv
  fi
  info "Creating .venv..."
  local pybin="${WTR_PYTHON_BIN:-python3}"
  "$pybin" -m venv .venv
  "$VENV_PY" -m ensurepip --upgrade 2>/dev/null || true
  if ! "$VENV_PY" -m pip --version >/dev/null 2>&1; then
    err "pip missing in venv."
    exit 1
  fi
  ok "Virtualenv created."
}


ensure_deps() {
  info "pip install -r requirements.txt (may take a few minutes)..."
  "$VENV_PY" -m pip install --upgrade pip setuptools wheel
  "$VENV_PY" -m pip install -r "$REQ_FILE"
  # websockify for noVNC bridge (used by remote solve links)
  "$VENV_PY" -m pip install 'websockify>=0.11' || true
  ok "Python deps installed."
}


# =========================================================
# GOOGLE CHROME ONLY (x86_64 + aarch64 — no Chromium)
# Bot uses SeleniumBase UC mode (uc=True); needs Google Chrome.
# =========================================================

chrome_installed() {
  command -v google-chrome >/dev/null 2>&1 \
    || command -v google-chrome-stable >/dev/null 2>&1
}


ensure_chrome() {
  if chrome_installed; then
    CHROME_CMD="$(command -v google-chrome || command -v google-chrome-stable)"
    ok "Google Chrome found: $CHROME_CMD"
    return
  fi

  local arch
  arch="$(uname -m)"
  local tmp_rpm
  tmp_rpm="$(mktemp --suffix=.rpm)"

  case "$arch" in
    x86_64|amd64)
      warn "Installing Google Chrome (x86_64)..."
      if curl -fL \
          "https://dl.google.com/linux/direct/google-chrome-stable_current_x86_64.rpm" \
          -o "$tmp_rpm"; then
        sudo dnf install -y "$tmp_rpm" || true
      else
        err "Failed to download Chrome x86_64 RPM."
      fi
      ;;
    aarch64|arm64)
      warn "aarch64 — Google Chrome ARM RPM only (Chromium is not installed)..."
      if curl -fL \
          "https://dl.google.com/linux/direct/google-chrome-stable_current_aarch64.rpm" \
          -o "$tmp_rpm" 2>/dev/null; then
        sudo dnf install -y "$tmp_rpm" || true
      elif curl -fL \
          "https://dl.google.com/linux/direct/google-chrome-stable_current_arm64.rpm" \
          -o "$tmp_rpm" 2>/dev/null; then
        sudo dnf install -y "$tmp_rpm" || true
      else
        err "Chrome aarch64/arm64 RPM not available from Google."
        err "This bot requires Google Chrome (SeleniumBase uc=True)."
        err "Use an x86_64 instance for Chrome workers, or install Chrome ARM manually."
        rm -f "$tmp_rpm"
        exit 1
      fi
      ;;
    *)
      err "Unsupported arch: $arch"
      rm -f "$tmp_rpm"
      exit 1
      ;;
  esac

  rm -f "$tmp_rpm"

  if chrome_installed; then
    CHROME_CMD="$(command -v google-chrome || command -v google-chrome-stable)"
    ok "Google Chrome installed: $CHROME_CMD"
  else
    err "Google Chrome is not installed."
    err "  x86_64: https://dl.google.com/linux/direct/google-chrome-stable_current_x86_64.rpm"
    err "  aarch64: https://dl.google.com/linux/direct/google-chrome-stable_current_aarch64.rpm"
    exit 1
  fi
}


# On aarch64, SeleniumBase may try to download an x86_64 driver and fail.
# Prefer a native chromedriver on PATH so it is used instead when possible.
# Note: UC mode (uc=True) still ships its own uc_driver; this helps non-UC
# fallbacks and some SeleniumBase lookups. Does not install the Chromium browser.
ensure_arm_chromedriver() {
  case "$(uname -m)" in
    aarch64|arm64) ;;
    *) return 0 ;;
  esac

  if command -v chromedriver >/dev/null 2>&1; then
    ok "chromedriver on PATH: $(command -v chromedriver)"
    return
  fi

  info "aarch64: installing native chromedriver (chromium-chromedriver package)..."
  sudo dnf install -y epel-release 2>/dev/null || true
  if sudo dnf install -y chromium-chromedriver 2>/dev/null; then
    ok "chromium-chromedriver package installed."
  else
    warn "chromium-chromedriver package not available via dnf."
  fi

  # Common install locations on EL/Oracle
  local candidates=(
    /usr/bin/chromedriver
    /usr/lib64/chromium-browser/chromedriver
    /usr/lib/chromium-browser/chromedriver
    /usr/lib64/chromium/chromedriver
  )
  local src=""
  for c in "${candidates[@]}"; do
    if [[ -x "$c" ]]; then
      src="$c"
      break
    fi
  done

  if [[ -n "$src" ]]; then
    if [[ ! -e /usr/local/bin/chromedriver ]]; then
      sudo ln -sf "$src" /usr/local/bin/chromedriver
      ok "Symlinked $src → /usr/local/bin/chromedriver"
    fi
  fi

  if command -v chromedriver >/dev/null 2>&1; then
    ok "chromedriver ready: $(command -v chromedriver) ($(chromedriver --version 2>/dev/null | head -1 || echo '?'))"
  else
    warn "No aarch64 chromedriver on PATH yet — UC driver download may still fail on ARM."
  fi
}


# =========================================================
# DISPLAY / VNC / noVNC / cloudflared (match old server stack)
# =========================================================

ensure_display_packages() {
  info "Display / VNC packages..."
  # Xvfb + minimal WM (openbox) + TigerVNC server (x0vncserver)
  pkg_install xorg-x11-server-Xvfb openbox tigervnc-server \
    xorg-x11-xauth mesa-libGL 2>/dev/null || \
  pkg_install xorg-x11-server-Xvfb openbox tigervnc-server || true
  ok "Display packages checked."
}


ensure_novnc() {
  if [[ -f "$NOVNC_DIR/vnc.html" ]]; then
    ok "noVNC present: $NOVNC_DIR"
    return
  fi
  info "Installing noVNC into $NOVNC_DIR ..."
  mkdir -p "$(dirname "$NOVNC_DIR")"
  local tmp
  tmp="$(mktemp -d)"
  if curl -fsSL \
      "https://github.com/novnc/noVNC/archive/refs/tags/v1.5.0.tar.gz" \
      -o "$tmp/novnc.tgz"; then
    tar -xzf "$tmp/novnc.tgz" -C "$tmp"
    rm -rf "$NOVNC_DIR"
    mv "$tmp"/noVNC-* "$NOVNC_DIR"
    ok "noVNC installed."
  else
    warn "Could not download noVNC (remote solve links need it)."
  fi
  rm -rf "$tmp"
}


ensure_cloudflared() {
  if command -v cloudflared >/dev/null 2>&1; then
    ok "cloudflared in PATH: $(command -v cloudflared)"
    dotenv_set CLOUDFLARED_BIN "$(command -v cloudflared)"
    return
  fi
  if [[ -x "$CLOUDFLARED_BIN_PATH" ]]; then
    ok "cloudflared: $CLOUDFLARED_BIN_PATH"
    dotenv_set CLOUDFLARED_BIN "$CLOUDFLARED_BIN_PATH"
    return
  fi

  info "Installing cloudflared..."
  mkdir -p "$(dirname "$CLOUDFLARED_BIN_PATH")"
  local url=""
  case "$(uname -m)" in
    x86_64|amd64)
      url="https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64"
      ;;
    aarch64|arm64)
      url="https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-arm64"
      ;;
    *)
      warn "No cloudflared binary mapping for $(uname -m)"
      return
      ;;
  esac
  if curl -fL "$url" -o "$CLOUDFLARED_BIN_PATH"; then
    chmod +x "$CLOUDFLARED_BIN_PATH"
    ok "cloudflared installed: $CLOUDFLARED_BIN_PATH"
    dotenv_set CLOUDFLARED_BIN "$CLOUDFLARED_BIN_PATH"
  else
    warn "cloudflared download failed (temp solve tunnels need it)."
  fi
}


start_xvfb_if_needed() {
  if pgrep -f "Xvfb :${DISPLAY_NUM}" >/dev/null 2>&1; then
    ok "Xvfb :${DISPLAY_NUM} already running."
  else
    info "Starting Xvfb :${DISPLAY_NUM}..."
    nohup Xvfb ":${DISPLAY_NUM}" -screen 0 1280x720x24 -ac +extension GLX +render -noreset \
      >>"${HOME}/xvfb.log" 2>&1 &
    sleep 1
    ok "Xvfb started."
  fi
  export DISPLAY=":${DISPLAY_NUM}"

  if pgrep -x openbox >/dev/null 2>&1; then
    ok "openbox already running."
  else
    info "Starting openbox..."
    nohup openbox >>"${HOME}/openbox.log" 2>&1 &
    sleep 0.5
  fi
}


start_vnc_if_needed() {
  if pgrep -x x0vncserver >/dev/null 2>&1 || ss -ltn 2>/dev/null | grep -q ":${VNC_PORT}"; then
    ok "x0vncserver / port ${VNC_PORT} already up."
  else
    info "Starting x0vncserver on ${VNC_PORT}..."
    # Prefer system binary (cron on old server used /usr/bin/x0vncserver)
    local vnc_bin
    vnc_bin="$(command -v x0vncserver || true)"
    if [[ -z "$vnc_bin" ]]; then
      warn "x0vncserver not found — install tigervnc-server"
      return
    fi
    nohup "$vnc_bin" \
      -display ":${DISPLAY_NUM}" \
      -rfbport "$VNC_PORT" \
      -localhost yes \
      -SecurityTypes None \
      >>"${HOME}/x0vncserver.log" 2>&1 &
    sleep 1
    ok "x0vncserver started."
  fi
}


start_websockify_if_needed() {
  if ss -ltn 2>/dev/null | grep -q ":${NOVNC_PORT}"; then
    ok "websockify / port ${NOVNC_PORT} already up."
    return
  fi
  if [[ ! -f "$NOVNC_DIR/vnc.html" ]]; then
    warn "noVNC missing — skip websockify"
    return
  fi
  info "Starting websockify on ${NOVNC_PORT} → VNC ${VNC_PORT}..."
  nohup "$VENV_PY" -u -m websockify \
    --web "$NOVNC_DIR" \
    "127.0.0.1:${NOVNC_PORT}" \
    "127.0.0.1:${VNC_PORT}" \
    >>"${HOME}/websockify.log" 2>&1 &
  sleep 1
  if ss -ltn 2>/dev/null | grep -q ":${NOVNC_PORT}"; then
    ok "websockify started."
  else
    warn "websockify may have failed — see ${HOME}/websockify.log"
  fi
}


ensure_cron_watchdogs() {
  # Same idea as the old server: keep VNC + websockify alive across reboot
  local tmp
  tmp="$(mktemp)"
  crontab -l 2>/dev/null | grep -v 'x0vncserver' | grep -v 'websockify' >"$tmp" || true
  local vnc_bin
  vnc_bin="$(command -v x0vncserver || echo /usr/bin/x0vncserver)"
  cat >>"$tmp" <<EOF
@reboot ${vnc_bin} -display :${DISPLAY_NUM} -rfbport ${VNC_PORT} -localhost yes -SecurityTypes None >> ${HOME}/x0vncserver.log 2>&1
@reboot sleep 2; ${PWD}/.venv/bin/python -u -m websockify --web ${NOVNC_DIR} 127.0.0.1:${NOVNC_PORT} 127.0.0.1:${VNC_PORT} >> ${HOME}/websockify.log 2>&1
@reboot sleep 1; Xvfb :${DISPLAY_NUM} -screen 0 1280x720x24 -ac +extension GLX +render -noreset >> ${HOME}/xvfb.log 2>&1
*/5 * * * * pgrep -x x0vncserver >/dev/null || ${vnc_bin} -display :${DISPLAY_NUM} -rfbport ${VNC_PORT} -localhost yes -SecurityTypes None >> ${HOME}/x0vncserver.log 2>&1
*/5 * * * * ss -ltn | grep -q ':${NOVNC_PORT}' || ${PWD}/.venv/bin/python -u -m websockify --web ${NOVNC_DIR} 127.0.0.1:${NOVNC_PORT} 127.0.0.1:${VNC_PORT} >> ${HOME}/websockify.log 2>&1
EOF
  crontab "$tmp"
  rm -f "$tmp"
  ok "Cron watchdogs for Xvfb/VNC/websockify installed."
}


# =========================================================
# DATA / PERMS / CLEAN CHROME
# =========================================================

ensure_data_dir() {
  mkdir -p data data/chrome-profile data/library
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


fix_project_permissions() {
  local CURRENT_USER CURRENT_GROUP
  CURRENT_USER="$(id -un)"
  CURRENT_GROUP="$(id -gn)"
  for item in .venv data "$ENV_FILE"; do
    if [[ -e "$item" ]]; then
      sudo chown -R "$CURRENT_USER:$CURRENT_GROUP" "$item" 2>/dev/null || true
    fi
  done
}


stop_chrome_if_needed() {
  info "Stopping previous app.py / Chrome / driver..."
  pkill -9 -f 'app\.py|uc_driver|google-chrome|chromedriver' 2>/dev/null || true
  sleep 2
  ok "Old worker/Chrome cleared (if any)."
}


# =========================================================
# MAIN
# =========================================================

mkdir -p data
detect_interactive

echo
echo "=========================================="
echo "   WTR-Lab Start (x86_64 + aarch64)"
echo "=========================================="
echo

if [[ "$IS_INTERACTIVE" -eq 1 ]]; then
  ok "Interactive terminal."
else
  warn "Non-interactive — BOT_TOKEN / .env must already exist."
fi

detect_os
ensure_sudo
ensure_basic_tools
ensure_project
ensure_data_dir

# 1) .env FIRST
ensure_env_first

# 2) Install stack
if ! test_setup_done; then
  info "First-time / incomplete setup..."
  ensure_python
  ensure_display_packages
  ensure_chrome
  ensure_arm_chromedriver
  ensure_venv
  ensure_deps
  ensure_novnc
  ensure_cloudflared
  mark_setup_done
  fix_project_permissions
  ok "Setup complete."
else
  ok "Setup marker present — refreshing critical pieces."
  ensure_python
  ensure_display_packages
  ensure_chrome
  ensure_arm_chromedriver
  if ! "$VENV_PY" -m pip --version >/dev/null 2>&1; then
    ensure_venv
    ensure_deps
  else
    # Ensure websockify present on upgrades
    "$VENV_PY" -m pip install -q 'websockify>=0.11' 2>/dev/null || true
  fi
  ensure_novnc
  ensure_cloudflared
  fix_project_permissions
fi

# 3) Runtime display stack (always try to have these up)
export DISPLAY=":${DISPLAY_NUM}"
start_xvfb_if_needed
start_vnc_if_needed
start_websockify_if_needed
ensure_cron_watchdogs

# 4) Start app
stop_chrome_if_needed

echo
echo "=========================================="
echo "          Starting app.py"
echo "=========================================="
echo

SERVER_ID_VAL="$(grep -E '^SERVER_ID=' "$ENV_FILE" 2>/dev/null | head -1 | cut -d= -f2- || echo '?')"
echo "SERVER_ID=$SERVER_ID_VAL  DISPLAY=$DISPLAY  arch=$ARCH"
if grep -qE '^HEADLESS=0' "$ENV_FILE" 2>/dev/null; then
  echo "HEADLESS=0 — Chrome on Xvfb; solve Turnstile via VNC/noVNC then /solved"
else
  echo "HEADLESS=1 — no window; set HEADLESS=0 for manual Turnstile"
fi
echo "VNC 127.0.0.1:${VNC_PORT}  noVNC 127.0.0.1:${NOVNC_PORT}"
echo

LOG_FILE="${HOME}/bot.log"
info "nohup app.py → $LOG_FILE"
nohup "$VENV_PY" "$APP_FILE" >"$LOG_FILE" 2>&1 &
WORKER_PID=$!
sleep 1
if kill -0 "$WORKER_PID" 2>/dev/null; then
  ok "Worker started (PID $WORKER_PID)."
  echo "  tail -f $LOG_FILE"
  echo "Stop: pkill -9 -f 'app.py|uc_driver|google-chrome|chromedriver'"
else
  err "app.py failed to stay up — check $LOG_FILE"
  exit 1
fi
