#!/usr/bin/env bash
# WTR-Lab Start — Oracle Linux / RHEL / Rocky / Alma (dnf)
# Supports x86_64 and aarch64.
#
# Usage:
#   chmod +x "Start for Linux.sh"
#   ./"Start for Linux.sh"
#
# Run WITHOUT sudo (script asks for sudo only for packages).
#
# One-time .env setup: BOT_TOKENn, SERVER_IDn (s4=coord), DB_PASSWORD, limits, etc.
# Later changes: edit .env or use the bot /set command — this script will
# NOT re-prompt for values already set in .env.
#
# After data/.setup_done exists: skip installs; only ensure services + start app.

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
GITHUB_ZIP_URL="${GITHUB_ZIP_URL:-https://github.com/patrick-mulinge/WTR-Lab-crawler/archive/refs/heads/linuxmultiworker.zip}"

IS_INTERACTIVE=0
ARCH="$(uname -m)"

info()  { echo "[*] $*"; }
ok()    { echo "[+] $*"; }
warn()  { echo "[!] $*"; }
err()   { echo "[x] $*" >&2; }


detect_interactive() {
  if [[ -t 0 ]] && [[ -t 1 ]]; then IS_INTERACTIVE=1; else IS_INTERACTIVE=0; fi
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


dotenv_get() {
  local key="$1"
  [[ -f "$ENV_FILE" ]] || return 0
  grep -E "^${key}=" "$ENV_FILE" 2>/dev/null | head -1 | cut -d= -f2- || true
}


dotenv_set() {
  local key="$1"
  local value="$2"
  local tmp
  tmp="$(mktemp)"
  touch "$ENV_FILE"
  if grep -qE "^${key}=" "$ENV_FILE" 2>/dev/null; then
    sed -E "s|^${key}=.*|${key}=${value}|" "$ENV_FILE" >"$tmp"
    mv "$tmp" "$ENV_FILE"
  else
    echo "${key}=${value}" >>"$ENV_FILE"
    rm -f "$tmp"
  fi
}


project_ok() { [[ -f "$APP_FILE" && -f "$REQ_FILE" ]]; }


ensure_project() {
  if project_ok; then
    ok "Project files found."
    return
  fi
  warn "app.py or requirements.txt missing — downloading..."
  [[ -n "$GITHUB_ZIP_URL" ]] || { err "No GITHUB_ZIP_URL"; exit 1; }
  local tmpzip tmpdir inner
  tmpzip="$(mktemp)"
  tmpdir="$(mktemp -d)"
  curl -fsSL "$GITHUB_ZIP_URL" -o "$tmpzip"
  unzip -q "$tmpzip" -d "$tmpdir"
  inner="$(find "$tmpdir" -mindepth 1 -maxdepth 1 -type d | head -1)"
  [[ -n "$inner" ]] || { err "Empty archive"; exit 1; }
  cp -R "$inner"/* .
  rm -rf "$tmpzip" "$tmpdir"
  project_ok || { err "Download incomplete"; exit 1; }
  ok "Project downloaded."
}


detect_os() {
  [[ -f /etc/os-release ]] || { err "No /etc/os-release"; exit 1; }
  # shellcheck disable=SC1091
  source /etc/os-release
  command -v dnf >/dev/null 2>&1 || { err "dnf required"; exit 1; }
  ok "OS: ${PRETTY_NAME:-$ID}  arch=$ARCH"
}


ensure_sudo() {
  if [[ $EUID -eq 0 ]]; then
    err "Do not run as root. Use: ./\"Start for Linux.sh\""
    exit 1
  fi
  command -v sudo >/dev/null 2>&1 || { err "sudo required"; exit 1; }
  sudo -v
  ok "sudo OK."
}


pkg_installed() { rpm -q "$1" >/dev/null 2>&1; }


# Install packages one-by-one so a missing name never blocks the rest
pkg_install_each() {
  local p
  for p in "$@"; do
    if pkg_installed "$p"; then
      ok "$p already installed."
      continue
    fi
    info "Installing $p ..."
    if sudo dnf install -y "$p"; then
      ok "$p installed."
    else
      warn "Could not install $p (skipping)."
    fi
  done
}


ensure_basic_tools() {
  info "Basic tools..."
  pkg_install_each ca-certificates curl wget unzip gnupg2 tar file
}


ensure_env_first() {
  info "Checking .env (one-time prompts only for missing keys)..."
  mkdir -p data
  touch "$ENV_FILE"
  chmod 600 "$ENV_FILE"

  local token server_id db_url allowed admin_ids admin_chat
  local cap daily tmin tmax headless

  token="$(dotenv_get BOT_TOKENn)"
  server_id="$(dotenv_get SERVER_IDn)"
  db_url="$(dotenv_get CHAPTER_DATABASE_URL)"
  [[ -n "$db_url" ]] || db_url="$(dotenv_get DATABASE_URL)"
  allowed="$(dotenv_get ALLOWED_USER_IDS)"
  admin_ids="$(dotenv_get ADMIN_USER_IDS)"
  admin_chat="$(dotenv_get ADMIN_CHAT_ID)"
  cap="$(dotenv_get CHAPTER_CAP)"
  daily="$(dotenv_get DAILY_TASK_LIMIT)"
  tmin="$(dotenv_get CHAPTER_THROTTLE_MIN)"
  tmax="$(dotenv_get CHAPTER_THROTTLE_MAX)"
  headless="$(dotenv_get HEADLESS)"

  if [[ "$IS_INTERACTIVE" -eq 0 ]]; then
    if [[ -z "$token" || "$token" == *"your_token"* ]]; then
      err "Non-interactive: set BOT_TOKENn in .env or environment."
      exit 1
    fi
    # Do not invent a coordinator id offline; app.py picks a random worker id if unset.
    ok ".env present (non-interactive)."
    return
  fi

  local need_wizard=0
  if [[ -z "$token" || "$token" == *"your_token"* ]]; then need_wizard=1; fi
  if [[ -z "$server_id" ]]; then need_wizard=1; fi

  if [[ "$need_wizard" -eq 0 ]]; then
    ok "BOT_TOKENn and SERVER_IDn already set — skipping prompts."
    ok "  SERVER_IDn=$server_id  (edit .env or /set to change)"
    [[ -n "$(dotenv_get CLOUDFLARED_BIN)" ]] || dotenv_set CLOUDFLARED_BIN "$CLOUDFLARED_BIN_PATH"
    [[ -n "$(dotenv_get NOVNC_WEB_DIR)" ]] || dotenv_set NOVNC_WEB_DIR "$NOVNC_DIR"
    [[ -n "$(dotenv_get CHROME_PROFILE_DIR)" ]] || dotenv_set CHROME_PROFILE_DIR "data/chrome-profile"
    [[ -n "$(dotenv_get REMOTE_SOLVE)" ]] || dotenv_set REMOTE_SOLVE "1"
    return
  fi

  echo
  echo "=========================================="
  echo "   First-time .env setup (one time only)"
  echo "=========================================="
  echo "Later: edit .env or use bot /set — this script will not ask again."
  echo

  if [[ -z "$token" || "$token" == *"your_token"* ]]; then
    token="${BOT_TOKENn:-}"
    while true; do
      prompt_or_env token "BOT_TOKENn (required, not BOT_TOKEN)" ""
      [[ -n "$token" ]] && break
      warn "Token required."
    done
    dotenv_set BOT_TOKENn "$token"
  else
    ok "BOT_TOKENn already set."
  fi

  if [[ -z "$server_id" ]]; then
    server_id="${SERVER_IDn:-}"
    echo
    echo "SERVER_IDn — unique id for this WTR machine (s4 = coordinator)."
    echo "  s1     = coordinator (by convention)"
    echo "  s2-s10 = workers"
    prompt_or_env server_id "SERVER_IDn (blank=random worker; s4=coordinator)" ""
    server_id="$(echo "$server_id" | tr '[:upper:]' '[:lower:]' | xargs)"
    [[ -n "$server_id" ]] || server_id=""
    if [[ -n "$server_id" ]]; then dotenv_set SERVER_IDn "$server_id"; fi
  else
    ok "SERVER_IDn already set: $server_id"
  fi

  # Northflank = light writes (tasks, heartbeats, premium, email locks).
  db_password="$(dotenv_get DB_PASSWORD)"
  if [[ -z "$db_password" ]]; then
    echo
    echo "DB_PASSWORD — Northflank shared TASK queue (multi-worker)."
    echo "  Leave empty = local SQLite tasks only (single server)."
    echo "  Host/db/user are built into app.py; only the password is in .env."
    db_password=""
    prompt_or_env db_password "DB_PASSWORD (Northflank tasks, optional)" ""
    dotenv_set DB_PASSWORD "$db_password"
  else
    ok "DB_PASSWORD already set (not re-prompting)."
  fi

  # Coordinator Postgres = heavy chapter metadata (pulls per chapter, chapter index).
  if [[ -z "$db_url" ]]; then
    echo
    echo "CHAPTER_DATABASE_URL — coordinator CHAPTER metadata Postgres (same-region workers)."
    echo "  Per-chapter pull records and chapter index go here (not Northflank)."
    echo "  Example: postgresql://wtr:pass@COORD_PRIVATE_IP:5432/wtr_chapters"
    echo "  Leave empty = local SQLite for chapter metadata."
    db_url="${CHAPTER_DATABASE_URL:-${DATABASE_URL:-}}"
    prompt_or_env db_url "CHAPTER_DATABASE_URL (coordinator chapters, optional)" ""
    if [[ -n "$db_url" ]]; then dotenv_set CHAPTER_DATABASE_URL "$db_url"; fi
  else
    ok "CHAPTER_DATABASE_URL already set (not re-prompting)."

  # NFS defaults for multi-worker
  nfs_ip="$(dotenv_get NFS_SERVER_IP)"
  if [[ -z "$nfs_ip" ]]; then
    echo "NFS_SERVER_IP — private IP of coordinator that exports the library (default 10.0.0.211)."
    prompt_or_env nfs_ip "NFS_SERVER_IP" "10.0.0.211"
    dotenv_set NFS_SERVER_IP "${nfs_ip:-10.0.0.211}"
  else
    ok "NFS_SERVER_IP already set: $nfs_ip"
  fi
  nfs_cidr="$(dotenv_get NFS_EXPORT_CIDR)"
  if [[ -z "$nfs_cidr" ]]; then
    prompt_or_env nfs_cidr "NFS_EXPORT_CIDR (VCN)" "10.0.0.0/16"
    dotenv_set NFS_EXPORT_CIDR "${nfs_cidr:-10.0.0.0/16}"
  fi
  fi

  qp="$(dotenv_get QUEUE_POOL)"
  if [[ -z "$qp" ]]; then
    echo
    echo "QUEUE_POOL — which tasks this machine claims:"
    echo "  normal  = free queue (default)"
    echo "  premium = premium queue only"
    echo "  both    = either"
    qp=""
    prompt_or_env qp "QUEUE_POOL" "normal"
    dotenv_set QUEUE_POOL "${qp:-normal}"
  else
    ok "QUEUE_POOL already set: $qp"
  fi

  lib="$(dotenv_get NFS_LIBRARY_PATH)"
  [[ -n "$lib" ]] || lib="$(dotenv_get LIBRARY_DIR)"
  if [[ -z "$lib" ]]; then
    echo
    echo "NFS_LIBRARY_PATH — chapter files root (NFS mount on workers, or local data/library)."
    echo "  Example: /home/opc/data/library"
    lib=""
    prompt_or_env lib "NFS_LIBRARY_PATH (optional, default data/library)" ""
    if [[ -n "$lib" ]]; then
      dotenv_set NFS_LIBRARY_PATH "$lib"
    fi
  else
    ok "NFS_LIBRARY_PATH already set: $lib"
  fi

  if [[ -z "$cap" ]]; then
    prompt_or_env allowed "ALLOWED_USER_IDS (optional)" ""
    prompt_or_env admin_ids "ADMIN_USER_IDS (optional)" ""
    prompt_or_env admin_chat "ADMIN_CHAT_ID (optional)" ""
    prompt_or_env cap "CHAPTER_CAP" "0"
    prompt_or_env daily "DAILY_TASK_LIMIT" "0"
    prompt_or_env tmin "CHAPTER_THROTTLE_MIN" "18"
    prompt_or_env tmax "CHAPTER_THROTTLE_MAX" "24"
    prompt_or_env headless "HEADLESS (0=visible for Turnstile)" "0"
    local remote_solve=""
    prompt_or_env remote_solve "REMOTE_SOLVE (1=send user a one-time Turnstile link)" "1"
    dotenv_set ALLOWED_USER_IDS "$allowed"
    dotenv_set ADMIN_USER_IDS "$admin_ids"
    dotenv_set ADMIN_CHAT_ID "$admin_chat"
    dotenv_set OUTPUT_GROUPS ""
    dotenv_set CHAPTER_CAP "${cap:-0}"
    dotenv_set DAILY_TASK_LIMIT "${daily:-0}"
    dotenv_set CHAPTER_THROTTLE_MIN "${tmin:-18}"
    dotenv_set CHAPTER_THROTTLE_MAX "${tmax:-24}"
    dotenv_set PROGRESS_UPDATE_SECONDS "25"
    dotenv_set HEADLESS "${headless:-0}"
    dotenv_set REMOTE_SOLVE "${remote_solve:-1}"
  fi

  dotenv_set CHROME_PROFILE_DIR "data/chrome-profile"
  dotenv_set CLOUDFLARED_BIN "$CLOUDFLARED_BIN_PATH"
  dotenv_set NOVNC_WEB_DIR "$NOVNC_DIR"
  dotenv_set VNC_PORT "$VNC_PORT"
  dotenv_set NOVNC_PORT "$NOVNC_PORT"
  # Older installs: default ON if key missing
  [[ -n "$(dotenv_get REMOTE_SOLVE)" ]] || dotenv_set REMOTE_SOLVE "1"

  ok ".env ready (SERVER_IDn=$(dotenv_get SERVER_IDn))."
}


pick_python_bin() {
  local cand
  for cand in python3.12 python3.11 python3.10 python3; do
    if command -v "$cand" >/dev/null 2>&1; then
      if "$cand" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)' 2>/dev/null; then
        echo "$cand"
        return 0
      fi
    fi
  done
  return 1
}


ensure_python() {
  local bin=""
  bin="$(pick_python_bin 2>/dev/null || true)"
  if [[ -n "$bin" ]]; then
    export WTR_PYTHON_BIN="$bin"
    ok "Python: $($bin --version 2>&1) ($bin)"
    return
  fi
  warn "Python 3.10+ missing — installing..."
  pkg_install_each python3 python3-pip python3-devel
  pkg_install_each python3.12 python3.12-pip python3.12-devel
  pkg_install_each python3.11 python3.11-pip python3.11-devel
  pkg_install_each gcc gcc-c++ make libffi-devel openssl-devel
  bin="$(pick_python_bin 2>/dev/null || true)"
  export WTR_PYTHON_BIN="${bin:-python3}"
  command -v "$WTR_PYTHON_BIN" >/dev/null || { err "No python"; exit 1; }
  ok "Using $WTR_PYTHON_BIN"
}


ensure_venv() {
  if [[ -x "$VENV_PY" ]] && "$VENV_PY" -m pip --version >/dev/null 2>&1; then
    ok "venv OK."
    return
  fi
  [[ -d .venv ]] && rm -rf .venv
  info "Creating .venv..."
  "${WTR_PYTHON_BIN:-python3}" -m venv .venv
  "$VENV_PY" -m ensurepip --upgrade 2>/dev/null || true
  "$VENV_PY" -m pip --version >/dev/null || { err "pip missing in venv"; exit 1; }
  ok "venv created."
}


ensure_deps() {
  info "pip install -r requirements.txt ..."
  "$VENV_PY" -m pip install --upgrade pip setuptools wheel
  "$VENV_PY" -m pip install -r "$REQ_FILE"
  "$VENV_PY" -m pip install 'websockify>=0.11' || true
  "$VENV_PY" -m pip install 'psycopg2-binary>=2.9' || true
  ok "Python deps installed."
}


chrome_installed() {
  command -v google-chrome >/dev/null 2>&1 \
    || command -v google-chrome-stable >/dev/null 2>&1
}


ensure_chrome() {
  if chrome_installed; then
    ok "Google Chrome: $(command -v google-chrome || command -v google-chrome-stable)"
    return
  fi
  local tmp_rpm
  tmp_rpm="$(mktemp --suffix=.rpm)"
  case "$ARCH" in
    x86_64|amd64)
      info "Downloading Chrome x86_64 RPM..."
      curl -fL "https://dl.google.com/linux/direct/google-chrome-stable_current_x86_64.rpm" -o "$tmp_rpm"
      sudo dnf install -y "$tmp_rpm" || true
      ;;
    aarch64|arm64)
      info "Downloading Chrome aarch64 RPM..."
      if curl -fL "https://dl.google.com/linux/direct/google-chrome-stable_current_aarch64.rpm" -o "$tmp_rpm" 2>/dev/null; then
        sudo dnf install -y "$tmp_rpm" || true
      elif curl -fL "https://dl.google.com/linux/direct/google-chrome-stable_current_arm64.rpm" -o "$tmp_rpm" 2>/dev/null; then
        sudo dnf install -y "$tmp_rpm" || true
      else
        err "Chrome ARM RPM not available."
        rm -f "$tmp_rpm"
        exit 1
      fi
      ;;
    *)
      err "Unsupported arch: $ARCH"
      rm -f "$tmp_rpm"
      exit 1
      ;;
  esac
  rm -f "$tmp_rpm"
  chrome_installed || { err "Chrome install failed"; exit 1; }
  ok "Google Chrome installed."
}


uc_driver_path() {
  local p
  p="$("$VENV_PY" -c 'import pathlib, seleniumbase; print(pathlib.Path(seleniumbase.__file__).parent / "drivers" / "uc_driver")' 2>/dev/null || true)"
  if [[ -n "$p" ]]; then
    echo "$p"
  else
    echo ".venv/lib/python3.12/site-packages/seleniumbase/drivers/uc_driver"
  fi
}


ensure_arm_uc_driver() {
  case "$ARCH" in
    aarch64|arm64) ;;
    *) return 0 ;;
  esac

  [[ -x "$VENV_PY" ]] || return 0

  local dest
  dest="$(uc_driver_path)"
  mkdir -p "$(dirname "$dest")"

  if [[ -e "$dest" ]]; then
    local kind
    kind="$(file -b "$dest" 2>/dev/null || true)"
    if echo "$kind" | grep -qiE 'ARM aarch64|aarch64'; then
      ok "uc_driver already aarch64 — not replacing."
      return 0
    fi
    warn "uc_driver is not aarch64 ($kind) — replacing from Chrome for Testing..."
  else
    info "No uc_driver yet — fetching linux-arm64 chromedriver..."
  fi

  if ! chrome_installed; then
    warn "Chrome not installed; skip ARM driver fetch."
    return 0
  fi

  local ver
  ver="$(google-chrome --version 2>/dev/null | grep -oE '[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+' | head -1 || true)"
  [[ -n "$ver" ]] || ver="$(google-chrome-stable --version 2>/dev/null | grep -oE '[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+' | head -1 || true)"
  if [[ -z "$ver" ]]; then
    warn "Could not read Chrome version."
    return 0
  fi
  info "Chrome version $ver — downloading chromedriver linux-arm64..."

  local tmp src
  tmp="$(mktemp -d)"
  if ! curl -fL \
      "https://storage.googleapis.com/chrome-for-testing-public/${ver}/linux-arm64/chromedriver-linux-arm64.zip" \
      -o "$tmp/cd.zip"; then
    warn "Download failed for $ver linux-arm64 chromedriver."
    rm -rf "$tmp"
    return 0
  fi
  unzip -q -o "$tmp/cd.zip" -d "$tmp"
  src="$(find "$tmp" -type f -name chromedriver | head -1)"
  if [[ -z "$src" || ! -f "$src" ]]; then
    warn "chromedriver binary not found in zip."
    rm -rf "$tmp"
    return 0
  fi
  cp "$src" "$dest"
  chmod +x "$dest"
  rm -rf "$tmp"
  ok "Installed aarch64 uc_driver → $dest"
  file "$dest" || true
}


ensure_display_packages() {
  info "Display / VNC packages (one at a time; no openbox)..."
  pkg_install_each xorg-x11-server-Xvfb tigervnc-server xorg-x11-xauth mesa-libGL
}


ensure_novnc() {
  if [[ -f "$NOVNC_DIR/vnc.html" ]]; then
    ok "noVNC: $NOVNC_DIR"
    return
  fi
  info "Installing noVNC..."
  local tmp
  tmp="$(mktemp -d)"
  if curl -fsSL "https://github.com/novnc/noVNC/archive/refs/tags/v1.5.0.tar.gz" -o "$tmp/novnc.tgz"; then
    tar -xzf "$tmp/novnc.tgz" -C "$tmp"
    rm -rf "$NOVNC_DIR"
    mv "$tmp"/noVNC-* "$NOVNC_DIR"
    ok "noVNC installed."
  else
    warn "noVNC download failed."
  fi
  rm -rf "$tmp"
}


ensure_cloudflared() {
  if command -v cloudflared >/dev/null 2>&1; then
    ok "cloudflared: $(command -v cloudflared)"
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
  case "$ARCH" in
    x86_64|amd64) url="https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64" ;;
    aarch64|arm64) url="https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-arm64" ;;
    *) warn "No cloudflared for $ARCH"; return ;;
  esac
  if curl -fL "$url" -o "$CLOUDFLARED_BIN_PATH"; then
    chmod +x "$CLOUDFLARED_BIN_PATH"
    dotenv_set CLOUDFLARED_BIN "$CLOUDFLARED_BIN_PATH"
    ok "cloudflared installed."
  else
    warn "cloudflared download failed."
  fi
}


start_xvfb_if_needed() {
  if pgrep -f "Xvfb :${DISPLAY_NUM}" >/dev/null 2>&1; then
    ok "Xvfb :${DISPLAY_NUM} running."
  else
    if ! command -v Xvfb >/dev/null 2>&1; then
      warn "Xvfb not installed."
      return
    fi
    info "Starting Xvfb :${DISPLAY_NUM}..."
    nohup Xvfb ":${DISPLAY_NUM}" -screen 0 1280x720x24 -ac +extension GLX +render -noreset \
      >>"${HOME}/xvfb.log" 2>&1 &
    sleep 1
    ok "Xvfb started."
  fi
}

# ---------------------------------------------------------------------------
# NFS: s4 exports library; other servers mount it
# Defaults (override in .env): NFS_SERVER_IP, NFS_EXPORT_CIDR, NFS_LIBRARY_PATH
# ---------------------------------------------------------------------------
ensure_nfs() {
  local sid lib ip cidr
  sid="$(dotenv_get SERVER_IDn)"
  [[ -n "$sid" ]] || sid="$(dotenv_get SERVER_ID)"
  lib="$(dotenv_get NFS_LIBRARY_PATH)"
  [[ -n "$lib" ]] || lib="${HOME}/data/library"
  ip="$(dotenv_get NFS_SERVER_IP)"
  [[ -n "$ip" ]] || ip="10.0.0.211"
  cidr="$(dotenv_get NFS_EXPORT_CIDR)"
  [[ -n "$cidr" ]] || cidr="10.0.0.0/16"

  mkdir -p "$(dirname "$lib")" 2>/dev/null || sudo mkdir -p "$(dirname "$lib")"
  sudo mkdir -p "$lib"
  sudo chown -R "$(id -u):$(id -g)" "$(dirname "$lib")" 2>/dev/null || true

  if command -v dnf >/dev/null 2>&1; then
    sudo dnf install -y nfs-utils >/dev/null 2>&1 || true
  elif command -v apt-get >/dev/null 2>&1; then
    sudo apt-get install -y nfs-common nfs-kernel-server >/dev/null 2>&1 || true
  fi

  if [[ "${sid,,}" == "s4" || "${sid,,}" == "coord" || "${sid,,}" == "coordinator" ]]; then
    info "NFS server mode (SERVER_IDn=$sid) exporting $lib to $cidr"
    sudo chown -R "$(id -u):$(id -g)" "$lib" 2>/dev/null || true
    sudo chmod 775 "$lib" 2>/dev/null || true
    echo "$lib $cidr(rw,sync,no_subtree_check,no_root_squash)" | sudo tee /etc/exports >/dev/null
    sudo systemctl enable --now nfs-server 2>/dev/null || sudo systemctl enable --now nfs-kernel-server 2>/dev/null || true
    sudo exportfs -ra 2>/dev/null || true
    if command -v firewall-cmd >/dev/null 2>&1; then
      sudo firewall-cmd --permanent --add-service=nfs 2>/dev/null || true
      sudo firewall-cmd --permanent --add-service=rpc-bind 2>/dev/null || true
      sudo firewall-cmd --permanent --add-service=mountd 2>/dev/null || true
      sudo firewall-cmd --reload 2>/dev/null || true
    fi
    ok "NFS export ready: $lib"
  else
    info "NFS client mode (SERVER_IDn=${sid:-worker}) mount $ip:$lib → $lib"
    if mountpoint -q "$lib" 2>/dev/null; then
      ok "NFS already mounted at $lib"
    else
      sudo mkdir -p "$lib"
      if sudo mount -t nfs4 -o vers=4,timeo=30 "$ip:$lib" "$lib" 2>/dev/null; then
        ok "Mounted NFS $ip:$lib"
      else
        warn "NFS mount failed ($ip:$lib). Check export, firewall, NFS_SERVER_IP in .env"
      fi
    fi
    if ! grep -qF "$ip:$lib" /etc/fstab 2>/dev/null; then
      echo "$ip:$lib $lib nfs4 defaults,_netdev 0 0" | sudo tee -a /etc/fstab >/dev/null || true
    fi
  fi
  # chrome-profile must stay local and writable
  mkdir -p "${HOME}/data/chrome-profile" 2>/dev/null || true
  sudo chown -R "$(id -u):$(id -g)" "${HOME}/data/chrome-profile" 2>/dev/null || true
}


start_vnc_if_needed() {
  if pgrep -x x0vncserver >/dev/null 2>&1 || ss -ltn 2>/dev/null | grep -q ":${VNC_PORT}"; then
    ok "x0vncserver / :${VNC_PORT} up."
    return
  fi
  local vnc_bin
  vnc_bin="$(command -v x0vncserver || true)"
  if [[ -z "$vnc_bin" ]]; then
    warn "x0vncserver not found."
    return
  fi
  info "Starting x0vncserver..."
  nohup "$vnc_bin" -display ":${DISPLAY_NUM}" -rfbport "$VNC_PORT" \
    -localhost yes -SecurityTypes None >>"${HOME}/x0vncserver.log" 2>&1 &
  sleep 1
  ok "x0vncserver started."
}


start_websockify_if_needed() {
  if ss -ltn 2>/dev/null | grep -q ":${NOVNC_PORT}"; then
    ok "websockify / :${NOVNC_PORT} up."
    return
  fi
  [[ -f "$NOVNC_DIR/vnc.html" ]] || { warn "noVNC missing"; return; }
  [[ -x "$VENV_PY" ]] || return
  info "Starting websockify..."
  nohup "$VENV_PY" -u -m websockify --web "$NOVNC_DIR" \
    "127.0.0.1:${NOVNC_PORT}" "127.0.0.1:${VNC_PORT}" \
    >>"${HOME}/websockify.log" 2>&1 &
  sleep 1
  ok "websockify started."
}


ensure_cron_watchdogs() {
  local tmp vnc_bin
  tmp="$(mktemp)"
  crontab -l 2>/dev/null | grep -v 'x0vncserver' | grep -v 'websockify' | grep -v "Xvfb :${DISPLAY_NUM}" >"$tmp" || true
  vnc_bin="$(command -v x0vncserver || echo /usr/bin/x0vncserver)"
  cat >>"$tmp" <<EOF
@reboot /usr/bin/Xvfb :${DISPLAY_NUM} -screen 0 1280x720x24 -ac +extension GLX +render -noreset >> ${HOME}/xvfb.log 2>&1
@reboot sleep 2; ${vnc_bin} -display :${DISPLAY_NUM} -rfbport ${VNC_PORT} -localhost yes -SecurityTypes None >> ${HOME}/x0vncserver.log 2>&1
@reboot sleep 3; ${PWD}/.venv/bin/python -u -m websockify --web ${NOVNC_DIR} 127.0.0.1:${NOVNC_PORT} 127.0.0.1:${VNC_PORT} >> ${HOME}/websockify.log 2>&1
*/5 * * * * pgrep -f 'Xvfb :${DISPLAY_NUM}' >/dev/null || /usr/bin/Xvfb :${DISPLAY_NUM} -screen 0 1280x720x24 -ac +extension GLX +render -noreset >> ${HOME}/xvfb.log 2>&1
*/5 * * * * pgrep -x x0vncserver >/dev/null || ${vnc_bin} -display :${DISPLAY_NUM} -rfbport ${VNC_PORT} -localhost yes -SecurityTypes None >> ${HOME}/x0vncserver.log 2>&1
*/5 * * * * ss -ltn | grep -q ':${NOVNC_PORT}' || ${PWD}/.venv/bin/python -u -m websockify --web ${NOVNC_DIR} 127.0.0.1:${NOVNC_PORT} 127.0.0.1:${VNC_PORT} >> ${HOME}/websockify.log 2>&1
EOF
  crontab "$tmp"
  rm -f "$tmp"
  ok "Cron watchdogs installed."
}


ensure_data_dir() { mkdir -p data data/chrome-profile data/library; }
mark_setup_done() { ensure_data_dir; date -Iseconds >"$MARKER"; }
test_setup_done() {
  [[ -f "$MARKER" && -x "$VENV_PY" && -f "$ENV_FILE" ]] \
    && "$VENV_PY" -m pip --version >/dev/null 2>&1
}


fix_project_permissions() {
  local u g
  u="$(id -un)"; g="$(id -gn)"
  for item in .venv data "$ENV_FILE"; do
    [[ -e "$item" ]] && sudo chown -R "$u:$g" "$item" 2>/dev/null || true
  done
}


stop_chrome_if_needed() {
  info "Stopping previous app.py / Chrome / driver..."
  pkill -9 -f 'app\.py|uc_driver|google-chrome|chromedriver' 2>/dev/null || true
  sleep 2
  ok "Cleared."
}


mkdir -p data
detect_interactive

echo
echo "=========================================="
echo "   WTR-Lab Start (x86_64 + aarch64)"
echo "=========================================="
echo

detect_os
ensure_sudo
ensure_basic_tools
ensure_project
ensure_data_dir
ensure_env_first

if ! test_setup_done; then
  info "First-time setup..."
  ensure_python
  ensure_display_packages
  ensure_chrome
  ensure_venv
  ensure_deps
  ensure_arm_uc_driver
  ensure_novnc
  ensure_cloudflared
  mark_setup_done
  fix_project_permissions
  ok "Setup complete."
else
  ok "Setup already done — skip installs; start services + app only."
  if [[ ! -x "$VENV_PY" ]]; then
    ensure_python
    ensure_venv
    ensure_deps
  fi
  chrome_installed || ensure_chrome
  ensure_arm_uc_driver
fi

export DISPLAY=":${DISPLAY_NUM}"
ensure_nfs
start_xvfb_if_needed
start_vnc_if_needed
start_websockify_if_needed
ensure_cron_watchdogs

stop_chrome_if_needed

echo
echo "=========================================="
echo "          Starting app.py"
echo "=========================================="
echo
echo "SERVER_IDn=$(dotenv_get SERVER_IDn)  DISPLAY=$DISPLAY  arch=$ARCH"
if grep -qE '^HEADLESS=0' "$ENV_FILE" 2>/dev/null; then
  echo "HEADLESS=0 — solve Turnstile over VNC, then /solved"
else
  echo "HEADLESS=1 — set HEADLESS=0 in .env for visible Chrome"
fi
echo "VNC 127.0.0.1:${VNC_PORT}  noVNC 127.0.0.1:${NOVNC_PORT}"
echo

LOG_FILE="${HOME}/bot.log"
info "nohup app.py → $LOG_FILE"
nohup "$VENV_PY" "$APP_FILE" >"$LOG_FILE" 2>&1 &
WORKER_PID=$!
sleep 1
if kill -0 "$WORKER_PID" 2>/dev/null; then
  ok "Started PID $WORKER_PID"
  echo "  tail -f $LOG_FILE"
  echo "Stop: pkill -9 -f 'app.py|uc_driver|google-chrome|chromedriver'"
else
  err "app.py failed — see $LOG_FILE"
  exit 1
fi
