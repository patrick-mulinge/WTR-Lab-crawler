from __future__ import annotations

import base64
import hashlib
import html
import json
import logging
import mimetypes
import os
import random
import re
import shutil
import sqlite3
import subprocess
import sys
import queue
import secrets
import math
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

import requests
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from bs4 import BeautifulSoup
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from dotenv import load_dotenv
from ebooklib import epub
from seleniumbase import Driver
from selenium.common.exceptions import (
    InvalidSessionIdException,
    NoSuchWindowException,
    TimeoutException,
    WebDriverException,
)
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
import telebot
from telebot.types import LinkPreviewOptions

# Telegram Bot API: disable_web_page_preview is deprecated.
LINK_PREVIEW_OFF = LinkPreviewOptions(is_disabled=True)


# ---------------------------------------------------------------------------
# Configuration — fully local (SQLite on this PC, no shared Postgres)
# ---------------------------------------------------------------------------

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

# ---------------------------------------------------------------------------
# Logging: everything (print output, errors, tracebacks, incoming Telegram
# updates, task picks, DB info) goes to bot.log and the console.
# LOG_LEVEL=DEBUG for more detail, TELEBOT_DEBUG=1 to also log every raw
# Telegram API call.
# ---------------------------------------------------------------------------

LOG_PATH = Path(os.environ.get("LOG_FILE") or (BASE_DIR / "bot.log"))
log = logging.getLogger("wtr")


class _LogStream:
    """File-like object that turns print()/stderr output into log records."""

    encoding = "utf-8"

    def __init__(self, level: int, original):
        self._level = level
        self._original = original
        self._buf = ""
        self._lock = threading.Lock()

    def write(self, text):
        if not text:
            return 0
        if isinstance(text, bytes):
            text = text.decode("utf-8", "replace")
        with self._lock:
            self._buf += text
            lines = []
            while "\n" in self._buf:
                line, self._buf = self._buf.split("\n", 1)
                lines.append(line)
        for line in lines:
            if line.strip():
                log.log(self._level, line.rstrip())
        return len(text)

    def flush(self):
        pass

    def isatty(self):
        return False

    def fileno(self):
        return self._original.fileno()

    def writable(self):
        return True


def setup_logging() -> None:
    level_name = (os.environ.get("LOG_LEVEL") or "INFO").strip().upper()
    level = getattr(logging, level_name, logging.INFO)
    fmt = logging.Formatter(
        "%(asctime)s [%(levelname)s] [%(threadName)s] %(message)s"
    )
    root = logging.getLogger()
    root.setLevel(level)
    for handler in list(root.handlers):
        root.removeHandler(handler)

    real_stdout = sys.__stdout__
    console = logging.StreamHandler(real_stdout)
    console.setFormatter(fmt)
    root.addHandler(console)

    # If the start script already redirects stdout into bot.log, writing the
    # file ourselves would duplicate every line, so only add the file handler
    # when stdout is NOT already that file.
    already_redirected = False
    try:
        if LOG_PATH.exists():
            already_redirected = os.path.samestat(
                os.fstat(real_stdout.fileno()), os.stat(LOG_PATH)
            )
    except Exception:
        already_redirected = False
    if not already_redirected:
        try:
            from logging.handlers import RotatingFileHandler

            file_handler = RotatingFileHandler(
                LOG_PATH, maxBytes=20 * 1024 * 1024, backupCount=5, encoding="utf-8"
            )
            file_handler.setFormatter(fmt)
            root.addHandler(file_handler)
        except Exception as error:
            real_stdout.write(f"[LOGGING] cannot open {LOG_PATH}: {error}\n")

    # TeleBot ships its own stderr handler; route it through ours instead.
    tb_logger = logging.getLogger("TeleBot")
    for handler in list(tb_logger.handlers):
        tb_logger.removeHandler(handler)
    tb_logger.propagate = True
    flag = (os.environ.get("TELEBOT_DEBUG") or "").strip().lower()
    tb_logger.setLevel(logging.DEBUG if flag in ("1", "true", "yes", "on") else logging.INFO)

    for noisy in ("urllib3", "selenium", "websockets", "asyncio"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    # print() and stderr now land in the log too.
    sys.stdout = _LogStream(logging.INFO, real_stdout)
    sys.stderr = _LogStream(logging.WARNING, sys.__stderr__)

    def _excepthook(exc_type, exc, tb):
        if issubclass(exc_type, KeyboardInterrupt):
            sys.__excepthook__(exc_type, exc, tb)
            return
        log.critical("Uncaught exception", exc_info=(exc_type, exc, tb))

    def _thread_excepthook(args):
        if args.exc_type is SystemExit:
            return
        log.critical(
            "Uncaught exception in thread %s",
            getattr(args.thread, "name", "?"),
            exc_info=(args.exc_type, args.exc_value, args.exc_traceback),
        )

    sys.excepthook = _excepthook
    threading.excepthook = _thread_excepthook
    log.info("=" * 70)
    log.info("Logging started: file=%s level=%s", LOG_PATH, level_name)


setup_logging()

# Share .env with another bot: that bot uses BOT_TOKEN / SERVER_ID.
# This WTR bot uses BOT_TOKENn / SERVER_IDn (coordinator = s4).
BOT_TOKEN = (os.environ.get("BOT_TOKENn") or "").strip()
if not BOT_TOKEN:
    raise SystemExit(
        "BOT_TOKENn is missing. Set BOT_TOKENn in .env (not BOT_TOKEN — that "
        "belongs to the other bot on this host) from @BotFather."
    )

def parse_id_list(raw: str) -> set[int]:
    """Accept '123', '123,456', '123, 456', '123 456' style lists."""
    if not raw or not str(raw).strip():
        return set()
    parts = re.split(r"[,;\s]+", str(raw).strip())
    out: set[int] = set()
    for part in parts:
        part = part.strip()
        if part.lstrip("-").isdigit():
            out.add(int(part))
    return out


# Who may use the bot. Empty = anyone who can message it.
ALLOWED_USER_IDS = parse_id_list(os.environ.get("ALLOWED_USER_IDS", ""))

# Admins: immune to CHAPTER_CAP and DAILY_TASK_LIMIT. Comma/space separated.
ADMIN_USER_IDS = parse_id_list(os.environ.get("ADMIN_USER_IDS", ""))

# Optional chat that receives a *copy* of finished books.
# If it is a numeric user id and ADMIN_USER_IDS was empty, treat it as admin too
# (common personal-bot setup: only ADMIN_CHAT_ID is set).
ADMIN_CHAT_ID = os.environ.get("ADMIN_CHAT_ID", "").strip()
if not ADMIN_USER_IDS and ADMIN_CHAT_ID.lstrip("-").isdigit():
    ADMIN_USER_IDS.add(int(ADMIN_CHAT_ID))

OUTPUT_GROUPS = [
    item.strip()
    for item in os.environ.get("OUTPUT_GROUPS", "").split(",")
    if item.strip()
]

CHROME_PROFILE_DIR = BASE_DIR / os.environ.get(
    "CHROME_PROFILE_DIR", "data/chrome-profile"
)
DATA_DIR = BASE_DIR / "data"
LIBRARY_DIR = Path(
    (os.environ.get("NFS_LIBRARY_PATH") or os.environ.get("LIBRARY_DIR") or "").strip()
    or str(DATA_DIR / "library")
)
SQLITE_PATH = DATA_DIR / "worker.sqlite3"

PROGRESS_UPDATE_SECONDS = int(os.environ.get("PROGRESS_UPDATE_SECONDS", "25"))
HEARTBEAT_SECONDS = int(os.environ.get("HEARTBEAT_SECONDS", "600"))  # 10 min (in-crawl progress touch)
HEARTBEAT_THREAD_SECONDS = int(os.environ.get("HEARTBEAT_THREAD_SECONDS", "30"))  # background "seen" beat
# Telegram bots (cloud Bot API) cannot upload files above 50 MB.
TELEGRAM_BOT_UPLOAD_LIMIT = 50 * 1024 * 1024
# Split parts are built to stay under this size (default 45 MB) for safety.
TELEGRAM_SPLIT_TARGET_BYTES = int(float(os.environ.get("TELEGRAM_SPLIT_MB", "45")) * 1024 * 1024)

_legacy = os.environ.get("CHAPTER_THROTTLE_SECONDS")
if _legacy is not None:
    _mid = max(0.0, float(_legacy))
    CHAPTER_THROTTLE_MIN = max(0.0, _mid * 0.75)
    CHAPTER_THROTTLE_MAX = max(CHAPTER_THROTTLE_MIN, _mid * 1.25)
else:
    CHAPTER_THROTTLE_MIN = max(
        0.0, float(os.environ.get("CHAPTER_THROTTLE_MIN", "18"))
    )
    CHAPTER_THROTTLE_MAX = max(
        CHAPTER_THROTTLE_MIN,
        float(os.environ.get("CHAPTER_THROTTLE_MAX", "24")),
    )


def next_chapter_throttle() -> float:
    if CHAPTER_THROTTLE_MAX <= 0:
        return 0.0
    if CHAPTER_THROTTLE_MAX <= CHAPTER_THROTTLE_MIN:
        return CHAPTER_THROTTLE_MIN
    return random.uniform(CHAPTER_THROTTLE_MIN, CHAPTER_THROTTLE_MAX)


# 0 or empty = no chapter cap (full novel / any range). Same idea as leaving
# ALLOWED_USER_IDS empty for a private bot.
_raw_cap = (os.environ.get("CHAPTER_CAP") or "0").strip()
DEFAULT_CHAPTER_CAP = int(_raw_cap) if _raw_cap.isdigit() else 0
DAILY_TASK_LIMIT = int(os.environ.get("DAILY_TASK_LIMIT", "0") or "0")

# 1/true = no Chrome window (Chrome new headless). 0/false = visible window.
# Default when HEADLESS is not set: headed/visible (0) so admin can solve
# Cloudflare Turnstile over VNC. Auto-click is disabled on all platforms.
_TRUE_WORDS = {"1", "true", "yes", "y", "on", "enable", "enabled"}
_FALSE_WORDS = {"0", "false", "no", "n", "off", "disable", "disabled"}


def _env_flag(name: str, default: str = "0") -> bool:
    """
    On/off setting. yes / 1 / true / on  = on;  no / 0 / false / off = off.
    Unset or blank uses the default; an unrecognised word also uses the default.
    """
    raw = (os.environ.get(name) or "").strip().lower()
    if raw in _TRUE_WORDS:
        return True
    if raw in _FALSE_WORDS:
        return False
    if raw:
        print(f"[CONFIG] {name}={raw!r} is not yes/no — using the default ({default}).")
    return default.strip().lower() in _TRUE_WORDS


def _env_minutes(name: str, default: int) -> int:
    """
    Minutes setting where blank / 0 / no / off / none / null / never means
    "no limit" (returns 0). Unset uses the default.
    """
    raw = os.environ.get(name)
    if raw is None:
        return default
    raw = raw.strip().lower()
    if raw in ("", "0", "no", "n", "off", "none", "null", "never", "false", "unlimited"):
        return 0
    try:
        return max(0, int(float(raw)))
    except ValueError:
        print(f"[CONFIG] {name}={raw!r} is not a number — using {default}.")
        return default


HEADLESS = _env_flag("HEADLESS", "0")

# Multi-worker shared task DB (Northflank). Password only from .env.
DB_HOST = "primary.final-lightnovel-bot-task-queue--87srfx6hrv5l.addon.code.run"
DB_PORT = 29301
DB_NAME = "_4f9e4d342580"
DB_USER = "_c676b0c0215f5654"
DB_PASSWORD = (os.environ.get("DB_PASSWORD") or "").strip()
# Prefer SERVER_IDn so this bot can share .env with another that owns SERVER_ID.
# No default coordinator id: unset → random worker id so two hosts never both
# poll Telegram by accident. Explicitly set SERVER_IDn=s4 on the coordinator only.
_raw_sid = (os.environ.get("SERVER_IDn") or os.environ.get("SERVER_ID") or "").strip()
if _raw_sid:
    SERVER_ID = _raw_sid
else:
    SERVER_ID = f"w-{secrets.token_hex(3)}"
    print(
        f"[CONFIG] SERVER_IDn not set — using random worker id {SERVER_ID} "
        "(set SERVER_IDn=s4 only on the coordinator that should poll Telegram)."
    )
QUEUE_POOL = (os.environ.get("QUEUE_POOL") or "both").strip().lower()
if QUEUE_POOL not in ("normal", "premium", "both"):
    QUEUE_POOL = "both"
# Only explicit s4 (or coord/coordinator) is the Telegram coordinator.
IS_COORDINATOR = SERVER_ID.lower() in ("s4", "coord", "coordinator")
WORKER_ONLY = _env_flag("WORKER_ONLY", "0" if IS_COORDINATOR else "1")
PREMIUM_PLAN_ID = "1000_priority"
PREMIUM_CHAPTERS_PER_DAY = 1000
PREMIUM_TASKS_PER_DAY = int(os.environ.get("PREMIUM_TASKS_PER_DAY", "3") or "3")  # per rolling 24h
PREMIUM_PRICE_USD = 1
# Telegram Stars (XTR): exact price required; change here or via env.
PREMIUM_STARS_PRICE = int(os.environ.get("PREMIUM_STARS_PRICE", "80") or "80")
PREMIUM_STARS_WEEKS = 1
STARS_ORDER_TTL_MINUTES = int(os.environ.get("STARS_ORDER_TTL_MINUTES", "60") or "60")
PREMIUM_BLURB = (
    f"⭐ <b>Premium</b> ({PREMIUM_STARS_PRICE} Stars or $1/week): "
    f"{PREMIUM_CHAPTERS_PER_DAY} chapters/day, "
    f"{PREMIUM_TASKS_PER_DAY} tasks/day, priority queue, Auto-Continue. /premium"
)
REFUND_POLICY_SHORT = (
    "↩️ Refunds are only for accidental purchases, and only if you have not "
    "used Premium. Queueing any task while Premium is active counts as use "
    "(it goes through the priority queue), even if you stay within the "
    "normal limits."
)
PREMIUM_PAY_INFO = (os.environ.get("PREMIUM_PAY_INFO") or "").strip() or (
    f"Pay <b>{PREMIUM_STARS_PRICE} Stars</b> for automatic activation, or <b>$1 / week</b> via PayPal:\n"
    "• Up to <b>1000</b> fresh chapters per day\n"
    f"• Up to <b>{PREMIUM_TASKS_PER_DAY}</b> tasks per day\n"
    "• <b>Priority</b> in the queue\n"
    "• <b>Auto-Continue</b> when your daily limit resets (within your range)\n\n"
    "Stars payments activate automatically. For PayPal, tap <b>I've paid</b> and wait for admin confirmation."
)
USE_SHARED_TASKS = bool(DB_PASSWORD)
CHAPTER_DATABASE_URL = (os.environ.get("CHAPTER_DATABASE_URL") or os.environ.get("DATABASE_URL") or "").strip()



# One-time browser link (noVNC over a cloudflared quick tunnel) so the USER whose
# download is blocked can tick the Cloudflare checkbox themselves. Needs a VNC
# server showing the Chrome window, websockify + noVNC, and cloudflared.
REMOTE_SOLVE = _env_flag("REMOTE_SOLVE", "1")
VNC_PORT = int(os.environ.get("VNC_PORT", "5901") or "5901")
NOVNC_PORT = int(os.environ.get("NOVNC_PORT", "6080") or "6080")
CLOUDFLARED_BIN = os.environ.get("CLOUDFLARED_BIN", "cloudflared").strip() or "cloudflared"
# Blank / 0 / no / none = the link never expires (waits until /solved or the task ends).
REMOTE_LINK_MAX_MINUTES = _env_minutes("REMOTE_LINK_MAX_MINUTES", 20)


def _find_novnc_dir() -> str:
    configured = (os.environ.get("NOVNC_WEB_DIR") or "").strip()
    if configured:
        return configured
    for candidate in (
        "/usr/share/novnc",
        "/usr/share/webapps/novnc",
        str(Path.home() / "noVNC"),
        str(Path(__file__).resolve().parent / "noVNC"),
    ):
        if (Path(candidate) / "vnc.html").is_file():
            return candidate
    return "/usr/share/novnc"


NOVNC_WEB_DIR = _find_novnc_dir()
# Log refreshes, key combos, clicks, new tabs and DevTools while a remote link is
# open. Uses page scripts + DevTools protocol, which Cloudflare may notice; set
# ACTIVITY_DEEP=0 to log only tabs, URL changes and login-screen detection.
ACTIVITY_DEEP = _env_flag("ACTIVITY_DEEP", "1")
# Optional: open Chrome in --app mode (no tab strip / address bar).
CHROME_APP_MODE = _env_flag("CHROME_APP_MODE", "0")

# Extra Chrome flags. Headed mode keeps images/WebGL so Turnstile/captcha can work.
# Headless stays lighter. Do not disable images/webgl when HEADLESS=0.
_HEADED_CHROME_ARGS = ",".join(
    [
        "--disable-gpu",
        "--no-sandbox",
        "--disable-dev-shm-usage",
        "--ozone-platform=x11",
        "--mute-audio",
        "--no-first-run",
        "--no-default-browser-check",
    ]
)
_HEADLESS_CHROME_ARGS = ",".join(
    [
        "--disable-gpu",
        "--no-sandbox",
        "--disable-dev-shm-usage",
        "--mute-audio",
        "--no-first-run",
        "--no-default-browser-check",
        "--renderer-process-limit=2",
    ]
)
if CHROME_APP_MODE and not HEADLESS:
    _HEADED_CHROME_ARGS += ",--app=https://wtr-lab.com/en"
LOW_RAM_CHROME_ARGS = (
    _HEADLESS_CHROME_ARGS if HEADLESS else _HEADED_CHROME_ARGS
)

WTR_HOSTS = {"wtr-lab.com", "www.wtr-lab.com"}

_last_handler_alert = 0.0


class _LoggingExceptionHandler(telebot.ExceptionHandler):
    """Log every exception raised inside a command/callback handler."""

    def handle(self, exception):
        global _last_handler_alert
        log.error(
            "Handler exception: %s: %s",
            type(exception).__name__,
            exception,
            exc_info=(type(exception), exception, exception.__traceback__),
        )
        # Alert admin at most once a minute so a broken handler can't spam.
        if time.time() - _last_handler_alert > 60:
            _last_handler_alert = time.time()
            try:
                notify_admin(
                    "⚠️ <b>Handler error</b>\n"
                    f"<code>{html.escape(type(exception).__name__)}: "
                    f"{html.escape(str(exception))[:400]}</code>\n"
                    "Full traceback is in bot.log."
                )
            except Exception:
                pass
        return True  # handled: keep polling instead of restarting


bot = telebot.TeleBot(
    BOT_TOKEN, threaded=True, exception_handler=_LoggingExceptionHandler()
)


def _log_updates(messages):
    """Log every incoming text message / command (who, where, what)."""
    for m in messages:
        try:
            user = getattr(m, "from_user", None)
            uid = user.id if user else "?"
            uname = f"@{user.username}" if user and user.username else "-"
            ctype = getattr(m, "content_type", "?")
            text = (getattr(m, "text", None) or "").replace("\n", " ")[:300]
            # Never write magic links / login emails into the log.
            st = pending_download.get(uid) if isinstance(uid, int) else None
            if str((st or {}).get("step", "")).startswith("login") or (
                "http" in text and "wtr-lab.com" not in text
            ):
                text = "<hidden: login step or link>"
            log.info(
                "[IN] user=%s %s chat=%s type=%s text=%r",
                uid, uname, getattr(getattr(m, "chat", None), "id", "?"), ctype, text,
            )
        except Exception as error:
            log.warning("[IN] could not log message: %s", error)


bot.set_update_listener(_log_updates)

_orig_process_callbacks = bot.process_new_callback_query


def _log_callbacks(callbacks):
    for c in callbacks:
        try:
            log.info(
                "[IN] callback user=%s data=%r",
                getattr(c.from_user, "id", "?"), getattr(c, "data", None),
            )
        except Exception:
            pass
    return _orig_process_callbacks(callbacks)


bot.process_new_callback_query = _log_callbacks

DATA_DIR.mkdir(parents=True, exist_ok=True)
LIBRARY_DIR.mkdir(parents=True, exist_ok=True)
CHROME_PROFILE_DIR.mkdir(parents=True, exist_ok=True)

stop_event = threading.Event()
pending_download: dict[int, dict[str, Any]] = {}
login_lock = threading.Lock()
current_login_user: Optional[int] = None

# Chat multi-step sessions (/download, /login, /set confirm) expire after this.
SESSION_SECONDS = 10 * 60
# Admin /pause|/stop: task stays "paused" then auto-returns to pending.
PAUSE_REPEND_SECONDS = 2 * 60
# /set KEY value waits for yes/no in pending_download step "set_confirm".

# Manual Turnstile wait: anyone may send /solved to resume immediately.
# Auto-recheck interval is hardcoded at 10 minutes (no .env).
TURNSTILE_RECHECK_SECONDS = 10 * 60
solved_event = threading.Event()
# Populated while the crawler is blocked on a captcha (single worker).
captcha_wait: dict[str, Any] = {}

# Logged-out handling for the shared WTR-Lab account in the Chrome profile.
# The worker thread is the ONLY thread that drives Chrome: /login requests are
# queued here and executed by the worker (a second Chrome on the same profile
# kills the running one).
LOGIN_RECHECK_SECONDS = 120
# Idle servers re-check their WTR-Lab login this often so /login, /servers and
# /status stay fresh even when no task is running (0 = only check at startup
# and before each task).
LOGIN_IDLE_CHECK_SECONDS = int(os.environ.get("LOGIN_IDLE_CHECK_SECONDS", "1800"))
login_wait: dict[str, Any] = {}      # set while the worker waits for a re-login
session_state = "unknown"            # "unknown" | "ok" | "logged_out"
login_requests: "queue.Queue[dict[str, Any]]" = queue.Queue()
# Magic links forwarded from the coordinator's Telegram chat to THIS server
# through the shared command table (user_id -> link). Never logged or stored.
remote_magic_links: dict[int, str] = {}


def is_wtr_url(url: str) -> bool:
    try:
        host = (urllib.parse.urlparse(url).hostname or "").lower()
    except Exception:
        return False
    return host in WTR_HOSTS


def user_allowed(user_id: int) -> bool:
    if not is_admin(user_id) and get_ban(user_id):
        return False
    if not ALLOWED_USER_IDS:
        return True
    return user_id in ALLOWED_USER_IDS


def is_admin(user_id: int) -> bool:
    """Admins skip chapter cap and daily task limits."""
    return user_id in ADMIN_USER_IDS


def notify_admin(text: str, reply_markup=None):
    """Send a message to ADMIN_CHAT_ID if configured."""
    if not ADMIN_CHAT_ID:
        return
    try:
        bot.send_message(
            ADMIN_CHAT_ID,
            text,
            parse_mode="HTML",
            link_preview_options=LINK_PREVIEW_OFF,
            reply_markup=reply_markup,
        )
    except Exception as e:
        print(f"[ADMIN NOTIFY ERROR] {e}")


def is_routine_failure(reason: str) -> bool:
    """Limits / expected stops — do not ping admin with screenshots for these."""
    text = (reason or "").lower()
    markers = (
        "daily chapter limit",
        "chapter limit",
        "pull cap",
        "ai-locked",
        "ai-lock",
        "no unlocked chapters",
        "cancelled",
        "nothing to cancel",
    )
    return any(m in text for m in markers)


def capture_browser_screenshot(browser: Any, tag: str = "error") -> Optional[Path]:
    """Save a PNG of the current Chrome page for error reports."""
    try:
        if browser is None or not getattr(browser, "driver", None):
            return None
        debug_dir = DATA_DIR / "debug"
        debug_dir.mkdir(parents=True, exist_ok=True)
        safe = re.sub(r"[^\w.-]+", "_", str(tag))[:40]
        path = debug_dir / f"shot-{int(time.time())}-{safe}.png"
        browser.driver.save_screenshot(str(path))
        if path.is_file() and path.stat().st_size > 0:
            return path
    except Exception as error:
        print(f"[SCREENSHOT] {type(error).__name__}: {error}")
    return None


def send_error_screenshot(
    browser: Any,
    chat_id,
    caption: str,
    *,
    user_id: Optional[int] = None,
    task_id: Optional[int] = None,
    to_admin: bool = True,
    open_url: Optional[str] = None,
) -> bool:
    """
    Optionally open `open_url` first (the chapter/page that failed), then
    screenshot Chrome and send it to the user (and admin when useful).
    """
    if open_url and browser is not None and getattr(browser, "driver", None):
        try:
            print(f"[SCREENSHOT] Opening error page: {open_url}")
            browser.open(open_url)
            time.sleep(2.5)
        except Exception as error:
            print(f"[SCREENSHOT] could not open error page: {error}")
    path = capture_browser_screenshot(browser, tag=f"t{task_id or 0}")
    caption = (caption or "⚠️ Download error")[:1024]
    sent = False
    if path is not None:
        try:
            with open(path, "rb") as photo:
                msg = bot.send_photo(chat_id, photo, caption=caption)
            track_and_autodelete(msg, user_id, DELETE_AFTER_NOTICE)
            sent = True
        except Exception as error:
            print(f"[SCREENSHOT SEND] user: {error}")
        if to_admin and ADMIN_CHAT_ID and str(ADMIN_CHAT_ID) != str(chat_id):
            admin_caption = caption
            if task_id is not None:
                admin_caption = f"Task #{task_id} · user {user_id}\n{caption}"
            admin_caption = admin_caption[:1024]
            try:
                with open(path, "rb") as photo:
                    bot.send_photo(ADMIN_CHAT_ID, photo, caption=admin_caption)
            except Exception as error:
                print(f"[SCREENSHOT SEND] admin: {error}")
                try:
                    notify_admin(html.escape(admin_caption))
                except Exception:
                    pass
    return sent

# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------

class TaskCancelled(Exception):
    pass


class WtrError(Exception):
    pass


class ManualChallengeRequired(WtrError):
    pass


class LoginRequired(WtrError):
    pass


class ChapterLocked(WtrError):
    def __init__(self, chapter_no: int):
        self.chapter_no = chapter_no
        super().__init__(f"Chapter {chapter_no} is AI-locked on WTR-Lab")


class DeadBrowser(WtrError):
    pass


DEAD_SESSION_MARKERS = (
    "invalid session id",
    "no such window",
    "target window already closed",
    "not connected to devtools",
    "web view not found",
    "chrome not reachable",
    "disconnected",
)


def is_dead_session(error: BaseException) -> bool:
    text = f"{type(error).__name__}: {error}".lower()
    return any(marker in text for marker in DEAD_SESSION_MARKERS)


def user_facing_error(error: BaseException) -> str:
    if isinstance(error, ChapterLocked):
        return (
            f"Stopped at chapter {error.chapter_no}: later chapters are still "
            "AI-locked on WTR-Lab (paywall / unlock bar)."
        )
    if isinstance(error, ManualChallengeRequired):
        return (
            "Cloudflare Turnstile challenge detected. "
            "Waiting for admin to clear it in Chrome (or /solved)."
        )
    if isinstance(error, LoginRequired):
        return "WTR-Lab requires login. Use /login or reply with your email when prompted."
    if is_dead_session(error) or isinstance(error, DeadBrowser):
        return "Chrome closed or the browser session died. The worker will reopen Chrome and retry."
    err_text = str(error).lower()
    if (
        isinstance(error, TimeoutException)
        or "script timeout" in err_text
        or "origin timeout" in err_text
        or "522" in err_text
    ):
        return (
            "WTR-Lab origin timed out (Cloudflare 522 / path blip). "
            "Cached chapters are kept — resend the same link or /continue after a pause."
        )
    if "__next_data__" in err_text:
        return (
            "WTR-Lab returned an empty or blocked page (no page data). "
            "Often means the shared account is logged out or Cloudflare is up — "
            "try /login, or solve the captcha and send /solved."
        )
    if "waiting for crawling" in err_text or "check back later" in err_text:
        detail = str(error).strip()
        return (
            f"{detail}\n"
            "This chapter is not ready on WTR-Lab yet — try again later "
            "with /continue (other chapters may still download)."
        )[:400]
    if "unknown wtr-lab response" in err_text:
        # Keep any HTTP/status detail already in the message
        detail = str(error).strip()
        if detail and detail.lower() != "unknown wtr-lab response":
            return detail[:400]
        return (
            "WTR-Lab returned an empty or unexpected chapter response. "
            "Try /continue later; if it keeps happening, the account may need /login."
        )
    if isinstance(error, WtrError):
        return str(error)[:400]
    return f"The download failed: {type(error).__name__}: {str(error)[:200]}"


def novel_status_label(value: Any) -> str:
    # WTR-Lab serie_data.status: 0 Ongoing, 1 Completed, 2 Hiatus, 3 Dropped
    mapping = {
        0: "Ongoing",
        1: "Completed",
        2: "Hiatus",
        3: "Dropped",
        "0": "Ongoing",
        "1": "Completed",
        "2": "Hiatus",
        "3": "Dropped",
        "ongoing": "Ongoing",
        "completed": "Completed",
        "complete": "Completed",
        "finished": "Completed",
        "hiatus": "Hiatus",
        "dropped": "Dropped",
    }
    if isinstance(value, str):
        key = value.strip().lower()
        return mapping.get(key, mapping.get(value, value.strip() or "Unknown"))
    return mapping.get(value, str(value if value is not None else "Unknown"))


def looks_mostly_cjk(text: str) -> bool:
    if not text:
        return False
    cjk = sum(1 for ch in text if "\u4e00" <= ch <= "\u9fff")
    letters = sum(1 for ch in text if ch.isalpha() or ("\u4e00" <= ch <= "\u9fff"))
    return letters > 0 and (cjk / letters) >= 0.4



# ---------------------------------------------------------------------------
# Local SQLite task queue (replaces shared Postgres)
# ---------------------------------------------------------------------------

def local_db() -> sqlite3.Connection:
    conn = sqlite3.connect(SQLITE_PATH, timeout=60)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA busy_timeout=30000;")
    return conn


def _psycopg2():
    try:
        import psycopg2
        return psycopg2
    except ImportError as e:
        raise SystemExit(
            "psycopg2-binary required when DB_PASSWORD is set. "
            "pip install psycopg2-binary"
        ) from e


def shared_db():
    pg = _psycopg2()
    return pg.connect(
        host=DB_HOST,
        port=int(DB_PORT),
        dbname=DB_NAME,
        user=DB_USER,
        password=DB_PASSWORD,
        connect_timeout=15,
        sslmode=os.environ.get("DB_SSLMODE", "require"),
    )


def setup_shared_tasks_db() -> None:
    if not USE_SHARED_TASKS:
        return
    conn = shared_db()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS wtrlab_tasks (
                    id BIGSERIAL PRIMARY KEY,
                    chat_id BIGINT NOT NULL,
                    user_id BIGINT NOT NULL,
                    username TEXT,
                    first_name TEXT,
                    last_name TEXT,
                    url TEXT NOT NULL,
                    chapter_range TEXT NOT NULL DEFAULT 'all',
                    status TEXT NOT NULL DEFAULT 'pending',
                    queue TEXT NOT NULL DEFAULT 'normal',
                    priority INT NOT NULL DEFAULT 0,
                    auto_continue BOOLEAN NOT NULL DEFAULT FALSE,
                    novel_title TEXT,
                    total_chapters INT,
                    error TEXT,
                    server_id TEXT,
                    run_id TEXT,
                    run_next INT NOT NULL DEFAULT 0,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    completed_at TIMESTAMPTZ,
                    last_progress_at TIMESTAMPTZ,
                    paused_until TIMESTAMPTZ
                );
                CREATE INDEX IF NOT EXISTS wtrlab_tasks_claim_idx
                    ON wtrlab_tasks (status, queue, priority DESC, id ASC);
                CREATE TABLE IF NOT EXISTS wtrlab_premium (
                    user_id BIGINT PRIMARY KEY,
                    plan TEXT NOT NULL DEFAULT '1000_priority',
                    expires_at TIMESTAMPTZ NOT NULL,
                    granted_by BIGINT,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                );
                CREATE TABLE IF NOT EXISTS wtrlab_servers (
                    server_id TEXT PRIMARY KEY,
                    queue_pool TEXT,
                    last_seen TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    current_task_id BIGINT,
                    note TEXT
                );
                CREATE TABLE IF NOT EXISTS wtrlab_email_locks (
                    email TEXT PRIMARY KEY,
                    server_id TEXT NOT NULL,
                    user_id BIGINT,
                    locked_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                );
                CREATE TABLE IF NOT EXISTS wtrlab_deliveries (
                    id BIGSERIAL PRIMARY KEY,
                    user_id BIGINT,
                    chat_id BIGINT,
                    novel_id TEXT,
                    url TEXT NOT NULL,
                    chapter_range TEXT,
                    server_id TEXT,
                    queue TEXT,
                    task_id BIGINT,
                    epub_name TEXT,
                    delivered_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                );
                CREATE INDEX IF NOT EXISTS wtrlab_deliveries_novel_idx
                    ON wtrlab_deliveries (novel_id, delivered_at DESC);
                CREATE TABLE IF NOT EXISTS wtrlab_auto_continue (
                    id BIGSERIAL PRIMARY KEY,
                    user_id BIGINT NOT NULL,
                    chat_id BIGINT NOT NULL,
                    url TEXT NOT NULL,
                    chapter_range TEXT NOT NULL DEFAULT 'all',
                    username TEXT,
                    first_name TEXT,
                    last_name TEXT,
                    runs_done INT NOT NULL DEFAULT 0,
                    max_runs INT NOT NULL DEFAULT 2,
                    enabled BOOLEAN NOT NULL DEFAULT TRUE,
                    last_task_id BIGINT,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                );
                CREATE INDEX IF NOT EXISTS wtrlab_auto_user_idx
                    ON wtrlab_auto_continue (user_id, enabled);
                CREATE TABLE IF NOT EXISTS wtrlab_commands (
                    id BIGSERIAL PRIMARY KEY,
                    target_server TEXT NOT NULL,
                    command TEXT NOT NULL,
                    payload TEXT,
                    created_by BIGINT,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    claimed_at TIMESTAMPTZ,
                    done_at TIMESTAMPTZ,
                    result TEXT
                );
                CREATE INDEX IF NOT EXISTS wtrlab_commands_pending_idx
                    ON wtrlab_commands (target_server, done_at, id);
                CREATE TABLE IF NOT EXISTS wtrlab_config (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    updated_by BIGINT
                );
                CREATE TABLE IF NOT EXISTS wtrlab_login_status (
                    server_id TEXT PRIMARY KEY,
                    status TEXT NOT NULL DEFAULT 'unknown',
                    detail TEXT,
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                );
                ALTER TABLE wtrlab_premium ADD COLUMN IF NOT EXISTS last_method TEXT;
                ALTER TABLE wtrlab_login_status ADD COLUMN IF NOT EXISTS last_check TIMESTAMPTZ;
                ALTER TABLE wtrlab_login_status ADD COLUMN IF NOT EXISTS since TIMESTAMPTZ;
                ALTER TABLE wtrlab_tasks ADD COLUMN IF NOT EXISTS novel_key TEXT;
                CREATE INDEX IF NOT EXISTS wtrlab_tasks_novel_idx
                    ON wtrlab_tasks (novel_key, status);
                UPDATE wtrlab_tasks
                   SET novel_key = substring(url from '/novel/([0-9]+)')
                 WHERE novel_key IS NULL
                   AND status IN ('pending', 'running', 'paused');
                CREATE TABLE IF NOT EXISTS wtrlab_star_orders (
                    order_id TEXT PRIMARY KEY,
                    user_id BIGINT NOT NULL,
                    plan TEXT NOT NULL,
                    weeks INT NOT NULL DEFAULT 1,
                    stars_amount INT NOT NULL,
                    currency TEXT NOT NULL DEFAULT 'XTR',
                    status TEXT NOT NULL DEFAULT 'pending',
                    invoice_message_id BIGINT,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    expires_at TIMESTAMPTZ NOT NULL,
                    paid_at TIMESTAMPTZ
                );
                CREATE INDEX IF NOT EXISTS wtrlab_star_orders_user_idx
                    ON wtrlab_star_orders (user_id, status);
                CREATE TABLE IF NOT EXISTS wtrlab_star_payments (
                    id BIGSERIAL PRIMARY KEY,
                    order_id TEXT UNIQUE,
                    user_id BIGINT NOT NULL,
                    telegram_username TEXT,
                    telegram_payment_charge_id TEXT NOT NULL UNIQUE,
                    provider_payment_charge_id TEXT,
                    currency TEXT NOT NULL,
                    amount INT NOT NULL,
                    plan TEXT,
                    weeks INT NOT NULL DEFAULT 1,
                    status TEXT NOT NULL DEFAULT 'paid',
                    paid_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    premium_started_at TIMESTAMPTZ,
                    premium_expires_at TIMESTAMPTZ,
                    refunded_at TIMESTAMPTZ,
                    refunded_by BIGINT,
                    refund_reason TEXT,
                    note TEXT
                );
                CREATE INDEX IF NOT EXISTS wtrlab_star_payments_user_idx
                    ON wtrlab_star_payments (user_id, id DESC);
                CREATE TABLE IF NOT EXISTS wtrlab_star_entitlements (
                    id BIGSERIAL PRIMARY KEY,
                    payment_id BIGINT NOT NULL UNIQUE REFERENCES wtrlab_star_payments(id),
                    user_id BIGINT NOT NULL,
                    starts_at TIMESTAMPTZ NOT NULL,
                    ends_at TIMESTAMPTZ NOT NULL,
                    status TEXT NOT NULL DEFAULT 'active'
                );
            """)
        conn.commit()
        conn.commit()
        print(f"[SHARED DB] ready SERVER_ID={SERVER_ID} QUEUE_POOL={QUEUE_POOL}")
    finally:
        conn.close()


def touch_task_progress(task_id) -> None:
    """Bump last_progress_at so reaper does not steal long crawls."""
    if not task_id:
        return
    if USE_SHARED_TASKS:
        try:
            conn = shared_db()
            try:
                with conn.cursor() as cur:
                    cur.execute(
                        "UPDATE wtrlab_tasks SET last_progress_at = NOW() WHERE id = %s",
                        (task_id,),
                    )
                conn.commit()
            finally:
                conn.close()
        except Exception as e:
            print(f"[TOUCH PROGRESS] {e}")
        return
    try:
        conn = local_db()
        try:
            conn.execute(
                "UPDATE tasks SET last_progress_at = datetime('now') WHERE id = ?",
                (task_id,),
            )
            conn.commit()
        finally:
            conn.close()
    except Exception as e:
        print(f"[TOUCH PROGRESS] {e}")


def heartbeat_once(current_task_id=None) -> None:

    if not USE_SHARED_TASKS:
        return
    try:
        conn = shared_db()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO wtrlab_servers (server_id, queue_pool, last_seen, current_task_id)
                    VALUES (%s, %s, NOW(), %s)
                    ON CONFLICT (server_id) DO UPDATE SET
                        queue_pool = EXCLUDED.queue_pool,
                        last_seen = NOW(),
                        current_task_id = EXCLUDED.current_task_id
                    """,
                    (SERVER_ID, QUEUE_POOL, current_task_id),
                )
            conn.commit()
        finally:
            conn.close()
    except Exception as e:
        print(f"[HEARTBEAT] {e}")


# Background heartbeat: keeps "seen" fresh while a long task is running
# (EPUB build, upload, Cloudflare wait, throttle sleeps, slow chapters).
_HB_STATE: dict = {"task_id": None, "started": False}


def start_heartbeat_thread() -> None:
    if not USE_SHARED_TASKS or _HB_STATE["started"]:
        return
    _HB_STATE["started"] = True

    def _loop():
        while not stop_event.is_set():
            try:
                heartbeat_once(_HB_STATE.get("task_id"))
            except Exception as e:
                print(f"[HEARTBEAT THREAD] {e}")
            stop_event.wait(max(5, HEARTBEAT_THREAD_SECONDS))

    threading.Thread(target=_loop, name="wtr-heartbeat", daemon=True).start()


COMMAND_POLL_SECONDS = int(os.environ.get("COMMAND_POLL_SECONDS", "5"))
_CMD_STATE = {"started": False}


def start_command_thread() -> None:
    """
    Poll the shared command table for commands aimed at this server
    (/login <server>, /set, /restart, reload_config). Runs in its own thread
    so it works while the crawler is busy or waiting for a login.
    """
    if not USE_SHARED_TASKS or _CMD_STATE["started"]:
        return
    _CMD_STATE["started"] = True

    def _loop():
        while not stop_event.is_set():
            try:
                drain_server_commands()
            except Exception as error:
                print(f"[CMD THREAD] {error}")
            stop_event.wait(max(2, COMMAND_POLL_SECONDS))

    threading.Thread(target=_loop, name="wtr-commands", daemon=True).start()


def enqueue_server_command(target_server: str, command: str, payload: str = "", created_by=None) -> Optional[int]:
    if not USE_SHARED_TASKS:
        return None
    target_server = (target_server or "").strip().lower()
    try:
        conn = shared_db()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO wtrlab_commands (target_server, command, payload, created_by)
                    VALUES (%s, %s, %s, %s) RETURNING id
                    """,
                    (target_server, command, payload or "", created_by),
                )
                row = cur.fetchone()
            conn.commit()
            return int(row[0]) if row else None
        finally:
            conn.close()
    except Exception as e:
        print(f"[CMD BUS] enqueue {e}")
        return None


def drain_server_commands() -> int:
    """Workers/coordinator pick pending commands aimed at this SERVER_ID or 'all'."""
    if not USE_SHARED_TASKS:
        return 0
    n = 0
    try:
        conn = shared_db()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE wtrlab_commands
                    SET claimed_at = NOW()
                    WHERE id IN (
                        SELECT id FROM wtrlab_commands
                        WHERE done_at IS NULL
                          AND claimed_at IS NULL
                          AND created_at > NOW() - INTERVAL '15 minutes'
                          AND (target_server = %s OR target_server = 'all')
                        ORDER BY id ASC
                        FOR UPDATE SKIP LOCKED
                        LIMIT 5
                    )
                    RETURNING id, command, payload
                    """,
                    (SERVER_ID.lower(),),
                )
                rows = cur.fetchall()
            for cid, command, payload in rows:
                result = "ok"
                try:
                    if command == "restart":
                        helper = ensure_restart_helper()
                        log_path = BASE_DIR / "restart-bot.log"
                        subprocess.Popen(
                            ["bash", str(helper)],
                            cwd=str(BASE_DIR),
                            stdout=open(log_path, "a", encoding="utf-8"),
                            stderr=subprocess.STDOUT,
                            start_new_session=True,
                        )
                        def _exit_later():
                            time.sleep(5)
                            os._exit(0)
                        threading.Thread(target=_exit_later, daemon=True).start()
                        result = "restart_scheduled"
                    elif command == "set":
                        # payload: KEY=VALUE
                        if payload and "=" in payload:
                            key, val = payload.split("=", 1)
                            key = key.strip().upper()
                            apply_config_live(key, val.strip())
                            # persist .env
                            try:
                                write_dotenv_key(key, val.strip())
                            except Exception:
                                pass
                            result = f"set {key}"
                        else:
                            result = "bad_payload"
                    elif command == "reload_config":
                        load_shared_config()
                        result = "reloaded"
                    elif command == "login_start":
                        data = json.loads(payload or "{}")
                        login_requests.put({
                            "chat_id": int(data["chat_id"]),
                            "user_id": int(data["user_id"]),
                            "email": str(data["email"]),
                        })
                        log.info("[LOGIN] login requested on %s by user %s",
                                 SERVER_ID, data.get("user_id"))
                        result = "login_queued"
                    elif command == "login_link":
                        data = json.loads(payload or "{}")
                        remote_magic_links[int(data["user_id"])] = str(data["url"])
                        result = "link_received"
                    else:
                        result = f"unknown:{command}"
                except Exception as e:
                    result = f"err:{e}"
                with conn.cursor() as cur:
                    # A magic link is a credential: never keep it in the table.
                    cur.execute(
                        "UPDATE wtrlab_commands SET done_at = NOW(), result = %s, "
                        "payload = CASE WHEN command = 'login_link' THEN '' "
                        "ELSE payload END WHERE id = %s",
                        (result[:500], cid),
                    )
                n += 1
            conn.commit()
        finally:
            conn.close()
    except Exception as e:
        print(f"[CMD BUS] drain {e}")
    return n



def set_login_status(status: str, detail: str = "", server_id: str = None) -> None:
    """status: logged_in | logged_out | ip_blocked | unknown"""
    if not USE_SHARED_TASKS:
        return
    sid = (server_id or SERVER_ID or "").strip()
    if not sid:
        return
    try:
        conn = shared_db()
        try:
            with conn.cursor() as cur:
                # last_check = every check; since = when the status last CHANGED.
                cur.execute(
                    """
                    INSERT INTO wtrlab_login_status
                        (server_id, status, detail, updated_at, last_check, since)
                    VALUES (%s, %s, %s, NOW(), NOW(), NOW())
                    ON CONFLICT (server_id) DO UPDATE SET
                        since = CASE
                            WHEN wtrlab_login_status.status IS DISTINCT FROM EXCLUDED.status
                                THEN NOW()
                            ELSE COALESCE(wtrlab_login_status.since, NOW())
                        END,
                        status = EXCLUDED.status,
                        detail = EXCLUDED.detail,
                        updated_at = NOW(),
                        last_check = NOW()
                    """,
                    (sid, status, (detail or "")[:500]),
                )
            conn.commit()
        finally:
            conn.close()
    except Exception as e:
        print(f"[LOGIN STATUS] {e}")


def list_login_statuses() -> list:
    if not USE_SHARED_TASKS:
        return []
    try:
        conn = shared_db()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT server_id, status, detail, updated_at FROM wtrlab_login_status ORDER BY server_id"
                )
                return cur.fetchall() or []
        finally:
            conn.close()
    except Exception as e:
        print(f"[LOGIN STATUS LIST] {e}")
        return []


def servers_needing_login() -> list:
    """Servers reported logged_out (or unknown never seen as logged_in)."""
    rows = list_login_statuses()
    need = []
    for row in rows:
        sid, st = row[0], (row[1] or "").lower()
        if st in ("logged_out", "unknown"):
            need.append(sid)
        elif st == "ip_blocked":
            need.append(sid)  # shown separately in UI
    return need


LOGIN_LABELS = {
    "logged_in": "✅ logged in",
    "logged_out": "❌ logged out",
    "ip_blocked": "🚫 IP blocked",
    "unknown": "❓ not checked yet",
}


def _as_utc(ts):
    import datetime as _dt
    if ts is None:
        return None
    if getattr(ts, "tzinfo", None) is None:
        ts = ts.replace(tzinfo=_dt.timezone.utc)
    return ts


def fmt_ago(ts) -> str:
    """'12s ago', '5m ago', '3h ago', '2d ago' (or '—' when unknown)."""
    import datetime as _dt
    ts = _as_utc(ts)
    if ts is None:
        return "—"
    secs = max(0, int((_dt.datetime.now(_dt.timezone.utc) - ts).total_seconds()))
    if secs < 60:
        return f"{secs}s ago"
    if secs < 3600:
        return f"{secs // 60}m ago"
    if secs < 86400:
        return f"{secs // 3600}h ago"
    return f"{secs // 86400}d ago"


def _server_sort_key(sid: str):
    m = re.match(r"^(\D*)(\d+)$", str(sid or ""))
    return (m.group(1), int(m.group(2))) if m else (str(sid), 0)


def fleet_overview() -> list:
    """
    Every known server with heartbeat + login state, as dicts:
    server_id, pool, last_seen, online, task_id, login, login_since,
    login_checked, login_detail.
    """
    if not USE_SHARED_TASKS:
        return []
    import datetime as _dt
    conn = shared_db()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT server_id, queue_pool, last_seen, current_task_id "
                "FROM wtrlab_servers"
            )
            servers = cur.fetchall() or []
            cur.execute(
                "SELECT server_id, status, detail, since, last_check, updated_at "
                "FROM wtrlab_login_status"
            )
            logins = {r[0]: r for r in (cur.fetchall() or [])}
    finally:
        conn.close()
    now = _dt.datetime.now(_dt.timezone.utc)
    limit = max(120, HEARTBEAT_THREAD_SECONDS * 4)
    out, seen_ids = [], set()
    for sid, pool, seen, tid in servers:
        seen_ids.add(sid)
        lg = logins.get(sid)
        seen_u = _as_utc(seen)
        online = bool(seen_u and (now - seen_u).total_seconds() <= limit)
        out.append({
            "server_id": sid,
            "pool": pool,
            "last_seen": seen_u,
            "online": online,
            "task_id": tid,
            "login": (lg[1] if lg else "unknown") or "unknown",
            "login_detail": (lg[2] if lg else "") or "",
            "login_since": _as_utc(lg[3] or lg[5]) if lg else None,
            "login_checked": _as_utc((lg[4] or lg[5])) if lg else None,
        })
    for sid, lg in logins.items():  # login row but no heartbeat row yet
        if sid in seen_ids:
            continue
        out.append({
            "server_id": sid, "pool": None, "last_seen": None, "online": False,
            "task_id": None, "login": lg[1] or "unknown",
            "login_detail": lg[2] or "",
            "login_since": _as_utc(lg[3] or lg[5]),
            "login_checked": _as_utc(lg[4] or lg[5]),
        })
    out.sort(key=lambda d: _server_sort_key(d["server_id"]))
    return out


def login_text(info: dict) -> str:
    """'✅ logged in · checked 2m ago' / '❌ logged out · since 1h ago · checked 3m ago'."""
    st = info.get("login") or "unknown"
    text = LOGIN_LABELS.get(st, st)
    if st in ("logged_out", "ip_blocked") and info.get("login_since"):
        text += f" · since {fmt_ago(info['login_since'])}"
    if info.get("login_checked"):
        text += f" · checked {fmt_ago(info['login_checked'])}"
    return text


def fleet_lines() -> list:
    """HTML lines for /servers and /status (no user names)."""
    lines = []
    for info in fleet_overview():
        sid = html.escape(str(info["server_id"]))
        state = "online" if info["online"] else "offline"
        task_bit = f" · task #{info['task_id']}" if info.get("task_id") else ""
        lines.append(
            f"• <code>{sid}</code> · {state} · "
            f"pool=<code>{html.escape(str(info['pool'] or '—'))}</code> · "
            f"seen {fmt_ago(info['last_seen'])}{task_bit}\n"
            f"   🔐 {html.escape(login_text(info))}"
        )
    return lines


IP_BLOCK_MARKERS = (
    "your ip has been blocked",
    "ip was blocked",
    "you have been blocked",
    "access denied",
    "contact the site owner",
    "email the site owner",
    "cf-error",
    "error 1006",
    "error 1015",
)


def page_looks_ip_blocked(html_text: str = "", title: str = "") -> bool:
    blob = f"{title} {html_text}".lower()
    return any(m in blob for m in IP_BLOCK_MARKERS)


def set_shared_config(key: str, value: str, updated_by=None) -> None:
    if not USE_SHARED_TASKS:
        return
    key = key.strip().upper()
    try:
        conn = shared_db()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO wtrlab_config (key, value, updated_at, updated_by)
                    VALUES (%s, %s, NOW(), %s)
                    ON CONFLICT (key) DO UPDATE SET
                        value = EXCLUDED.value,
                        updated_at = NOW(),
                        updated_by = EXCLUDED.updated_by
                    """,
                    (key, value, updated_by),
                )
            conn.commit()
        finally:
            conn.close()
    except Exception as e:
        print(f"[CONFIG] set {e}")


def load_shared_config() -> None:
    """Pull CHAPTER_CAP / DAILY_TASK_LIMIT from Northflank so all servers match."""
    global DEFAULT_CHAPTER_CAP, DAILY_TASK_LIMIT
    if not USE_SHARED_TASKS:
        return
    try:
        conn = shared_db()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT key, value FROM wtrlab_config")
                rows = cur.fetchall() or []
            for key, value in rows:
                k = (key or "").upper()
                if k == "CHAPTER_CAP":
                    DEFAULT_CHAPTER_CAP = int(value) if str(value).strip().isdigit() else 0
                elif k == "DAILY_TASK_LIMIT":
                    DAILY_TASK_LIMIT = int(value) if str(value).strip().lstrip("-").isdigit() else 0
        finally:
            conn.close()
    except Exception as e:
        print(f"[CONFIG] load {e}")


def write_dotenv_key(key: str, value: str) -> None:
    env_path = BASE_DIR / ".env"
    key = key.strip().upper()
    lines = []
    if env_path.is_file():
        lines = env_path.read_text(encoding="utf-8").splitlines()
    out = []
    found = False
    for line in lines:
        if line.strip().startswith(f"{key}=") or line.strip().startswith(f"{key} ="):
            out.append(f"{key}={value}")
            found = True
        else:
            out.append(line)
    if not found:
        out.append(f"{key}={value}")
    env_path.write_text(chr(10).join(out) + chr(10), encoding="utf-8")


def get_premium(user_id: int):
    """Effective premium. Admins always win: unlimited, set by ADMIN_USER_IDS."""
    if is_admin(int(user_id)):
        return {
            "user_id": int(user_id),
            "plan": PREMIUM_PLAN_ID,
            "expires_at": None,
            "admin": True,
            "priority": 1,
            "chapters_per_day": PREMIUM_CHAPTERS_PER_DAY,
            "tasks_per_day": PREMIUM_TASKS_PER_DAY,
            "auto_continue": True,
        }
    return get_db_premium(user_id)


def get_db_premium(user_id: int):
    """Real purchased/granted premium from the database (ignores admin status)."""
    if not USE_SHARED_TASKS:
        return None
    try:
        conn = shared_db()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT user_id, plan, expires_at, granted_by
                    FROM wtrlab_premium
                    WHERE user_id = %s AND expires_at > NOW()
                    """,
                    (int(user_id),),
                )
                row = cur.fetchone()
                if not row:
                    return None
                return {
                    "user_id": row[0],
                    "plan": row[1] or PREMIUM_PLAN_ID,
                    "expires_at": row[2],
                    "granted_by": row[3],
                    "admin": False,
                    "priority": 1,
                    "chapters_per_day": PREMIUM_CHAPTERS_PER_DAY,
                    "tasks_per_day": PREMIUM_TASKS_PER_DAY,
                    "auto_continue": True,
                }
        finally:
            conn.close()
    except Exception as e:
        print(f"[PREMIUM] get: {e}")
        return None


def grant_premium(user_id: int, weeks: int = 1, granted_by=None, method: str = "admin"):
    if not USE_SHARED_TASKS:
        return False, "Shared DB not configured (set DB_PASSWORD)."
    weeks = max(1, int(weeks or 1))
    try:
        conn = shared_db()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT pg_advisory_xact_lock(%s::bigint)",
                    (int(user_id),),
                )
                cur.execute(
                    """
                    INSERT INTO wtrlab_premium (user_id, plan, expires_at, granted_by, last_method, updated_at)
                    VALUES (%s, %s, NOW() + (%s || ' weeks')::interval, %s, %s, NOW())
                    ON CONFLICT (user_id) DO UPDATE SET
                        plan = EXCLUDED.plan,
                        expires_at = GREATEST(wtrlab_premium.expires_at, NOW())
                            + (%s || ' weeks')::interval,
                        granted_by = EXCLUDED.granted_by,
                        last_method = EXCLUDED.last_method,
                        updated_at = NOW()
                    RETURNING expires_at
                    """,
                    (int(user_id), PREMIUM_PLAN_ID, str(weeks), granted_by, method, str(weeks)),
                )
                exp = cur.fetchone()[0]
            conn.commit()
            return True, f"Premium until {exp} ({weeks} week(s))"
        finally:
            conn.close()
    except Exception as e:
        return False, str(e)


def revoke_premium(user_id: int):
    if not USE_SHARED_TASKS:
        return False, "Shared DB not configured."
    try:
        conn = shared_db()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM wtrlab_premium WHERE user_id = %s RETURNING user_id",
                    (int(user_id),),
                )
                gone = cur.fetchone()
            conn.commit()
            return (True, "Revoked") if gone else (False, "Not found")
        finally:
            conn.close()
    except Exception as e:
        return False, str(e)



# ---------------------------------------------------------------------------
# Telegram Stars (XTR) payments for Premium
# ---------------------------------------------------------------------------

def format_time_left(expires_at) -> str:
    """'6d 23h' style remaining time for a timestamptz (or 'expired')."""
    import datetime as _dt
    try:
        now = _dt.datetime.now(expires_at.tzinfo) if getattr(expires_at, "tzinfo", None) else _dt.datetime.utcnow()
        secs = int((expires_at - now).total_seconds())
    except Exception:
        return "?"
    if secs <= 0:
        return "expired"
    days, rem = divmod(secs, 86400)
    hours, rem = divmod(rem, 3600)
    minutes = rem // 60
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"


def fmt_ts(value) -> str:
    try:
        return value.strftime("%Y-%m-%d %H:%M UTC")
    except Exception:
        return str(value) if value else "—"


def create_star_order(user_id: int) -> tuple[Optional[str], Optional[str]]:
    """Create a pending order for one Premium purchase. Returns (order_id, error)."""
    if not USE_SHARED_TASKS:
        return None, "Payments need the shared database (set DB_PASSWORD)."
    order_id = uuid.uuid4().hex
    try:
        conn = shared_db()
        try:
            with conn.cursor() as cur:
                # Keep earlier unexpired invoices payable. A user may already
                # have opened an invoice when they request another one; voiding
                # it here could charge them after pre-checkout but then reject
                # the successful payment. Each invoice has its own unique order
                # id and can only be fulfilled once.
                cur.execute(
                    """
                    INSERT INTO wtrlab_star_orders
                        (order_id, user_id, plan, weeks, stars_amount, currency,
                         status, expires_at)
                    VALUES (%s, %s, %s, %s, %s, 'XTR', 'pending',
                            NOW() + (%s || ' minutes')::interval)
                    """,
                    (
                        order_id, int(user_id), PREMIUM_PLAN_ID,
                        PREMIUM_STARS_WEEKS, PREMIUM_STARS_PRICE,
                        str(STARS_ORDER_TTL_MINUTES),
                    ),
                )
            conn.commit()
            return order_id, None
        finally:
            conn.close()
    except Exception as e:
        print(f"[STARS] create order: {e}")
        return None, "Could not create the order. Please try again."


def validate_star_checkout(order_id: str, user_id: int, amount: int, currency: str):
    """Pre-checkout validation. Returns (ok, error_message). Exact amount required."""
    if currency != "XTR":
        return False, "Invalid currency."
    try:
        conn = shared_db()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT user_id, stars_amount, currency, status,
                           expires_at > NOW()
                    FROM wtrlab_star_orders WHERE order_id = %s
                    """,
                    (order_id or "",),
                )
                row = cur.fetchone()
        finally:
            conn.close()
    except Exception as e:
        print(f"[STARS] pre-checkout db: {e}")
        return False, "Temporary error. Please try again in a minute."
    if not row:
        return False, "Unknown order. Open /premium and tap Pay again."
    o_user, o_amount, o_currency, o_status, not_expired = row
    if int(o_user) != int(user_id):
        return False, "This invoice belongs to another account."
    if o_currency != "XTR" or int(amount) != int(o_amount):
        return False, "Amount mismatch. Open /premium and tap Pay again."
    if int(o_amount) != int(PREMIUM_STARS_PRICE):
        return False, "The price changed. Open /premium and tap Pay again."
    if o_status != "pending":
        return False, "This invoice is no longer valid. Open /premium for a new one."
    if not not_expired:
        return False, "This invoice expired. Open /premium for a new one."
    return True, ""


def process_star_payment(user_id: int, username: Optional[str], sp) -> dict:
    """
    Record a successful Stars payment and grant Premium in ONE transaction.
    Returns {"result": "granted"|"duplicate"|"review", ...}.
    Premium is granted only when the order exists, belongs to the payer, the
    currency is XTR and the paid amount equals the ordered amount exactly.
    """
    charge_id = str(sp.telegram_payment_charge_id)
    provider_id = getattr(sp, "provider_payment_charge_id", None)
    payload = str(sp.invoice_payload or "")
    amount = int(sp.total_amount)
    currency = str(sp.currency)

    conn = shared_db()
    try:
        with conn.cursor() as cur:
            # Serialize Premium changes for this user, including two different
            # invoices paid almost simultaneously. Without this lock, two first
            # purchases could both calculate their expiry from NOW() and one
            # could overwrite the other's seven days.
            cur.execute(
                "SELECT pg_advisory_xact_lock(%s::bigint)",
                (int(user_id),),
            )
            cur.execute(
                "SELECT id FROM wtrlab_star_payments "
                "WHERE telegram_payment_charge_id = %s",
                (charge_id,),
            )
            dup = cur.fetchone()
            if dup:
                conn.rollback()
                return {"result": "duplicate", "payment_id": dup[0]}

            cur.execute(
                """
                SELECT user_id, stars_amount, currency, status, weeks, plan
                FROM wtrlab_star_orders WHERE order_id = %s FOR UPDATE
                """,
                (payload,),
            )
            order = cur.fetchone()

            # Recheck after acquiring the order lock. Another update for the
            # same charge may have committed while this transaction waited.
            cur.execute(
                "SELECT id FROM wtrlab_star_payments "
                "WHERE telegram_payment_charge_id = %s",
                (charge_id,),
            )
            dup = cur.fetchone()
            if dup:
                conn.rollback()
                return {"result": "duplicate", "payment_id": dup[0]}

            problem = None
            if not order:
                problem = "unknown order id"
            elif int(order[0]) != int(user_id):
                problem = "order belongs to a different user"
            elif currency != "XTR" or order[2] != "XTR":
                problem = f"wrong currency ({currency})"
            elif amount != int(order[1]):
                problem = f"amount mismatch (paid {amount}, ordered {order[1]})"
            elif order[3] != "pending":
                problem = f"order status is '{order[3]}'"

            if problem:
                cur.execute(
                    """
                    INSERT INTO wtrlab_star_payments
                        (order_id, user_id, telegram_username,
                         telegram_payment_charge_id, provider_payment_charge_id,
                         currency, amount, plan, weeks, status, note)
                    VALUES (NULL, %s, %s, %s, %s, %s, %s, NULL, 0, 'review', %s)
                    ON CONFLICT (telegram_payment_charge_id) DO NOTHING
                    RETURNING id
                    """,
                    (
                        int(user_id), username, charge_id, provider_id,
                        currency, amount, f"{problem}; payload={payload[:80]}",
                    ),
                )
                row = cur.fetchone()
                conn.commit()
                return {
                    "result": "review",
                    "problem": problem,
                    "payment_id": row[0] if row else None,
                    "charge_id": charge_id,
                    "amount": amount,
                }

            weeks = int(order[4] or 1)
            plan = order[5] or PREMIUM_PLAN_ID

            cur.execute(
                "SELECT GREATEST(expires_at, NOW()) FROM wtrlab_premium "
                "WHERE user_id = %s FOR UPDATE",
                (int(user_id),),
            )
            cur_row = cur.fetchone()
            if cur_row:
                starts_at = cur_row[0]
            else:
                cur.execute("SELECT NOW()")
                starts_at = cur.fetchone()[0]
            cur.execute(
                "SELECT %s::timestamptz + (%s || ' weeks')::interval",
                (starts_at, str(weeks)),
            )
            ends_at = cur.fetchone()[0]

            cur.execute(
                """
                INSERT INTO wtrlab_premium
                    (user_id, plan, expires_at, granted_by, last_method, updated_at)
                VALUES (%s, %s, %s, NULL, 'stars', NOW())
                ON CONFLICT (user_id) DO UPDATE SET
                    plan = EXCLUDED.plan,
                    expires_at = EXCLUDED.expires_at,
                    granted_by = NULL,
                    last_method = 'stars',
                    updated_at = NOW()
                """,
                (int(user_id), plan, ends_at),
            )
            cur.execute(
                """
                INSERT INTO wtrlab_star_payments
                    (order_id, user_id, telegram_username,
                     telegram_payment_charge_id, provider_payment_charge_id,
                     currency, amount, plan, weeks, status,
                     premium_started_at, premium_expires_at)
                VALUES (%s, %s, %s, %s, %s, 'XTR', %s, %s, %s, 'paid', %s, %s)
                ON CONFLICT (telegram_payment_charge_id) DO NOTHING
                RETURNING id
                """,
                (
                    payload, int(user_id), username, charge_id, provider_id,
                    amount, plan, weeks, starts_at, ends_at,
                ),
            )
            pay_row = cur.fetchone()
            if not pay_row:
                conn.rollback()
                return {"result": "duplicate", "payment_id": None}
            payment_id = pay_row[0]
            cur.execute(
                """
                INSERT INTO wtrlab_star_entitlements
                    (payment_id, user_id, starts_at, ends_at, status)
                VALUES (%s, %s, %s, %s, 'active')
                """,
                (payment_id, int(user_id), starts_at, ends_at),
            )
            cur.execute(
                "UPDATE wtrlab_star_orders SET status = 'paid', paid_at = NOW() "
                "WHERE order_id = %s",
                (payload,),
            )
        conn.commit()
        return {
            "result": "granted",
            "payment_id": payment_id,
            "starts_at": starts_at,
            "ends_at": ends_at,
            "weeks": weeks,
            "amount": amount,
            "charge_id": charge_id,
        }
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        raise
    finally:
        conn.close()


def telegram_refund_star_payment(user_id: int, charge_id: str):
    """Call Telegram's refundStarPayment (works on older pyTelegramBotAPI too)."""
    fn = getattr(bot, "refund_star_payment", None)
    if fn:
        return fn(int(user_id), charge_id)
    from telebot import apihelper
    return apihelper._make_request(
        bot.token,
        "refundStarPayment",
        params={"user_id": int(user_id), "telegram_payment_charge_id": charge_id},
        method="post",
    )


def activate_review_star_payment(payment_id: int, admin_id: int, weeks: int = 1):
    """Manually activate a successfully paid Stars transaction held for review."""
    weeks = max(1, min(52, int(weeks or 1)))
    conn = shared_db()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT user_id, amount, currency, telegram_payment_charge_id "
                "FROM wtrlab_star_payments WHERE id = %s AND status = 'review' FOR UPDATE",
                (int(payment_id),),
            )
            payment = cur.fetchone()
            if not payment:
                conn.rollback()
                return False, "Review payment not found or it is no longer awaiting review.", None
            user_id, amount, currency, charge_id = payment
            cur.execute(
                "SELECT pg_advisory_xact_lock(%s::bigint)",
                (int(user_id),),
            )
            cur.execute(
                "SELECT GREATEST(expires_at, NOW()) FROM wtrlab_premium "
                "WHERE user_id = %s FOR UPDATE",
                (int(user_id),),
            )
            row = cur.fetchone()
            if row:
                starts_at = row[0]
            else:
                cur.execute("SELECT NOW()")
                starts_at = cur.fetchone()[0]
            cur.execute(
                "SELECT %s::timestamptz + (%s || ' weeks')::interval",
                (starts_at, str(weeks)),
            )
            ends_at = cur.fetchone()[0]
            cur.execute(
                """
                INSERT INTO wtrlab_premium
                    (user_id, plan, expires_at, granted_by, last_method, updated_at)
                VALUES (%s, %s, %s, %s, 'stars', NOW())
                ON CONFLICT (user_id) DO UPDATE SET
                    plan = EXCLUDED.plan, expires_at = EXCLUDED.expires_at,
                    granted_by = EXCLUDED.granted_by, last_method = 'stars',
                    updated_at = NOW()
                """,
                (int(user_id), PREMIUM_PLAN_ID, ends_at, int(admin_id)),
            )
            cur.execute(
                """
                UPDATE wtrlab_star_payments
                SET status = 'paid', plan = %s, weeks = %s,
                    premium_started_at = %s, premium_expires_at = %s,
                    note = CONCAT(COALESCE(note, ''), %s)
                WHERE id = %s AND status = 'review'
                """,
                (PREMIUM_PLAN_ID, weeks, starts_at, ends_at,
                 f"; manually activated by admin {admin_id}", int(payment_id)),
            )
            cur.execute(
                """
                INSERT INTO wtrlab_star_entitlements
                    (payment_id, user_id, starts_at, ends_at, status)
                VALUES (%s, %s, %s, %s, 'active')
                """,
                (int(payment_id), int(user_id), starts_at, ends_at),
            )
        conn.commit()
        return True, f"Activated {weeks} week(s) for user {user_id}; expires {fmt_ts(ends_at)}. ({amount} {currency}, charge {charge_id})", int(user_id)
    except Exception as error:
        try:
            conn.rollback()
        except Exception:
            pass
        return False, f"Activation failed: {error}", None
    finally:
        conn.close()


def refund_star_payment_record(payment_id: int, admin_id: int, reason: str = ""):
    """
    Refund via Telegram, then update ledger, entitlement and Premium expiry.
    Supports both normal paid records and successful payments held for review.
    Returns (ok, message, user_id).
    """
    conn = shared_db()
    try:
        with conn.cursor() as cur:
            # Claim the payment so two admins cannot refund it twice, while
            # remembering its previous state in case Telegram rejects the call.
            cur.execute(
                """
                WITH candidate AS (
                    SELECT id, status FROM wtrlab_star_payments
                    WHERE id = %s AND status IN ('paid', 'review')
                    FOR UPDATE
                )
                UPDATE wtrlab_star_payments p SET status = 'refunding'
                FROM candidate c WHERE p.id = c.id
                RETURNING p.user_id, p.telegram_payment_charge_id,
                          p.amount, p.weeks, c.status
                """,
                (int(payment_id),),
            )
            row = cur.fetchone()
        conn.commit()
        if not row:
            return False, "Payment not found, already refunded, or not refundable.", None
        user_id, charge_id, amount, weeks, previous_status = row

        try:
            telegram_refund_star_payment(int(user_id), charge_id)
        except Exception as error:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE wtrlab_star_payments SET status = %s WHERE id = %s AND status = 'refunding'",
                    (previous_status, int(payment_id)),
                )
            conn.commit()
            return False, f"Telegram refused the refund: {error}", int(user_id)

        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE wtrlab_star_payments
                SET status = 'refunded', refunded_at = NOW(),
                    refunded_by = %s, refund_reason = %s
                WHERE id = %s
                """,
                (int(admin_id), (reason or "")[:300], int(payment_id)),
            )
            cur.execute(
                "UPDATE wtrlab_star_entitlements SET status = 'refunded' "
                "WHERE payment_id = %s AND status = 'active' RETURNING id",
                (int(payment_id),),
            )
            if cur.fetchone():
                # Take back only the time this payment bought.
                cur.execute(
                    """
                    UPDATE wtrlab_premium
                    SET expires_at = expires_at - (%s || ' weeks')::interval,
                        updated_at = NOW()
                    WHERE user_id = %s
                    """,
                    (str(int(weeks or 1)), int(user_id)),
                )
        conn.commit()
        return True, f"Refunded {amount} ⭐ to user {user_id}.", int(user_id)
    except Exception as error:
        try:
            conn.rollback()
        except Exception:
            pass
        return False, f"Refund error: {error}", None
    finally:
        conn.close()



def claim_email_lock(email: str, user_id=None) -> tuple:
    """Prevent same WTR email on two workers. Returns (ok, message)."""
    email = (email or "").strip().lower()
    if not email or not USE_SHARED_TASKS:
        return True, ""
    try:
        conn = shared_db()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT server_id, user_id FROM wtrlab_email_locks WHERE email = %s",
                    (email,),
                )
                row = cur.fetchone()
                if row and row[0] != SERVER_ID:
                    return False, (
                        f"Email already in use on worker {row[0]}. "
                        "Use a different email on this server."
                    )
                cur.execute(
                    """
                    INSERT INTO wtrlab_email_locks (email, server_id, user_id, locked_at)
                    VALUES (%s, %s, %s, NOW())
                    ON CONFLICT (email) DO UPDATE SET
                        server_id = EXCLUDED.server_id,
                        user_id = EXCLUDED.user_id,
                        locked_at = NOW()
                    """,
                    (email, SERVER_ID, user_id),
                )
            conn.commit()
            return True, ""
        finally:
            conn.close()
    except Exception as e:
        print(f"[EMAIL LOCK] {e}")
        return True, ""


def release_email_lock(email: str) -> None:
    if not USE_SHARED_TASKS or not email:
        return
    try:
        conn = shared_db()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM wtrlab_email_locks WHERE email = %s AND server_id = %s",
                    (email.strip().lower(), SERVER_ID),
                )
            conn.commit()
        finally:
            conn.close()
    except Exception as e:
        print(f"[EMAIL UNLOCK] {e}")


def extract_novel_id(url: str):
    try:
        path = urllib.parse.urlparse(url).path or ""
        m = re.search(r"/novel/(\d+)", path)
        return m.group(1) if m else None
    except Exception:
        return None


def record_delivery(
    *,
    user_id=None,
    chat_id=None,
    url: str = "",
    chapter_range: str = "all",
    task_id=None,
    epub_name=None,
    novel_id=None,
    queue=None,
) -> None:
    if not USE_SHARED_TASKS or not url:
        return
    nid = novel_id or extract_novel_id(url)
    try:
        conn = shared_db()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO wtrlab_deliveries (
                        user_id, chat_id, novel_id, url, chapter_range,
                        server_id, queue, task_id, epub_name, delivered_at
                    ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,NOW())
                    """,
                    (
                        user_id, chat_id, nid, url, chapter_range or "all",
                        SERVER_ID, queue or QUEUE_POOL, task_id, epub_name,
                    ),
                )
            conn.commit()
        finally:
            conn.close()
    except Exception as e:
        print(f"[DELIVERY] {e}")


def delivery_home_for_novel(novel_id=None, url=None):
    if not USE_SHARED_TASKS:
        return None
    try:
        conn = shared_db()
        try:
            with conn.cursor() as cur:
                if novel_id:
                    cur.execute(
                        """
                        SELECT server_id, queue, chapter_range, delivered_at
                        FROM wtrlab_deliveries WHERE novel_id = %s
                        ORDER BY delivered_at DESC LIMIT 1
                        """,
                        (str(novel_id),),
                    )
                elif url:
                    cur.execute(
                        """
                        SELECT server_id, queue, chapter_range, delivered_at
                        FROM wtrlab_deliveries WHERE url = %s
                        ORDER BY delivered_at DESC LIMIT 1
                        """,
                        (url,),
                    )
                else:
                    return None
                row = cur.fetchone()
                if not row:
                    return None
                return {
                    "server_id": row[0],
                    "queue": row[1],
                    "chapter_range": row[2],
                    "delivered_at": row[3],
                }
        finally:
            conn.close()
    except Exception as e:
        print(f"[DELIVERY LOOKUP] {e}")
        return None


def register_auto_continue(
    user_id, chat_id, url, chapter_range,
    username=None, first_name=None, last_name=None, last_task_id=None,
) -> None:
    if not USE_SHARED_TASKS:
        return
    try:
        conn = shared_db()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO wtrlab_auto_continue (
                        user_id, chat_id, url, chapter_range,
                        username, first_name, last_name,
                        runs_done, max_runs, enabled, last_task_id, updated_at
                    ) VALUES (%s,%s,%s,%s,%s,%s,%s,0,2,TRUE,%s,NOW())
                    """,
                    (
                        int(user_id), int(chat_id), url, chapter_range or "all",
                        username, first_name, last_name, last_task_id,
                    ),
                )
            conn.commit()
        finally:
            conn.close()
    except Exception as e:
        print(f"[AUTO-CONT REGISTER] {e}")


def process_auto_continues() -> int:
    """
    When premium user has remaining daily pulls and an enabled auto-continue
    row with runs_done < max_runs, enqueue a new task.
    Called periodically from the coordinator worker loop.
    """
    if not USE_SHARED_TASKS or not IS_COORDINATOR:
        return 0
    spawned = 0
    try:
        conn = shared_db()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT id, user_id, chat_id, url, chapter_range,
                           username, first_name, last_name, runs_done, max_runs
                    FROM wtrlab_auto_continue
                    WHERE enabled = TRUE AND runs_done < max_runs
                    ORDER BY id ASC
                    LIMIT 20
                    """
                )
                rows = cur.fetchall()
            for row in rows:
                (ac_id, uid, chat_id, url, crange,
                 uname, fname, lname, runs_done, max_runs) = row
                prem = get_premium(int(uid))
                if not prem:
                    continue
                # Skip if user still has pending/running for same url
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        SELECT 1 FROM wtrlab_tasks
                        WHERE user_id = %s AND url = %s
                          AND status IN ('pending', 'running', 'paused')
                        LIMIT 1
                        """,
                        (int(uid), url),
                    )
                    if cur.fetchone():
                        continue
                # Need remaining chapter allowance
                rem = pulls_remaining(int(uid), unlimited=bool(prem.get("admin")))
                if rem <= 0 and get_chapter_cap(int(uid)) > 0:
                    continue
                tid, err = insert_task(
                    chat_id=int(chat_id),
                    user_id=int(uid),
                    url=url,
                    chapter_range=crange or "all",
                    username=uname,
                    first_name=fname,
                    last_name=lname,
                    auto_continue=True,
                )
                if err or not tid:
                    continue
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        UPDATE wtrlab_auto_continue
                        SET runs_done = runs_done + 1,
                            last_task_id = %s,
                            updated_at = NOW(),
                            enabled = CASE WHEN runs_done + 1 >= max_runs
                                           THEN FALSE ELSE TRUE END
                        WHERE id = %s
                        """,
                        (tid, ac_id),
                    )
                conn.commit()
                spawned += 1
                try:
                    bot.send_message(
                        chat_id,
                        f"🔁 Auto-Continue started task #{tid} "
                        f"({runs_done + 1}/{max_runs}) for your novel.",
                    )
                except Exception:
                    pass
        finally:
            conn.close()
    except Exception as e:
        print(f"[AUTO-CONT] {e}")
    return spawned


def chapter_db():
    """Coordinator Postgres for chapters index + chapter_pulls (not NFS)."""
    if not CHAPTER_DATABASE_URL:
        return None
    pg = _psycopg2()
    return pg.connect(CHAPTER_DATABASE_URL, connect_timeout=15)


def use_chapter_pg() -> bool:
    return bool(CHAPTER_DATABASE_URL)


def setup_chapter_db() -> None:
    """Create chapters + chapter_pulls on coordinator Postgres if configured."""
    if not CHAPTER_DATABASE_URL:
        return
    conn = chapter_db()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS chapters (
                    novel_id TEXT NOT NULL,
                    chapter_no INT NOT NULL,
                    chapter_id TEXT,
                    title TEXT,
                    xhtml_path TEXT NOT NULL,
                    downloaded_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    PRIMARY KEY (novel_id, chapter_no)
                )
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS chapter_pulls (
                    user_id BIGINT NOT NULL,
                    novel_id TEXT NOT NULL,
                    chapter_no INT NOT NULL,
                    pulled_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    PRIMARY KEY (user_id, novel_id, chapter_no)
                )
                """
            )
            cur.execute(
                """
                CREATE INDEX IF NOT EXISTS chapter_pulls_user_time_idx
                    ON chapter_pulls (user_id, pulled_at)
                """
            )
        conn.commit()
        print("[CHAPTER DB] coordinator Postgres ready for chapters + pulls")
    finally:
        conn.close()



def setup_local_db():
    conn = local_db()
    try:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS tasks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                username TEXT,
                first_name TEXT,
                last_name TEXT,
                url TEXT NOT NULL,
                chapter_range TEXT NOT NULL DEFAULT 'all',
                status TEXT NOT NULL DEFAULT 'pending',
                novel_title TEXT,
                total_chapters INTEGER,
                error TEXT,
                created_at TEXT DEFAULT (datetime('now')),
                completed_at TEXT,
                last_progress_at TEXT
            );

            CREATE INDEX IF NOT EXISTS tasks_status_idx
                ON tasks (status, id);

            CREATE TABLE IF NOT EXISTS novels (
                novel_id TEXT PRIMARY KEY,
                source_url TEXT NOT NULL,
                title TEXT,
                author TEXT,
                synopsis TEXT,
                cover_url TEXT,
                updated_at TEXT DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS chapters (
                novel_id TEXT NOT NULL,
                chapter_no INTEGER NOT NULL,
                chapter_id TEXT,
                title TEXT,
                xhtml_path TEXT NOT NULL,
                downloaded_at TEXT DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (novel_id, chapter_no)
            );

            CREATE TABLE IF NOT EXISTS task_cache (
                task_id INTEGER PRIMARY KEY,
                novel_id TEXT,
                epub_path TEXT,
                display_title TEXT,
                updated_at TEXT DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS chapter_pulls (
                user_id INTEGER NOT NULL,
                novel_id TEXT NOT NULL,
                chapter_no INTEGER NOT NULL,
                pulled_at TEXT NOT NULL DEFAULT (datetime('now')),
                PRIMARY KEY (user_id, novel_id, chapter_no)
            );

            CREATE INDEX IF NOT EXISTS chapter_pulls_user_novel_time_idx
                ON chapter_pulls (user_id, novel_id, pulled_at);

            CREATE TABLE IF NOT EXISTS user_bans (
                user_id INTEGER PRIMARY KEY,
                banned_until TEXT,
                reason TEXT,
                banned_by INTEGER,
                created_at TEXT NOT NULL DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS user_limits (
                user_id INTEGER PRIMARY KEY,
                chapter_cap INTEGER NOT NULL,
                updated_at TEXT NOT NULL DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS user_actions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts TEXT NOT NULL DEFAULT (datetime('now')),
                session_id TEXT,
                task_id INTEGER,
                user_id INTEGER,
                username TEXT,
                kind TEXT NOT NULL,
                detail TEXT
            );

            CREATE INDEX IF NOT EXISTS user_actions_user_idx
                ON user_actions (user_id, id);
            """
        )
        # Always sync CHAPTER_CAP from .env so changing the env takes effect
        # (INSERT OR IGNORE left stale 0 values forever).
        conn.execute(
            """
            INSERT INTO settings(key, value) VALUES ('chapter_cap', ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """,
            (str(DEFAULT_CHAPTER_CAP),),
        )
        # Existing installs may predate these columns.
        for col in ("first_name", "last_name"):
            try:
                conn.execute(f"ALTER TABLE tasks ADD COLUMN {col} TEXT")
            except sqlite3.OperationalError:
                pass  # column already exists
        for col, decl in (
            ("run_next", "INTEGER NOT NULL DEFAULT 0"),
            ("paused_until", "TEXT"),
        ):
            try:
                conn.execute(f"ALTER TABLE tasks ADD COLUMN {col} {decl}")
            except sqlite3.OperationalError:
                pass
        try:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS chapter_pulls (
                    user_id INTEGER NOT NULL,
                    novel_id TEXT NOT NULL,
                    chapter_no INTEGER NOT NULL,
                    pulled_at TEXT NOT NULL DEFAULT (datetime('now')),
                    PRIMARY KEY (user_id, novel_id, chapter_no)
                );
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS chapter_pulls_user_novel_time_idx
                    ON chapter_pulls (user_id, novel_id, pulled_at);
                """
            )
        except sqlite3.OperationalError:
            pass
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Bans, per-user chapter limit overrides, and the action log
# ---------------------------------------------------------------------------

def get_ban(user_id: int) -> Optional[dict]:
    """Active ban for this user, or None. Expired timed bans are removed."""
    conn = local_db()
    try:
        row = conn.execute(
            "SELECT user_id, banned_until, reason FROM user_bans WHERE user_id = ?",
            (int(user_id),),
        ).fetchone()
        if not row:
            return None
        until = row["banned_until"]
        if until:
            expired = conn.execute(
                "SELECT datetime('now') >= ?", (until,)
            ).fetchone()[0]
            if expired:
                conn.execute("DELETE FROM user_bans WHERE user_id = ?", (int(user_id),))
                conn.commit()
                return None
        return {"user_id": row["user_id"], "until": until, "reason": row["reason"] or ""}
    except Exception as error:
        print(f"[BAN CHECK ERROR] {error}")
        return None
    finally:
        conn.close()


def ban_user(user_id: int, days: Optional[int], by: int, reason: str = "") -> Optional[str]:
    """Ban forever (days=None) or for N days. Returns the expiry text or None."""
    conn = local_db()
    try:
        until = None
        if days is not None:
            until = conn.execute(
                "SELECT datetime('now', ?)", (f"+{int(days)} days",)
            ).fetchone()[0]
        conn.execute(
            """
            INSERT INTO user_bans (user_id, banned_until, reason, banned_by)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                banned_until = excluded.banned_until,
                reason = excluded.reason,
                banned_by = excluded.banned_by,
                created_at = datetime('now')
            """,
            (int(user_id), until, reason, int(by)),
        )
        conn.commit()
        return until
    finally:
        conn.close()


def unban_user(user_id: int) -> bool:
    conn = local_db()
    try:
        cur = conn.execute("DELETE FROM user_bans WHERE user_id = ?", (int(user_id),))
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def resolve_user_ref(ref: str) -> Optional[int]:
    """Numeric Telegram id, or @username seen in tasks / the action log."""
    ref = (ref or "").strip()
    if ref.lstrip("-").isdigit():
        return int(ref)
    name = ref.lstrip("@").strip().lower()
    if not name:
        return None
    conn = local_db()
    try:
        row = conn.execute(
            "SELECT user_id FROM tasks WHERE lower(username) = ? ORDER BY id DESC LIMIT 1",
            (name,),
        ).fetchone()
        if not row:
            row = conn.execute(
                "SELECT user_id FROM user_actions WHERE lower(username) = ? "
                "ORDER BY id DESC LIMIT 1",
                (name,),
            ).fetchone()
        return int(row["user_id"]) if row and row["user_id"] is not None else None
    finally:
        conn.close()


def username_for(user_id: Optional[int]) -> str:
    if user_id is None:
        return ""
    conn = local_db()
    try:
        row = conn.execute(
            "SELECT username FROM tasks WHERE user_id = ? AND username IS NOT NULL "
            "AND username != '' ORDER BY id DESC LIMIT 1",
            (int(user_id),),
        ).fetchone()
        return (row["username"] or "") if row else ""
    except Exception:
        return ""
    finally:
        conn.close()


def get_user_cap_override(user_id: int) -> Optional[int]:
    conn = local_db()
    try:
        row = conn.execute(
            "SELECT chapter_cap FROM user_limits WHERE user_id = ?", (int(user_id),)
        ).fetchone()
        return int(row["chapter_cap"]) if row else None
    except Exception:
        return None
    finally:
        conn.close()


def set_user_cap_override(user_id: int, cap: Optional[int]) -> None:
    """cap=None removes the override; cap=0 means unlimited for this user."""
    conn = local_db()
    try:
        if cap is None:
            conn.execute("DELETE FROM user_limits WHERE user_id = ?", (int(user_id),))
        else:
            conn.execute(
                """
                INSERT INTO user_limits (user_id, chapter_cap) VALUES (?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    chapter_cap = excluded.chapter_cap,
                    updated_at = datetime('now')
                """,
                (int(user_id), int(cap)),
            )
        conn.commit()
    finally:
        conn.close()


def log_action(
    session_id: Optional[str],
    task_id: Optional[int],
    user_id: Optional[int],
    username: str,
    kind: str,
    detail: str = "",
) -> None:
    try:
        conn = local_db()
        try:
            conn.execute(
                """
                INSERT INTO user_actions
                    (session_id, task_id, user_id, username, kind, detail)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (session_id, task_id, user_id, username or "", kind, (detail or "")[:500]),
            )
            conn.commit()
        finally:
            conn.close()
    except Exception as error:
        print(f"[ACTION LOG ERROR] {error}")


def get_chapter_cap(user_id: Optional[int] = None) -> int:
    """
    Max first-time network chapter fetches per 24h. With user_id, a per-user
    override from /userlimit wins (0 = unlimited for that user).
    """
    if user_id is not None:
        prem = get_premium(int(user_id))
        if prem and not prem.get("admin"):
            return int(prem.get("chapters_per_day") or PREMIUM_CHAPTERS_PER_DAY)
    if user_id is not None:
        override = get_user_cap_override(user_id)
        if override is not None:
            return max(0, override)
    return get_default_chapter_cap()


def get_default_chapter_cap() -> int:
    """
    Max first-time network chapter fetches per user per 24h, UNIVERSAL
    across all novels (CHAPTER_CAP in .env). 0 = unlimited. Cache hits
    do not count.

    .env is the source of truth; the settings row is kept in sync on startup.
    """
    # Prefer the value loaded from .env at process start.
    if DEFAULT_CHAPTER_CAP >= 0:
        return DEFAULT_CHAPTER_CAP
    conn = local_db()
    try:
        row = conn.execute(
            "SELECT value FROM settings WHERE key='chapter_cap'"
        ).fetchone()
        if not row:
            return 0
        return max(0, int(row["value"]))
    except Exception:
        return 0
    finally:
        conn.close()


def apply_range_cap(chapter_range: str, unlimited: bool = False) -> str:
    """
    Ranges are no longer shrunk to 1..CHAPTER_CAP.
    CHAPTER_CAP limits daily network fetches during download instead.
    """
    text = (chapter_range or "all").strip()
    return text or "all"


def count_chapter_pulls(user_id: int, hours: int = 24) -> int:
    """
    How many first-time network chapter fetches this user did in the window,
    UNIVERSAL across all novels (same scope as DAILY_TASK_LIMIT).
    Uses coordinator Postgres when CHAPTER_DATABASE_URL is set.
    """
    if use_chapter_pg():
        try:
            conn = chapter_db()
            try:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        SELECT COUNT(*) FROM chapter_pulls
                        WHERE user_id = %s
                          AND pulled_at >= NOW() - (%s || ' hours')::interval
                        """,
                        (int(user_id), str(int(hours))),
                    )
                    row = cur.fetchone()
                    return int(row[0] if row else 0)
            finally:
                conn.close()
        except Exception as error:
            print(f"[PULL COUNT PG] {error}")
            return 0
    conn = local_db()
    try:
        row = conn.execute(
            """
            SELECT COUNT(*) AS c FROM chapter_pulls
            WHERE user_id = ?
              AND pulled_at >= datetime('now', ?)
            """,
            (int(user_id), f"-{int(hours)} hours"),
        ).fetchone()
        return int(row["c"] if row else 0)
    except Exception as error:
        print(f"[PULL COUNT ERROR] {error}")
        return 0
    finally:
        conn.close()


def register_chapter_pull(
    user_id: int, novel_id: str, chapter_no: int
) -> bool:
    """
    Record a first-time network fetch. Returns True if this row is new
    (counts against CHAPTER_CAP). Re-downloads of the same chapter do not
    insert again and do not count.
    """
    if use_chapter_pg():
        try:
            conn = chapter_db()
            try:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        INSERT INTO chapter_pulls (user_id, novel_id, chapter_no, pulled_at)
                        VALUES (%s, %s, %s, NOW())
                        ON CONFLICT (user_id, novel_id, chapter_no) DO NOTHING
                        RETURNING 1
                        """,
                        (int(user_id), str(novel_id), int(chapter_no)),
                    )
                    row = cur.fetchone()
                conn.commit()
                return row is not None
            finally:
                conn.close()
        except Exception as error:
            print(f"[PULL REGISTER PG] {error}")
            return False
    conn = local_db()
    try:
        cur = conn.execute(
            """
            INSERT OR IGNORE INTO chapter_pulls
                (user_id, novel_id, chapter_no, pulled_at)
            VALUES (?, ?, ?, datetime('now'))
            """,
            (int(user_id), str(novel_id), int(chapter_no)),
        )
        conn.commit()
        return cur.rowcount > 0
    except Exception as error:
        print(f"[PULL REGISTER ERROR] {error}")
        return False
    finally:
        conn.close()


def pulls_remaining(user_id: int, unlimited: bool = False) -> int:
    """
    Remaining first-time network fetches for this user, UNIVERSAL across all
    novels in 24h (same scope as DAILY_TASK_LIMIT — does not reset per novel).
    Large number means unlimited (admin or CHAPTER_CAP=0).
    """
    if unlimited or is_admin(user_id):
        return 10**9
    cap = get_chapter_cap(user_id)
    if cap <= 0:
        return 10**9
    used = count_chapter_pulls(user_id, hours=24)
    return max(0, cap - used)


def fresh_downloads_line(user_id: int, unlimited: bool, cap: int) -> str:
    """One-line 'fresh downloads remaining' string for progress messages.
    'Fresh downloads' = chapters fetched new from the site (vs already-cached
    chapters, which are always free and don't count here)."""
    if unlimited or is_admin(user_id) or cap <= 0:
        return "⬇️ Fresh chapter downloads: unlimited"
    remaining = pulls_remaining(user_id, unlimited)
    return f"⬇️ Fresh chapter downloads left: {remaining}/{cap} (24h)"


def daily_usage_summary(user_id: int) -> str:
    """
    Short summary sent after an EPUB is delivered: tasks and fresh chapter
    downloads left today (both on a rolling 24h window).
    """
    unlimited = is_admin(user_id)

    task_limit = DAILY_TASK_LIMIT
    if not unlimited and get_premium(user_id):
        task_limit = PREMIUM_TASKS_PER_DAY
    if task_limit > 0 and not unlimited:
        tasks_left = max(0, task_limit - count_recent_tasks(user_id))
        task_part = f"{tasks_left} task(s)"
    else:
        task_part = "unlimited tasks"

    cap = get_chapter_cap(user_id)
    if cap > 0 and not unlimited:
        remaining = pulls_remaining(user_id, unlimited)
        chapter_part = f"{remaining}/{cap} fresh chapter downloads"
    else:
        chapter_part = "unlimited fresh chapter downloads"

    return (
        f"📊 You have {task_part} and {chapter_part} remaining today.\n"
        "Send /cap to view your limits."
    )


def normalize_novel_url(url: str) -> str:
    """Strip query/fragment so the same novel matches across retries."""
    try:
        parts = urllib.parse.urlsplit(url.strip())
        path = parts.path.rstrip("/") or "/"
        return urllib.parse.urlunsplit(
            (parts.scheme.lower(), parts.netloc.lower(), path, "", "")
        )
    except Exception:
        return (url or "").strip().lower()


def count_recent_tasks(user_id: int, hours: int = 24) -> int:
    """
    Count tasks that consume the daily quota.
    Includes failed so a mid-download failure still holds the slot for *new*
    novels; same-link resume is allowed separately via is_same_link_retry().
    """
    if USE_SHARED_TASKS:
        try:
            conn = shared_db()
            try:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        SELECT COUNT(*) FROM wtrlab_tasks
                        WHERE user_id = %s
                          AND status IN (
                              'pending', 'running', 'done',
                              'failed', 'upload_failed'
                          )
                          AND created_at >= NOW() - (%s || ' hours')::interval
                        """,
                        (int(user_id), str(int(hours))),
                    )
                    return int(cur.fetchone()[0])
            finally:
                conn.close()
        except Exception as e:
            print(f"[TASK COUNT SHARED] {e}")
    conn = local_db()
    try:
        row = conn.execute(
            """
            SELECT COUNT(*) AS c FROM tasks
            WHERE user_id = ?
              AND status IN (
                  'pending', 'running', 'done',
                  'failed', 'upload_failed'
              )
              AND created_at >= datetime('now', ?)
            """,
            (user_id, f"-{hours} hours"),
        ).fetchone()
        return int(row["c"])
    finally:
        conn.close()


def is_same_link_retry(user_id: int, url: str, hours: int = 24) -> bool:
    """
    True if this user already started the same novel recently and the latest
    attempt for that link was not a full success — resend is a resume and
    does not consume another daily task slot.
    """
    target = normalize_novel_url(url)
    if USE_SHARED_TASKS:
        try:
            conn = shared_db()
            try:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        SELECT url, status FROM wtrlab_tasks
                        WHERE user_id = %s
                          AND created_at >= NOW() - (%s || ' hours')::interval
                        ORDER BY id DESC
                        LIMIT 50
                        """,
                        (int(user_id), str(int(hours))),
                    )
                    shared_rows = cur.fetchall()
            finally:
                conn.close()
            for s_url, s_status in shared_rows:
                if normalize_novel_url(s_url) != target:
                    continue
                return s_status != "done"
            return False
        except Exception as e:
            print(f"[SAME LINK SHARED] {e}")
    conn = local_db()
    try:
        rows = conn.execute(
            """
            SELECT url, status FROM tasks
            WHERE user_id = ?
              AND created_at >= datetime('now', ?)
            ORDER BY id DESC
            LIMIT 50
            """,
            (user_id, f"-{hours} hours"),
        ).fetchall()
        for row in rows:
            if normalize_novel_url(row["url"]) != target:
                continue
            # Latest row for this novel decides: only free-retry if not fully done.
            return row["status"] != "done"
        return False
    finally:
        conn.close()


def _norm_range(chapter_range: Optional[str]) -> str:
    return re.sub(r"\s+", "-", (chapter_range or "all").strip().lower())


def duplicate_request_for_user(
    user_id: int, url: str, chapter_range: Optional[str]
) -> Optional[str]:
    """
    Same user + same novel + same range already pending/running/paused is a
    double-send: refuse it. Anyone else (or the same user with a DIFFERENT
    range) is allowed to queue the same novel; claim_task() runs the tasks of
    one novel one at a time, so servers never write the same folder together.
    """
    target = normalize_novel_url(url)
    want = _norm_range(chapter_range)
    rows = []
    if USE_SHARED_TASKS:
        try:
            _, rows = _pg_exec(
                """
                SELECT id, status, url, chapter_range FROM wtrlab_tasks
                WHERE user_id = %s AND status IN ('pending', 'running', 'paused')
                ORDER BY id DESC LIMIT 100
                """,
                (int(user_id),), fetch=True,
            )
            rows = rows or []
        except Exception as e:
            log.exception("[DUP CHECK SHARED] %s", e)
            return None
    else:
        conn = local_db()
        try:
            rows = [
                (r["id"], r["status"], r["url"], r["chapter_range"])
                for r in conn.execute(
                    """
                    SELECT id, status, url, chapter_range FROM tasks
                    WHERE user_id = ? AND status IN ('pending', 'running', 'paused')
                    ORDER BY id DESC LIMIT 100
                    """,
                    (int(user_id),),
                ).fetchall()
            ]
        finally:
            conn.close()
    for tid, st, u, cr in rows:
        if normalize_novel_url(u or "") == target and _norm_range(cr) == want:
            return (
                f"You already have this novel (same chapter range) queued as "
                f"task #{tid} (status={st}). Wait for it to finish or /cancel "
                "it first. To get a different range, send it with that range."
            )
    return None


def queue_wait_note(task_id) -> str:
    """Extra line for the 'Queued' reply when the same novel is ahead of this task."""
    if not USE_SHARED_TASKS or not task_id:
        return ""
    try:
        _, rows = _pg_exec(
            """
            SELECT o.id, o.status FROM wtrlab_tasks me
            JOIN wtrlab_tasks o
              ON o.novel_key = me.novel_key AND o.id <> me.id
            WHERE me.id = %s AND me.novel_key IS NOT NULL
              AND (
                    o.status = 'running'
                 OR (o.status = 'pending' AND (
                        o.priority > me.priority
                     OR (o.priority = me.priority AND o.id < me.id)))
              )
            ORDER BY (o.status = 'running') DESC, o.id ASC
            LIMIT 1
            """,
            (int(task_id),), fetch=True,
        )
    except Exception as e:
        log.warning("[QUEUE NOTE] %s", e)
        return ""
    if not rows:
        return ""
    other, st = rows[0]
    where = "is downloading now" if st == "running" else "is queued ahead of yours"
    return (
        f"\n\n⏳ Another request for this same novel (task #{other}) {where}. "
        "Yours starts right after it, so the files never clash."
    )


def insert_task(
    chat_id: int,
    user_id: int,
    url: str,
    chapter_range: str,
    username: Optional[str] = None,
    first_name: Optional[str] = None,
    last_name: Optional[str] = None,
    unlimited_cap: bool = False,
    auto_continue: bool = False,
) -> tuple[Optional[int], Optional[str]]:
    """Returns (task_id, error_message). error_message is None on success."""
    if not is_wtr_url(url):
        return None, "Only wtr-lab.com links are supported."

    if is_admin(user_id):
        unlimited_cap = True

    prem = get_premium(user_id) if not unlimited_cap else {"admin": True}
    if prem:
        # Premium: higher chapter cap and PREMIUM_TASKS_PER_DAY tasks/day.
        # Only admins are unlimited.
        unlimited_cap = unlimited_cap or bool(prem.get("admin"))
    premium_limited = bool(prem) and not prem.get("admin")
    task_limit = PREMIUM_TASKS_PER_DAY if premium_limited else DAILY_TASK_LIMIT

    chapter_range = apply_range_cap(chapter_range, unlimited=unlimited_cap or bool(prem))
    dup = duplicate_request_for_user(user_id, url, chapter_range)
    if dup:
        return None, dup

    retry = is_same_link_retry(user_id, url)

    if (
        task_limit > 0
        and not unlimited_cap
        and not retry
        and not auto_continue  # auto-continue resumes an existing novel
    ):
        used = count_recent_tasks(user_id)
        if used >= task_limit:
            return None, (
                f"Daily task limit reached ({task_limit} / 24h).\n"
                "A different novel is blocked while that slot is used.\n"
                "• Resend the same failed/partial link to resume from cache, or\n"
                "• Use /continue to re-queue your latest failed task.\n"
                "Contact admin if you need help or another task."
            )

    queue = "premium" if prem else "normal"
    priority = 10 if prem else 0
    novel_key = extract_novel_id(url) or normalize_novel_url(url)

    if USE_SHARED_TASKS:
        try:
            conn = shared_db()
            try:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        INSERT INTO wtrlab_tasks (
                            chat_id, user_id, username, first_name, last_name,
                            url, chapter_range, status, queue, priority,
                            auto_continue, novel_key, created_at, last_progress_at
                        ) VALUES (
                            %s,%s,%s,%s,%s,%s,%s,'pending',%s,%s,%s,%s,NOW(),NOW()
                        )
                        RETURNING id
                        """,
                        (
                            int(chat_id), int(user_id), username, first_name, last_name,
                            url, chapter_range, queue, priority, bool(auto_continue),
                            novel_key,
                        ),
                    )
                    row = cur.fetchone()
                conn.commit()
                return (int(row[0]) if row else None), None
            finally:
                conn.close()
        except Exception as e:
            log.exception("[INSERT SHARED] %s", e)
            return None, f"Queue error: {e}"

    conn = local_db()
    try:
        cur = conn.execute(
            """
            INSERT INTO tasks (
                chat_id, user_id, username, first_name, last_name,
                url, chapter_range, status
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, 'pending')
            """,
            (
                chat_id, user_id, username, first_name, last_name,
                url, chapter_range,
            ),
        )
        conn.commit()
        return int(cur.lastrowid), None
    finally:
        conn.close()


def claim_task() -> Optional[dict]:
    """
    Shared DB: requeue stale running tasks, then claim by QUEUE_POOL.
    Local SQLite: run_next then oldest pending.
    Stale = running with no progress for 15+ minutes (dead worker / crashed Chrome).
    Chapters already on NFS stay on disk so the next worker resumes from cache.
    """
    recover_paused_tasks()
    if USE_SHARED_TASKS:
        try:
            conn = shared_db()
            try:
                with conn.cursor() as cur:
                    # One claim at a time across ALL servers, so two workers
                    # can't both start different tasks of the same novel.
                    cur.execute("SELECT pg_advisory_xact_lock(7340117)")
                    cur.execute(
                        """
                        UPDATE wtrlab_tasks
                        SET status = 'pending',
                            server_id = NULL,
                            run_id = NULL,
                            last_progress_at = NOW()
                        WHERE status = 'running'
                          AND (
                            last_progress_at IS NULL
                            OR last_progress_at < NOW() - INTERVAL '15 minutes'
                          )
                        """
                    )
                    reaped = cur.rowcount or 0
                    if reaped:
                        print(f"[REAPER] Requeued {reaped} stale running task(s)")
                    if QUEUE_POOL == "premium":
                        qfilter = "AND q.queue = 'premium'"
                    elif QUEUE_POOL == "normal":
                        qfilter = "AND q.queue = 'normal'"
                    else:
                        qfilter = ""
                    cur.execute(
                        f"""
                        UPDATE wtrlab_tasks t
                        SET status = 'running',
                            server_id = %s,
                            run_id = %s,
                            last_progress_at = NOW(),
                            run_next = 0
                        WHERE t.id = (
                            SELECT q.id FROM wtrlab_tasks q
                            WHERE q.status = 'pending' {qfilter}
                              AND (
                                q.novel_key IS NULL
                                OR NOT EXISTS (
                                    SELECT 1 FROM wtrlab_tasks r
                                    WHERE r.status = 'running'
                                      AND r.novel_key = q.novel_key
                                )
                              )
                            ORDER BY
                                COALESCE(q.run_next, 0) DESC,
                                q.priority DESC,
                                q.id ASC
                            FOR UPDATE OF q SKIP LOCKED
                            LIMIT 1
                        )
                        RETURNING *
                        """,
                        (SERVER_ID, secrets.token_hex(8)),
                    )
                    row = cur.fetchone()
                    if not row:
                        conn.commit()
                        heartbeat_once(None)
                        return None
                    cols = [d[0] for d in cur.description]
                    task = dict(zip(cols, row))
                conn.commit()
                heartbeat_once(task.get("id"))
                return task
            finally:
                conn.close()
        except Exception as e:
            log.exception("[CLAIM SHARED] %s", e)
            return None

    conn = local_db()
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            """
            SELECT * FROM tasks
            WHERE status = 'pending'
            ORDER BY COALESCE(run_next, 0) DESC, id ASC
            LIMIT 1
            """
        ).fetchone()
        if not row:
            conn.commit()
            return None
        conn.execute(
            """
            UPDATE tasks
            SET status = 'running',
                last_progress_at = datetime('now'),
                run_next = 0
            WHERE id = ? AND status = 'pending'
            """,
            (row["id"],),
        )
        conn.commit()
        return dict(row)
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _pg_exec(sql: str, params=(), fetch: bool = False):
    """Run one statement on the shared Postgres. Returns (rowcount, rows)."""
    conn = shared_db()
    try:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            rows = cur.fetchall() if fetch else None
            count = cur.rowcount
        conn.commit()
        return count, rows
    finally:
        conn.close()


def resume_own_stale_task() -> Optional[dict]:
    """
    Called once when this worker starts. Any task still marked 'running' on
    THIS server id was interrupted by a stop/crash (nothing of ours can be
    running yet), so take it back and continue it first instead of leaving
    it for the 15-minute reaper while we pick something else.
    Chapters already downloaded are reused from the cache.
    """
    if not USE_SHARED_TASKS:
        return None
    try:
        conn = shared_db()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT id FROM wtrlab_tasks WHERE status = 'running' "
                    "AND server_id = %s ORDER BY id ASC",
                    (SERVER_ID,),
                )
                ids = [r[0] for r in cur.fetchall()]
                if not ids:
                    conn.commit()
                    return None
                if len(ids) > 1:
                    cur.execute(
                        """
                        UPDATE wtrlab_tasks
                        SET status = 'pending', server_id = NULL, run_id = NULL,
                            last_progress_at = NOW()
                        WHERE id = ANY(%s)
                        """,
                        (ids[1:],),
                    )
                cur.execute(
                    """
                    UPDATE wtrlab_tasks
                    SET run_id = %s, last_progress_at = NOW()
                    WHERE id = %s AND status = 'running' AND server_id = %s
                    RETURNING *
                    """,
                    (secrets.token_hex(8), ids[0], SERVER_ID),
                )
                row = cur.fetchone()
                if not row:
                    conn.commit()
                    return None
                cols = [d[0] for d in cur.description]
                task = dict(zip(cols, row))
            conn.commit()
        finally:
            conn.close()
        log.warning(
            "[RESUME] %s was stopped mid-task: continuing task #%s (user=%s) "
            "first%s",
            SERVER_ID, task["id"], task.get("user_id"),
            f"; re-queued {len(ids) - 1} extra" if len(ids) > 1 else "",
        )
        heartbeat_once(task.get("id"))
        return task
    except Exception:
        log.exception("[RESUME] could not check for an interrupted task")
        return None


def recover_paused_tasks() -> int:
    """Admin-paused tasks whose timer expired → pending again."""
    if USE_SHARED_TASKS:
        try:
            n, _ = _pg_exec(
                """
                UPDATE wtrlab_tasks
                SET status = 'pending', paused_until = NULL,
                    last_progress_at = NOW()
                WHERE status = 'paused'
                  AND (paused_until IS NULL OR paused_until <= NOW())
                """
            )
            if n:
                log.info("[PAUSE] Auto-repended %s paused task(s)", n)
            return n or 0
        except Exception as error:
            log.error("[PAUSE] shared recover failed: %s", error)
            return 0
    conn = local_db()
    try:
        cur = conn.execute(
            """
            UPDATE tasks
            SET status = 'pending',
                paused_until = NULL,
                last_progress_at = datetime('now')
            WHERE status = 'paused'
              AND (
                paused_until IS NULL
                OR paused_until <= datetime('now')
              )
            """
        )
        conn.commit()
        n = cur.rowcount
        if n:
            print(f"[PAUSE] Auto-repended {n} paused task(s)")
        return n
    except Exception as error:
        print(f"[PAUSE] recover failed: {error}")
        return 0
    finally:
        conn.close()


def recover_interrupted_tasks():
    conn = local_db()
    try:
        conn.execute(
            """
            UPDATE tasks
            SET status = 'pending'
            WHERE status = 'running'
            """
        )
        conn.commit()
    finally:
        conn.close()


def purge_old_finished_tasks(days: int = 7) -> int:
    """
    Delete finished tasks (done / failed / cancelled / upload_failed) whose
    completed_at (or created_at fallback) is older than *days*.
    Does not touch chapter/novel cache files — only the tasks table rows.
    Returns number of rows deleted.
    """
    conn = local_db()
    try:
        # Prefer completed_at; fall back to created_at when completed_at is null.
        cur = conn.execute(
            """
            DELETE FROM tasks
            WHERE status IN ('done', 'failed', 'cancelled', 'upload_failed')
              AND datetime(COALESCE(completed_at, created_at))
                  < datetime('now', ?)
            """,
            (f"-{int(days)} days",),
        )
        # Also drop orphaned task_cache rows for deleted task ids.
        conn.execute(
            """
            DELETE FROM task_cache
            WHERE task_id NOT IN (SELECT id FROM tasks)
            """
        )
        conn.commit()
        deleted = cur.rowcount if cur.rowcount is not None else 0
        if deleted:
            print(f"[PURGE] Removed {deleted} finished task(s) older than {days} day(s)")
        return int(deleted)
    except Exception as error:
        print(f"[PURGE ERROR] {error}")
        try:
            conn.rollback()
        except Exception:
            pass
        return 0
    finally:
        conn.close()


def update_task_metadata(task_id: int, title: str, total: int):
    if USE_SHARED_TASKS:
        try:
            _pg_exec(
                """
                UPDATE wtrlab_tasks
                SET novel_title = %s, total_chapters = %s,
                    last_progress_at = NOW()
                WHERE id = %s
                """,
                (title, int(total), task_id),
            )
        except Exception as error:
            log.error("[TASK META] shared update failed: %s", error)
        return
    conn = local_db()
    try:
        conn.execute(
            """
            UPDATE tasks
            SET novel_title = ?, total_chapters = ?,
                last_progress_at = datetime('now')
            WHERE id = ?
            """,
            (title, total, task_id),
        )
        conn.commit()
    finally:
        conn.close()


def touch_progress(task_id: int):
    if not task_id:
        return
    if USE_SHARED_TASKS:
        try:
            conn = shared_db()
            try:
                with conn.cursor() as cur:
                    cur.execute(
                        "UPDATE wtrlab_tasks SET last_progress_at = NOW() WHERE id = %s",
                        (task_id,),
                    )
                conn.commit()
            finally:
                conn.close()
        except Exception:
            pass
    try:
        conn = local_db()
        try:
            conn.execute(
                "UPDATE tasks SET last_progress_at = datetime('now') WHERE id = ?",
                (task_id,),
            )
            conn.commit()
        except Exception:
            pass
        finally:
            conn.close()
    except Exception:
        pass


def is_cancelled(task_id: int) -> bool:
    """True if the worker should stop this task (cancelled or admin-paused)."""
    if USE_SHARED_TASKS:
        try:
            _, rows = _pg_exec(
                "SELECT status FROM wtrlab_tasks WHERE id = %s", (task_id,), fetch=True
            )
            return bool(rows and rows[0][0] in ("cancelled", "paused"))
        except Exception as error:
            log.error("[CANCEL CHECK] shared query failed: %s", error)
            return False
    conn = local_db()
    try:
        row = conn.execute(
            "SELECT status FROM tasks WHERE id = ?", (task_id,)
        ).fetchone()
        return bool(row and row["status"] in ("cancelled", "paused"))
    finally:
        conn.close()


def mark_done(task_id: int):
    log.info("[TASK] #%s -> done", task_id)
    if USE_SHARED_TASKS:
        try:
            conn = shared_db()
            try:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        UPDATE wtrlab_tasks
                        SET status = 'done', completed_at = NOW(),
                            server_id = COALESCE(server_id, %s)
                        WHERE id = %s AND status != 'cancelled'
                        """,
                        (SERVER_ID, task_id),
                    )
                conn.commit()
                return
            finally:
                conn.close()
        except Exception as e:
            print(f"[MARK DONE SHARED] {e}")
    conn = local_db()
    try:
        conn.execute(
            """
            UPDATE tasks
            SET status = 'done', completed_at = datetime('now')
            WHERE id = ? AND status != 'cancelled'
            """,
            (task_id,),
        )
        conn.commit()
    finally:
        conn.close()


def mark_failed(task_id: int, error: str = ""):
    log.warning("[TASK] #%s -> failed: %s", task_id, (error or "")[:300])
    if USE_SHARED_TASKS:
        try:
            conn = shared_db()
            try:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        UPDATE wtrlab_tasks
                        SET status = 'failed', error = %s, completed_at = NOW()
                        WHERE id = %s AND status != 'cancelled'
                        """,
                        ((error or "")[:500], task_id),
                    )
                conn.commit()
                return
            finally:
                conn.close()
        except Exception as e:
            print(f"[MARK FAIL SHARED] {e}")
    conn = local_db()
    try:
        conn.execute(
            """
            UPDATE tasks
            SET status = 'failed', error = ?, completed_at = datetime('now')
            WHERE id = ? AND status != 'cancelled'
            """,
            ((error or "")[:500], task_id),
        )
        conn.commit()
    finally:
        conn.close()


def requeue_task(task_id: int):
    log.info("[TASK] #%s -> requeued (pending)", task_id)
    if USE_SHARED_TASKS:
        try:
            conn = shared_db()
            try:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        UPDATE wtrlab_tasks
                        SET status = 'pending',
                            paused_until = NULL,
                            completed_at = NULL,
                            server_id = NULL,
                            run_id = NULL,
                            error = NULL
                        WHERE id = %s AND status NOT IN ('cancelled', 'done')
                        """,
                        (task_id,),
                    )
                conn.commit()
                return
            finally:
                conn.close()
        except Exception as e:
            print(f"[REQUEUE SHARED] {e}")
    conn = local_db()
    try:
        conn.execute(
            """
            UPDATE tasks
            SET status = 'pending',
                paused_until = NULL,
                completed_at = NULL
            WHERE id = ? AND status NOT IN ('cancelled', 'done')
            """,
            (task_id,),
        )
        conn.commit()
    finally:
        conn.close()


def admin_requeue_task(task_id: int) -> tuple[bool, str]:
    """Admin: failed/cancelled/upload_failed/paused → pending."""
    if USE_SHARED_TASKS:
        try:
            conn = shared_db()
            try:
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT id, status FROM wtrlab_tasks WHERE id = %s",
                        (task_id,),
                    )
                    row = cur.fetchone()
                    if not row:
                        return False, f"No task #{task_id}."
                    if row[1] not in ("failed", "upload_failed", "cancelled", "paused"):
                        return False, f"Task #{task_id} is {row[1]}."
                    cur.execute(
                        """
                        UPDATE wtrlab_tasks
                        SET status = 'pending', error = NULL, completed_at = NULL,
                            paused_until = NULL, run_next = 0, server_id = NULL,
                            last_progress_at = NOW()
                        WHERE id = %s
                        """,
                        (task_id,),
                    )
                conn.commit()
                return True, f"Task #{task_id} → pending."
            finally:
                conn.close()
        except Exception as e:
            print(f"[ADMIN REQUEUE SHARED] {e}")
    return _admin_requeue_task_local(task_id)

def _admin_requeue_task_local(task_id: int) -> tuple[bool, str]:
    """Local SQLite fallback."""
    conn = local_db()
    try:
        row = conn.execute("SELECT id, status FROM tasks WHERE id = ?", (task_id,)).fetchone()
        if not row:
            return False, f"No task #{task_id}."
        if row["status"] not in ("failed", "upload_failed", "cancelled", "paused"):
            return False, f"Task #{task_id} is {row['status']} (need failed/cancelled/paused)."
        conn.execute(
            """
            UPDATE tasks
            SET status = 'pending',
                error = NULL,
                completed_at = NULL,
                paused_until = NULL,
                run_next = 0,
                last_progress_at = datetime('now')
            WHERE id = ?
            """,
            (task_id,),
        )
        conn.commit()
        return True, f"Task #{task_id} → pending."
    finally:
        conn.close()

def admin_pause_task(task_id: int) -> tuple[bool, str]:
    """Admin: running/pending → paused; auto-pending after PAUSE_REPEND_SECONDS."""
    until = time.strftime(
        "%Y-%m-%d %H:%M:%S",
        time.gmtime(time.time() + PAUSE_REPEND_SECONDS),
    )
    if USE_SHARED_TASKS:
        _, rows = _pg_exec(
            "SELECT status FROM wtrlab_tasks WHERE id = %s", (task_id,), fetch=True
        )
        if not rows:
            return False, f"No task #{task_id}."
        status = rows[0][0]
        if status not in ("running", "pending"):
            return False, f"Task #{task_id} is {status} (need running/pending)."
        _pg_exec(
            """
            UPDATE wtrlab_tasks
            SET status = 'paused',
                paused_until = NOW() + (%s || ' seconds')::interval,
                last_progress_at = NOW()
            WHERE id = %s
            """,
            (str(PAUSE_REPEND_SECONDS), task_id),
        )
        mins = PAUSE_REPEND_SECONDS // 60
        return (
            True,
            f"Task #{task_id} paused ({status}→paused). "
            f"Auto-pending in ~{mins} min if you do not /continue it sooner.",
        )
    # SQLite datetime('now') is UTC when using default; use same style via SQL.
    conn = local_db()
    try:
        row = conn.execute("SELECT id, status FROM tasks WHERE id = ?", (task_id,)).fetchone()
        if not row:
            return False, f"No task #{task_id}."
        if row["status"] not in ("running", "pending"):
            return False, f"Task #{task_id} is {row['status']} (need running/pending)."
        conn.execute(
            """
            UPDATE tasks
            SET status = 'paused',
                paused_until = datetime('now', ?),
                last_progress_at = datetime('now')
            WHERE id = ?
            """,
            (f"+{PAUSE_REPEND_SECONDS} seconds", task_id),
        )
        conn.commit()
        mins = PAUSE_REPEND_SECONDS // 60
        return (
            True,
            f"Task #{task_id} paused ({row['status']}→paused). "
            f"Auto-pending in ~{mins} min if you do not /continue it sooner.",
        )
    finally:
        conn.close()


def admin_cancel_task(task_id: int) -> tuple[bool, str]:
    if USE_SHARED_TASKS:
        _, rows = _pg_exec(
            "SELECT status FROM wtrlab_tasks WHERE id = %s", (task_id,), fetch=True
        )
        if not rows:
            return False, f"No task #{task_id}."
        if rows[0][0] not in ("pending", "running", "paused"):
            return False, f"Task #{task_id} is {rows[0][0]} (need pending/running/paused)."
        _pg_exec(
            """
            UPDATE wtrlab_tasks
            SET status = 'cancelled', completed_at = NOW(),
                paused_until = NULL, run_next = 0
            WHERE id = %s
            """,
            (task_id,),
        )
        return True, f"Task #{task_id} cancelled."
    conn = local_db()
    try:
        row = conn.execute("SELECT id, status FROM tasks WHERE id = ?", (task_id,)).fetchone()
        if not row:
            return False, f"No task #{task_id}."
        if row["status"] not in ("pending", "running", "paused"):
            return False, f"Task #{task_id} is {row['status']} (need pending/running/paused)."
        conn.execute(
            """
            UPDATE tasks
            SET status = 'cancelled',
                completed_at = datetime('now'),
                paused_until = NULL,
                run_next = 0
            WHERE id = ?
            """,
            (task_id,),
        )
        conn.commit()
        return True, f"Task #{task_id} cancelled."
    finally:
        conn.close()


def admin_start_task(task_id: int) -> tuple[bool, str]:
    """One-shot next claim: pending (or requeue failed) with run_next=1."""
    if USE_SHARED_TASKS:
        _, rows = _pg_exec(
            "SELECT status FROM wtrlab_tasks WHERE id = %s", (task_id,), fetch=True
        )
        if not rows:
            return False, f"No task #{task_id}."
        st = rows[0][0]
        if st in ("failed", "upload_failed", "cancelled", "paused"):
            _pg_exec(
                """
                UPDATE wtrlab_tasks
                SET status = 'pending', error = NULL, completed_at = NULL,
                    paused_until = NULL, run_next = 1, server_id = NULL,
                    last_progress_at = NOW()
                WHERE id = %s
                """,
                (task_id,),
            )
        elif st == "pending":
            _pg_exec(
                "UPDATE wtrlab_tasks SET run_next = 1, last_progress_at = NOW() WHERE id = %s",
                (task_id,),
            )
        elif st == "running":
            return False, f"Task #{task_id} is already running."
        else:
            return False, f"Task #{task_id} is {st}."
        _pg_exec(
            "UPDATE wtrlab_tasks SET run_next = 0 WHERE id != %s AND COALESCE(run_next, 0) != 0",
            (task_id,),
        )
        return (
            True,
            f"Task #{task_id} marked next in line (one-shot). "
            "Pause the current crawl with /pause &lt;id&gt; if you need it to start right after.",
        )
    conn = local_db()
    try:
        row = conn.execute(
            "SELECT id, status FROM tasks WHERE id = ?", (task_id,)
        ).fetchone()
        if not row:
            return False, f"No task #{task_id}."
        st = row["status"]
        if st in ("failed", "upload_failed", "cancelled", "paused"):
            conn.execute(
                """
                UPDATE tasks
                SET status = 'pending',
                    error = NULL,
                    completed_at = NULL,
                    paused_until = NULL,
                    run_next = 1,
                    last_progress_at = datetime('now')
                WHERE id = ?
                """,
                (task_id,),
            )
        elif st == "pending":
            conn.execute(
                "UPDATE tasks SET run_next = 1, last_progress_at = datetime('now') WHERE id = ?",
                (task_id,),
            )
        elif st == "running":
            return False, f"Task #{task_id} is already running."
        else:
            return False, f"Task #{task_id} is {st}."
        # Clear other run_next so only one boost exists
        conn.execute(
            "UPDATE tasks SET run_next = 0 WHERE id != ? AND COALESCE(run_next, 0) != 0",
            (task_id,),
        )
        conn.commit()
        return (
            True,
            f"Task #{task_id} marked next in line (one-shot). "
            "Pause the current crawl with /pause &lt;id&gt; if you need it to start right after.",
        )
    finally:
        conn.close()


def mark_upload_failed(task_id: int):
    log.warning("[TASK] #%s -> upload_failed", task_id)
    if USE_SHARED_TASKS:
        try:
            conn = shared_db()
            try:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        UPDATE wtrlab_tasks
                        SET status = 'upload_failed', completed_at = NOW(),
                            server_id = COALESCE(server_id, %s)
                        WHERE id = %s AND status != 'cancelled'
                        """,
                        (SERVER_ID, task_id),
                    )
                conn.commit()
                return
            finally:
                conn.close()
        except Exception as e:
            print(f"[MARK UPLOAD FAILED SHARED] {e}")
    conn = local_db()
    try:
        conn.execute(
            """
            UPDATE tasks
            SET status = 'upload_failed', completed_at = datetime('now')
            WHERE id = ? AND status != 'cancelled'
            """,
            (task_id,),
        )
        conn.commit()
    finally:
        conn.close()


def cancel_user_tasks(user_id: int) -> int:
    if USE_SHARED_TASKS:
        try:
            n, _ = _pg_exec(
                """
                UPDATE wtrlab_tasks
                SET status = 'cancelled', completed_at = NOW(),
                    run_next = 0, paused_until = NULL
                WHERE user_id = %s AND status IN ('pending', 'running', 'paused')
                """,
                (int(user_id),),
            )
            return n or 0
        except Exception as error:
            log.error("[CANCEL] shared cancel failed: %s", error)
            return 0
    conn = local_db()
    try:
        cur = conn.execute(
            """
            UPDATE tasks
            SET status = 'cancelled', completed_at = datetime('now'),
                run_next = 0, paused_until = NULL
            WHERE user_id = ? AND status IN ('pending', 'running', 'paused')
            """,
            (user_id,),
        )
        conn.commit()
        return cur.rowcount
    finally:
        conn.close()


def session_start(user_id: int, **fields: Any) -> None:
    state = dict(fields)
    state["started_at"] = time.time()
    pending_download[user_id] = state


def session_get(user_id: int) -> Optional[dict[str, Any]]:
    """Return pending chat state, or None if missing. Does not clear expiry."""
    return pending_download.get(user_id)


def session_expired(state: Optional[dict[str, Any]]) -> bool:
    if not state:
        return False
    started = float(state.get("started_at") or 0)
    return bool(started and (time.time() - started) > SESSION_SECONDS)


def session_touch(user_id: int) -> None:
    state = pending_download.get(user_id)
    if state is not None:
        state["started_at"] = time.time()


def write_dotenv_key(key: str, value: str) -> None:
    """Create or replace KEY=value in BASE_DIR/.env (key stored UPPERCASE)."""
    key = key.strip().upper()
    path = BASE_DIR / ".env"
    lines: list[str] = []
    if path.is_file():
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    out: list[str] = []
    found = False
    prefix = f"{key}="
    for line in lines:
        stripped = line.strip()
        if stripped.startswith(prefix) or stripped.startswith(f"export {prefix}"):
            out.append(f"{key}={value}")
            found = True
        else:
            out.append(line)
    if not found:
        if out and out[-1].strip():
            out.append("")
        out.append(f"{key}={value}")
    path.write_text("\n".join(out) + "\n", encoding="utf-8")


def read_dotenv_key(key: str) -> str:
    key = key.strip().upper()
    path = BASE_DIR / ".env"
    if not path.is_file():
        return os.environ.get(key, "")
    prefix = f"{key}="
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        s = line.strip()
        if s.startswith("export "):
            s = s[7:].strip()
        if s.startswith(prefix):
            return s[len(prefix):].strip().strip('"').strip("'")
    return os.environ.get(key, "")


def apply_config_live(key: str, value: str) -> str:
    """
    Update process state for KEY. Returns 'live' or 'restart'
    (restart = written to .env but process needs /restart).
    """
    global ALLOWED_USER_IDS, ADMIN_USER_IDS, ADMIN_CHAT_ID, OUTPUT_GROUPS
    global DEFAULT_CHAPTER_CAP, DAILY_TASK_LIMIT
    global CHAPTER_THROTTLE_MIN, CHAPTER_THROTTLE_MAX
    global HEADLESS, REMOTE_SOLVE, REMOTE_LINK_MAX_MINUTES
    global VNC_PORT, NOVNC_PORT, ACTIVITY_DEEP, CHROME_APP_MODE

    key = key.strip().upper()
    os.environ[key] = value

    if key == "ALLOWED_USER_IDS":
        ALLOWED_USER_IDS = parse_id_list(value)
        return "live"
    if key == "ADMIN_USER_IDS":
        ADMIN_USER_IDS = parse_id_list(value)
        if ADMIN_CHAT_ID and ADMIN_CHAT_ID.lstrip("-").isdigit():
            ADMIN_USER_IDS.add(int(ADMIN_CHAT_ID))
        return "live"
    if key == "ADMIN_CHAT_ID":
        ADMIN_CHAT_ID = value.strip()
        if ADMIN_CHAT_ID.lstrip("-").isdigit():
            ADMIN_USER_IDS.add(int(ADMIN_CHAT_ID))
        return "live"
    if key == "OUTPUT_GROUPS":
        OUTPUT_GROUPS = [x.strip() for x in value.split(",") if x.strip()]
        return "live"
    if key == "CHAPTER_CAP":
        DEFAULT_CHAPTER_CAP = int(value) if str(value).strip().isdigit() else 0
        try:
            conn = local_db()
            conn.execute(
                """
                INSERT INTO settings(key, value) VALUES ('chapter_cap', ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
                """,
                (str(DEFAULT_CHAPTER_CAP),),
            )
            conn.commit()
            conn.close()
        except Exception as error:
            print(f"[SET] chapter_cap db: {error}")
        return "live"
    if key == "DAILY_TASK_LIMIT":
        DAILY_TASK_LIMIT = int(value) if str(value).strip().lstrip("-").isdigit() else 0
        return "live"
    if key == "CHAPTER_THROTTLE_MIN":
        CHAPTER_THROTTLE_MIN = max(0.0, float(value or 0))
        return "live"
    if key == "CHAPTER_THROTTLE_MAX":
        CHAPTER_THROTTLE_MAX = max(CHAPTER_THROTTLE_MIN, float(value or 0))
        return "live"
    if key == "REMOTE_LINK_MAX_MINUTES":
        try:
            REMOTE_LINK_MAX_MINUTES = max(0, int(float(value or 0)))
        except ValueError:
            REMOTE_LINK_MAX_MINUTES = 0
        return "live"
    if key in ("REMOTE_SOLVE", "ACTIVITY_DEEP", "CHROME_APP_MODE"):
        flag = str(value).strip().lower() in _TRUE_WORDS
        if key == "REMOTE_SOLVE":
            REMOTE_SOLVE = flag
        elif key == "ACTIVITY_DEEP":
            ACTIVITY_DEEP = flag
        else:
            CHROME_APP_MODE = flag
        return "live"
    if key in ("VNC_PORT", "NOVNC_PORT"):
        try:
            n = int(value)
        except ValueError:
            n = 5901 if key == "VNC_PORT" else 6080
        if key == "VNC_PORT":
            VNC_PORT = n
        else:
            NOVNC_PORT = n
        return "live"
    if key in (
        "BOT_TOKEN",
        "HEADLESS",
        "CHROME_PROFILE_DIR",
        "CLOUDFLARED_BIN",
        "NOVNC_WEB_DIR",
    ):
        return "restart"
    return "restart"


RESTART_HELPER_NAME = "restart-bot.sh"
RESTART_HELPER_BODY = r'''#!/bin/bash
# Written by app.py /restart — do not run by hand unless you know why.
set -e
cd "$(dirname "$0")"
echo "[restart-bot] killing app + chrome stack…"
pkill -9 -f 'app\.py|uc_driver|google-chrome|chrome|chromedriver' 2>/dev/null || true
sleep 2
pkill -9 -f 'app\.py|uc_driver|google-chrome|chrome|chromedriver' 2>/dev/null || true
echo "[restart-bot] waiting 20s then Start for Linux.sh…"
sleep 20
echo "[restart-bot] starting Start for Linux.sh"
if [ -f "./Start for Linux.sh" ]; then
  exec bash "./Start for Linux.sh"
else
  echo "[restart-bot] ERROR: Start for Linux.sh not found in $(pwd)"
  exit 1
fi
'''


def ensure_restart_helper() -> Path:
    path = BASE_DIR / RESTART_HELPER_NAME
    path.write_text(RESTART_HELPER_BODY, encoding="utf-8")
    path.chmod(0o755)
    return path


def list_user_queue(user_id: int) -> list[dict]:
    conn = local_db()
    try:
        rows = conn.execute(
            """
            SELECT id, status, url, chapter_range, novel_title, created_at
            FROM tasks
            WHERE user_id = ? AND status IN ('pending', 'running')
            ORDER BY id ASC
            """,
            (user_id,),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()

# ---------------------------------------------------------------------------
# Chapter / novel cache helpers (same SQLite file as the task queue)
# ---------------------------------------------------------------------------

def _chapter_file_ok(path: Path, chapter_no: int) -> bool:
    try:
        if not path.is_file():
            return False
        if chapter_cache_is_bad(path):
            print(
                f"[CACHE] Re-download chapter {chapter_no}: "
                "empty or unusable file"
            )
            return False
        text = path.read_text(encoding="utf-8", errors="replace")
        if xhtml_has_unresolved_placeholders(text):
            print(
                f"[CACHE] Re-download chapter {chapter_no}: "
                "unresolved glossary placeholders in cached XHTML"
            )
            return False
        return True
    except Exception:
        return False


def cache_has_chapter(novel_id: str, chapter_no: int) -> bool:
    # Prefer shared index; also accept file on NFS even if index lags briefly.
    disk_path = LIBRARY_DIR / str(novel_id) / "chapters" / f"{int(chapter_no):05}.xhtml"
    xhtml_path = None
    if use_chapter_pg():
        try:
            conn = chapter_db()
            try:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        SELECT xhtml_path FROM chapters
                        WHERE novel_id=%s AND chapter_no=%s
                        """,
                        (str(novel_id), int(chapter_no)),
                    )
                    row = cur.fetchone()
                    if row:
                        xhtml_path = row[0]
            finally:
                conn.close()
        except Exception as error:
            print(f"[CACHE HAS PG] {error}")
    else:
        conn = local_db()
        try:
            row = conn.execute(
                """
                SELECT xhtml_path FROM chapters
                WHERE novel_id=? AND chapter_no=?
                """,
                (novel_id, chapter_no),
            ).fetchone()
            if row:
                xhtml_path = row["xhtml_path"]
        finally:
            conn.close()

    if xhtml_path:
        path = Path(xhtml_path)
        if _chapter_file_ok(path, int(chapter_no)):
            return True
    # NFS file present but index missing (another worker just wrote it)
    if disk_path.is_file() and _chapter_file_ok(disk_path, int(chapter_no)):
        return True
    return False


def cache_chapter(
    novel_id: str,
    chapter_no: int,
    chapter_id: str,
    title: str,
    file_path: Path,
):
    path_s = str(file_path)
    if use_chapter_pg():
        try:
            conn = chapter_db()
            try:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        INSERT INTO chapters(
                            novel_id, chapter_no, chapter_id, title, xhtml_path, downloaded_at
                        ) VALUES (%s,%s,%s,%s,%s,NOW())
                        ON CONFLICT (novel_id, chapter_no) DO UPDATE SET
                            chapter_id = EXCLUDED.chapter_id,
                            title = EXCLUDED.title,
                            xhtml_path = EXCLUDED.xhtml_path,
                            downloaded_at = NOW()
                        """,
                        (str(novel_id), int(chapter_no), chapter_id, title, path_s),
                    )
                conn.commit()
                return
            finally:
                conn.close()
        except Exception as error:
            print(f"[CACHE CHAPTER PG] {error}")
    conn = local_db()
    try:
        conn.execute(
            """
            INSERT INTO chapters(novel_id, chapter_no, chapter_id, title, xhtml_path)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(novel_id, chapter_no)
            DO UPDATE SET
                chapter_id=excluded.chapter_id,
                title=excluded.title,
                xhtml_path=excluded.xhtml_path,
                downloaded_at=CURRENT_TIMESTAMP;
            """,
            (novel_id, chapter_no, chapter_id, title, path_s),
        )
        conn.commit()
    finally:
        conn.close()


def save_task_cache(task_id: int, novel_id: str, epub_path: Path, display_title: str):
    conn = local_db()
    try:
        conn.execute(
            """
            INSERT INTO task_cache(task_id, novel_id, epub_path, display_title)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(task_id)
            DO UPDATE SET
                novel_id=excluded.novel_id,
                epub_path=excluded.epub_path,
                display_title=excluded.display_title,
                updated_at=CURRENT_TIMESTAMP;
            """,
            (task_id, novel_id, str(epub_path), display_title),
        )
        conn.commit()
    finally:
        conn.close()


def atomic_write_text(path: Path, content: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(content, encoding="utf-8")
    temp.replace(path)


def atomic_write_bytes(path: Path, content: bytes):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_bytes(content)
    temp.replace(path)


def release_stale_chrome(reason: str = "") -> None:
    """
    SeleniumBase copies chromedriver → uc_driver.exe. If a previous worker
    left uc_driver/chrome running, Windows raises WinError 32 (file in use).
    Kill only this worker's Chrome (chrome-profile) plus uc_driver.exe.
    """
    if reason:
        print(f"[BROWSER] Releasing stale Chrome/uc_driver. {reason}")
    try:
        if sys.platform == "win32":
            subprocess.run(
                ["taskkill", "/F", "/IM", "uc_driver.exe"],
                capture_output=True,
                timeout=20,
            )
            profile = str(CHROME_PROFILE_DIR.resolve())
            ps = (
                "$hint = '" + profile.replace("'", "''") + "'; "
                "Get-CimInstance Win32_Process -Filter \"Name='chrome.exe'\" | "
                "ForEach-Object { "
                "  if ($_.CommandLine -and $_.CommandLine.Contains($hint)) { "
                "    Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue "
                "  } "
                "}"
            )
            subprocess.run(
                ["powershell", "-NoProfile", "-Command", ps],
                capture_output=True,
                timeout=30,
            )
        else:
            subprocess.run(["pkill", "-f", "uc_driver"], capture_output=True, timeout=10)
    except Exception as error:
        print(f"[BROWSER] Stale-process cleanup: {error}")
    time.sleep(1.5)


# ---------------------------------------------------------------------------
# Chrome via SeleniumBase UC Mode (headless by default)
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# One-time remote-solve link + activity log
# ---------------------------------------------------------------------------

_ACTIVITY_JS = r"""
(function(){
  try {
    if (!window.__wtrAct) {
      window.__wtrAct = [];
      var push = function(t, d){ try { window.__wtrAct.push([t, String(d).slice(0, 120)]); } catch(e){} };
      document.addEventListener('keydown', function(e){
        var k = e.key || '';
        var combo = (e.ctrlKey?'Ctrl+':'')+(e.altKey?'Alt+':'')+(e.shiftKey?'Shift+':'')+(e.metaKey?'Meta+':'')+k;
        if (e.ctrlKey || e.altKey || e.metaKey || /^F\d+$/.test(k)) push('key', combo);
      }, true);
      document.addEventListener('click', function(e){
        var t = e.target;
        var el = (t && t.closest) ? (t.closest('a,button,input,select,textarea,[role=button]') || t) : t;
        var label = '';
        try { label = (el.innerText || el.value || el.getAttribute('aria-label') || el.id || '') + ''; } catch(x){}
        push('click', (el && el.tagName ? el.tagName.toLowerCase() : '?') + ' "' + label.trim().slice(0, 60) + '"');
      }, true);
      document.addEventListener('contextmenu', function(){ push('rightclick', 'context menu'); }, true);
    }
    var nav = (performance.getEntriesByType('navigation') || [])[0];
    var out = window.__wtrAct.splice(0, 50);
    return {events: out, origin: performance.timeOrigin, navType: nav ? nav.type : ''};
  } catch (e) { return {events: [], origin: 0, navType: ''}; }
})();
"""


def _port_open(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=1):
            return True
    except OSError:
        return False


def _strip_url(url: str) -> str:
    """Scheme + host + path only (no query/fragment, which can hold tokens)."""
    try:
        parts = urllib.parse.urlsplit(url or "")
        return urllib.parse.urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))
    except Exception:
        return (url or "")[:200]


class RemoteSolve:
    """
    A single-use link to the Chrome window: noVNC (websockify) behind a
    cloudflared quick tunnel with a random trycloudflare.com address. The link
    stops working when stop() kills the tunnel. While it is open, poll() logs
    what the person does in the browser (tabs, navigation, refreshes, key
    combos, clicks, DevTools, login screen, Chrome closing) to user_actions.
    """

    def __init__(self, browser, *, task_id, user_id, allowed_url: str, enforce: bool = True):
        self.browser = browser
        self.enforce = enforce
        self.task_id = task_id
        self.user_id = user_id
        self.allowed_url = allowed_url
        self.session_id = secrets.token_hex(4)
        self.username = username_for(user_id)
        self.procs: list = []
        self.started_at = time.time()
        self.expires_at: Optional[float] = (
            self.started_at + REMOTE_LINK_MAX_MINUTES * 60
            if REMOTE_LINK_MAX_MINUTES > 0
            else None
        )
        self.stopped = False
        self.event_count = 0
        self.url: Optional[str] = None
        self._last_url: Optional[str] = None
        self._last_origin: Any = None
        self._seen_targets: set = set()
        self._login_flagged = False

    # -- logging ----------------------------------------------------------
    def log(self, kind: str, detail: str = "") -> None:
        self.event_count += 1
        log_action(self.session_id, self.task_id, self.user_id, self.username, kind, detail)

    # -- lifecycle --------------------------------------------------------
    def start(self) -> Optional[str]:
        try:
            cloudflared = shutil.which(CLOUDFLARED_BIN)
            if not cloudflared:
                print(f"[REMOTE] cloudflared not found ({CLOUDFLARED_BIN})")
                return None
            if not (Path(NOVNC_WEB_DIR) / "vnc.html").is_file():
                print(f"[REMOTE] noVNC not found in {NOVNC_WEB_DIR} (set NOVNC_WEB_DIR)")
                return None
            if not _port_open(VNC_PORT):
                print(f"[REMOTE] No VNC server listening on 127.0.0.1:{VNC_PORT}")
                return None

            if not _port_open(NOVNC_PORT):
                # Start websockify once and leave it running. Do NOT put it in
                # self.procs — stop() only kills the cloudflared tunnel so the
                # next captcha can reuse the same websockify.
                # -u = unbuffered logs; start_new_session = survive parent signals.
                log_path = DATA_DIR / "websockify.log"
                DATA_DIR.mkdir(parents=True, exist_ok=True)
                command = [
                    sys.executable, "-u", "-m", "websockify",
                    "--web", NOVNC_WEB_DIR,
                    f"127.0.0.1:{NOVNC_PORT}",
                    f"127.0.0.1:{VNC_PORT}",
                ]
                print(f"[REMOTE] starting websockify: {' '.join(command)}")
                print(f"[REMOTE] websockify log: {log_path}")
                try:
                    log_fh = open(log_path, "a", encoding="utf-8")
                    log_fh.write(f"\n--- start {time.strftime('%Y-%m-%d %H:%M:%S')} ---\n")
                    log_fh.flush()
                except Exception as error:
                    print(f"[REMOTE] could not open websockify log: {error}")
                    log_fh = subprocess.DEVNULL
                proc = subprocess.Popen(
                    command,
                    stdout=log_fh,
                    stderr=subprocess.STDOUT,
                    start_new_session=True,
                )
                # Poll up to ~10s for the listen port.
                ready = False
                for i in range(20):
                    time.sleep(0.5)
                    if proc.poll() is not None:
                        print(f"[REMOTE] websockify exited early (code={proc.poll()})")
                        break
                    if _port_open(NOVNC_PORT):
                        ready = True
                        break
                    if i in (3, 9, 15):
                        print(f"[REMOTE] waiting for :{NOVNC_PORT} … ({(i + 1) * 0.5:.0f}s)")
                if not ready:
                    out = ""
                    try:
                        if log_path.is_file():
                            out = log_path.read_text(encoding="utf-8", errors="replace")[-3000:]
                    except Exception:
                        pass
                    print(
                        f"[REMOTE] websockify failed to start "
                        f"(exit={proc.poll()}, port_open={_port_open(NOVNC_PORT)})\n"
                        f"{out or '(no output in log)'}"
                    )
                    try:
                        if proc.poll() is None:
                            proc.terminate()
                    except Exception:
                        pass
                    self.stop("websockify failed")
                    return None
                print(f"[REMOTE] websockify listening on 127.0.0.1:{NOVNC_PORT}")

            tunnel = subprocess.Popen(
                [cloudflared, "tunnel", "--no-autoupdate", "--url",
                 f"http://127.0.0.1:{NOVNC_PORT}"],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
            )
            self.procs.append(tunnel)
            found: "queue.Queue[str]" = queue.Queue()

            def reader():
                try:
                    for line in tunnel.stdout:
                        match = re.search(r"https://[a-z0-9-]+\.trycloudflare\.com", line)
                        if match:
                            found.put(match.group(0))
                except Exception:
                    pass

            threading.Thread(target=reader, daemon=True).start()
            base = found.get(timeout=30)
        except Exception as error:
            print(f"[REMOTE] could not start link: {type(error).__name__}: {error}")
            self.stop("start failed")
            return None

        self.url = f"{base}/vnc.html?autoconnect=true&resize=scale&reconnect=true"
        self._baseline()
        self.log("session_start", "one-time link issued")
        return self.url

    def stop(self, reason: str) -> None:
        if self.stopped:
            return
        self.stopped = True
        for proc in reversed(self.procs):
            try:
                proc.terminate()
                try:
                    proc.wait(timeout=4)
                except Exception:
                    proc.kill()
            except Exception:
                pass
        self.procs.clear()
        if self.url:
            seconds = int(time.time() - self.started_at)
            self.log("session_end", f"link closed ({reason}) after {seconds}s")
            print(f"[REMOTE] link closed ({reason})")

    def expired(self) -> bool:
        return self.expires_at is not None and time.time() >= self.expires_at

    # -- monitoring -------------------------------------------------------
    def _baseline(self) -> None:
        try:
            self._last_url = _strip_url(self.browser.driver.current_url or "")
        except Exception:
            self._last_url = None
        if ACTIVITY_DEEP:
            try:
                for target in self.browser._cdp("Target.getTargets", {}).get("targetInfos", []):
                    self._seen_targets.add(target.get("targetId"))
            except Exception:
                pass

    def _host_allowed(self, url: str) -> bool:
        if url.startswith(("about:blank", "data:")):
            return True
        host = (urllib.parse.urlsplit(url).hostname or "").lower()
        return host.endswith("wtr-lab.com") or host.endswith("cloudflare.com")

    def poll(self) -> None:
        browser = self.browser
        if not browser.is_alive():
            self.log("chrome_closed", "Chrome window was closed or crashed")
            raise DeadBrowser("Chrome closed during a remote-solve session")
        driver = browser.driver

        try:
            handles = list(driver.window_handles)
            if len(handles) > 1:
                self.log(
                    "new_tab",
                    f"opened {len(handles) - 1} extra tab/window"
                    + (" (closed)" if self.enforce else ""),
                )
                if self.enforce:
                    browser._keep_one_tab()

            current = driver.current_url or ""
            clean = _strip_url(current)
            if clean != self._last_url:
                self._last_url = clean
                self.log("navigated", clean)
                if not self._host_allowed(current):
                    self.log("off_site", f"left wtr-lab.com: {clean}")
                    try:
                        if not self.enforce:
                            raise RuntimeError("admin session: not redirected")
                        browser.open(self.allowed_url)
                        self._last_url = _strip_url(self.allowed_url)
                    except Exception as error:
                        if is_dead_session(error):
                            raise DeadBrowser(str(error)) from error

            on_login = browser.is_login_page()
            if on_login and not self._login_flagged:
                self.log("login_screen", "page shows the login screen (possible logout)")
            self._login_flagged = on_login

            if ACTIVITY_DEEP:
                result = driver.execute_script(_ACTIVITY_JS) or {}
                for kind, detail in result.get("events", []):
                    self.log(str(kind), str(detail))
                origin = result.get("origin")
                if self._last_origin is not None and origin != self._last_origin:
                    if result.get("navType") == "reload":
                        self.log("reload", "page refreshed")
                    else:
                        self.log("page_loaded", f"page loaded again ({result.get('navType') or 'navigate'})")
                self._last_origin = origin

                for target in browser._cdp("Target.getTargets", {}).get("targetInfos", []):
                    target_id = target.get("targetId")
                    if target_id in self._seen_targets:
                        continue
                    self._seen_targets.add(target_id)
                    target_url = (target.get("url") or "")
                    if target_url.startswith("devtools://"):
                        self.log("devtools", "opened Chrome DevTools")
                        try:
                            if not self.enforce:
                                raise RuntimeError("admin session: not closed")
                            browser._cdp("Target.closeTarget", {"targetId": target_id})
                        except Exception:
                            pass
                    elif target_url.startswith(("chrome://", "chrome-extension://", "view-source:")):
                        self.log("internal_page", _strip_url(target_url)[:120])
        except DeadBrowser:
            raise
        except Exception as error:
            if is_dead_session(error):
                self.log("chrome_closed", "Chrome window was closed or crashed")
                raise DeadBrowser(str(error)) from error
            print(f"[REMOTE] monitor error: {type(error).__name__}: {error}")


class WtrBrowser:
    """
    Chrome via SeleniumBase UC Mode, tuned for low RAM.
    HEADLESS=1: no window (not recommended — admin cannot solve Turnstile over VNC).
    HEADLESS=0 (default): visible window for manual Turnstile solving over VNC.
    Auto-click is never used; challenges wait for admin + /solved or 10-min recheck.
    """

    def __init__(self):
        self.driver = None
        self.on_status = None
        self._spawn()

    def _spawn(self):
        kwargs = {
            "uc": True,
            "user_data_dir": str(CHROME_PROFILE_DIR),
            "block_images": bool(HEADLESS),  # images ON when headed (captcha)
            "disable_gpu": True,
            "chromium_arg": LOW_RAM_CHROME_ARGS,
            "pls": "eager",
        }
        if HEADLESS:
            kwargs["headless2"] = True
            kwargs["headed"] = False
        else:
            kwargs["headed"] = True
        print(
            f"[BROWSER] Starting Chrome "
            f"({'headless2, low-RAM, no Turnstile' if HEADLESS else 'headed / visible window'})"
        )
        last_error: Optional[BaseException] = None
        for attempt in range(1, 4):
            if attempt == 1:
                release_stale_chrome()
            try:
                try:
                    self.driver = Driver(**kwargs)
                except TypeError:
                    kwargs.pop("disable_gpu", None)
                    kwargs.pop("pls", None)
                    self.driver = Driver(**kwargs)
                last_error = None
                break
            except PermissionError as error:
                last_error = error
                print(
                    f"[BROWSER] Driver file locked (attempt {attempt}/3): {error}"
                )
                release_stale_chrome("WinError 32 — uc_driver still in use")
                time.sleep(2 * attempt)
            except Exception as error:
                text = str(error).lower()
                if "being used by another process" in text or "winerror 32" in text:
                    last_error = error
                    print(
                        f"[BROWSER] Driver file locked (attempt {attempt}/3): {error}"
                    )
                    release_stale_chrome("driver copy locked")
                    time.sleep(2 * attempt)
                    continue
                raise
        if last_error:
            raise last_error
        self.driver.set_page_load_timeout(180)
        self.driver.set_script_timeout(180)
        self._keep_one_tab()

    def close(self):
        try:
            if self.driver:
                self.driver.quit()
        except Exception:
            pass
        self.driver = None
        time.sleep(0.8)
        release_stale_chrome()

    def recreate(self, reason: str = ""):
        print(f"[BROWSER] Recreating Chrome. {reason}".strip())
        self.close()
        time.sleep(2)
        self._spawn()
        try:
            self.open("https://wtr-lab.com/")
        except Exception as error:
            print(f"[BROWSER] Reopen after recreate failed: {error}")

    def is_alive(self) -> bool:
        try:
            return bool(self.driver and self.driver.current_window_handle)
        except Exception:
            return False

    def _notify(self, kind: str, detail: str = ""):
        callback = self.on_status
        if callback:
            try:
                callback(kind, detail)
            except Exception:
                pass

    def _keep_one_tab(self):
        """Close leftover UC / Turnstile tabs so Chrome does not keep extra renderers."""
        try:
            handles = list(self.driver.window_handles)
            if len(handles) <= 1:
                return
            keep = self.driver.current_window_handle
            for handle in handles:
                if handle == keep:
                    continue
                try:
                    self.driver.switch_to.window(handle)
                    self.driver.close()
                except Exception:
                    pass
            remaining = list(self.driver.window_handles)
            if remaining:
                target = keep if keep in remaining else remaining[0]
                self.driver.switch_to.window(target)
        except Exception:
            pass

    def open(self, url: str):
        if HEADLESS:
            # uc_open_with_reconnect opens extra tabs (the RAM spike). Skip it.
            self.driver.get(url)
            self._keep_one_tab()
            return
        # Headed: reconnect helper is fine; do NOT auto-click Turnstile (crashes
        # Chrome on headless Linux / VNC setups without a real window manager).
        self.driver.uc_open_with_reconnect(url, 4)
        self._keep_one_tab()

    def html(self) -> str:
        return self.driver.get_page_source()

    def is_login_page(self) -> bool:
        try:
            src = (self.html() or "").lower()
            title = (self.driver.get_title() or "").lower()
            try:
                url = (self.driver.current_url or "").lower()
            except Exception:
                url = ""
            return (
                "continue with email" in src
                or "welcome to wtr-lab" in src
                or "sign in to continue" in src
                or "sign in with email" in src
                or "log in to continue" in src
                or "create an account" in src and "email" in src
                or ("login" in title and "wtr" in title)
                or "/login" in url
                or "/signin" in url
                or "/sign-in" in url
                or "/auth" in url
            )
        except Exception:
            return False

    def is_logged_in(self) -> bool:
        """Check the account AND record the result for /servers, /status, /login."""
        ok = self._check_logged_in()
        try:
            set_login_status(
                "logged_in" if ok else "logged_out",
                getattr(self, "login_reason", "") or "",
            )
        except Exception as error:
            print(f"[LOGIN STATUS] {error}")
        return ok

    def _check_logged_in(self) -> bool:
        """
        Open the profile page and decide if the shared account is usable.

        - Clear login form → logged out.
        - Clear logged-in markers / app data without login form → logged in.
        - Cloudflare or a slow/empty first paint is NOT treated as logout on the
          first try (that caused false /login prompts). We wait and re-check.
        """
        last_reason = "unknown"
        self.login_reason = ""
        try:
            for attempt in range(1, 4):
                self.open("https://wtr-lab.com/en/profile")
                time.sleep(2.0 + attempt * 0.8)
                if self.page_looks_challenged():
                    last_reason = "cloudflare"
                    print(
                        f"[LOGIN CHECK] attempt {attempt}/3: Cloudflare on profile — "
                        "waiting (not treating as logout yet)"
                    )
                    time.sleep(4.0)
                    continue
                if self.is_login_page():
                    print(
                        f"[LOGIN CHECK] attempt {attempt}/3: login form → not logged in"
                    )
                    self.login_reason = "login form shown on /profile"
                    return False
                src = (self.html() or "").lower()
                try:
                    url = (self.driver.current_url or "").lower()
                except Exception:
                    url = ""
                positive = (
                    "log out" in src
                    or "logout" in src
                    or "sign out" in src
                    or "my library" in src
                    or "reading list" in src
                    or "account settings" in src
                )
                has_next = "__next_data__" in src
                if positive or (
                    has_next
                    and not self.is_login_page()
                    and "/login" not in url
                    and "/signin" not in url
                ):
                    print(f"[LOGIN CHECK] attempt {attempt}/3: logged in")
                    self.login_reason = "profile page OK"
                    return True
                last_reason = f"no markers (url={url[:60]!r}, next_data={has_next})"
                print(f"[LOGIN CHECK] attempt {attempt}/3: {last_reason} — retrying")
                time.sleep(2.0)

            # Still unclear after retries. Prefer "logged in" when it was only
            # Cloudflare noise so we don't pause the queue for a false logout.
            if last_reason == "cloudflare":
                print(
                    "[LOGIN CHECK] still Cloudflare after retries — "
                    "not forcing /login (session may still be valid)"
                )
                self.login_reason = "Cloudflare page; session assumed valid"
                return True
            print(f"[LOGIN CHECK] still unclear ({last_reason}) → not logged in")
            self.login_reason = f"unclear: {last_reason}"[:200]
            return False
        except Exception as error:
            print(f"[LOGIN CHECK] failed: {type(error).__name__}: {error}")
            self.login_reason = f"check failed: {type(error).__name__}"
            return False

    def fetch_json(
        self,
        url: str,
        method: str = "GET",
        payload: Optional[dict] = None,
    ) -> tuple[int, Any]:
        try:
            result = self.driver.execute_async_script(
                """
                const url = arguments[0];
                const method = arguments[1];
                const payload = arguments[2];
                const done = arguments[arguments.length - 1];
                (async () => {
                    try {
                        const options = {
                            method,
                            credentials: "include",
                            headers: {"Content-Type": "application/json;charset=UTF-8"}
                        };
                        if (payload !== null) {
                            options.body = JSON.stringify(payload);
                        }
                        const response = await fetch(url, options);
                        const text = await response.text();
                        let parsed = null;
                        try { parsed = JSON.parse(text); }
                        catch (_) { parsed = {raw_text: text}; }
                        done({status: response.status, body: parsed});
                    } catch (error) {
                        done({status: 0, body: {error: String(error)}});
                    }
                })();
                """,
                url,
                method,
                payload,
            )
        except Exception as error:
            if is_dead_session(error):
                raise DeadBrowser(str(error)) from error
            print(f"[FETCH_JSON] {type(error).__name__}: {error}")
            return 0, {"error": str(error), "timeout": True}

        if not result:
            return 0, {"error": "empty script result", "timeout": True}
        return int(result["status"]), result["body"]

    def _cdp(self, command: str, params: Optional[dict] = None) -> Any:
        params = params or {}
        for target in (self.driver, getattr(self.driver, "driver", None)):
            if target is None:
                continue
            fn = getattr(target, "execute_cdp_cmd", None)
            if fn:
                return fn(command, params)
        raise RuntimeError("Chrome DevTools protocol is not available")

    def _absolute_url(self, url: str) -> str:
        url = (url or "").strip()
        if not url:
            return ""
        if url.startswith("//"):
            return "https:" + url
        if url.startswith("/"):
            return "https://wtr-lab.com" + url
        return url

    def fetch_bytes(self, url: str) -> Optional[bytes]:
        """
        Download binary data (covers, chapter images).

        In-page fetch() cannot read img.wtr-lab.com: the CDN 307s through
        /api/v2/img and the final response has no CORS headers. Chrome also
        content-negotiates AVIF, which Telegram cannot send. Prefer a plain
        HTTP GET with Accept: image/jpeg, then CDP, then in-page fetch.
        """
        url = self._absolute_url(url)
        if not url:
            return None

        for label, loader in (
            ("http", self._fetch_bytes_http),
            ("cdp", self._fetch_bytes_cdp),
            ("page", self._fetch_bytes_page),
        ):
            try:
                data = loader(url)
            except DeadBrowser:
                raise
            except Exception as error:
                print(f"[FETCH_BYTES {label}] {type(error).__name__}: {error}")
                continue
            if data and sniff_image_suffix(data):
                return data
            if data:
                print(
                    f"[FETCH_BYTES {label}] not an image "
                    f"({len(data)} bytes) from {url}"
                )
        return None

    def _fetch_bytes_cdp(self, url: str) -> Optional[bytes]:
        try:
            try:
                tree = self._cdp("Page.getFrameTree", {})
                frame_id = tree["frameTree"]["frame"]["id"]
            except Exception:
                frame_id = None

            params: dict[str, Any] = {
                "url": url,
                "options": {"disableCache": False, "includeCredentials": True},
            }
            if frame_id:
                params["frameId"] = frame_id

            result = self._cdp("Network.loadNetworkResource", params)
            resource = (result or {}).get("resource") or {}
            if not resource.get("success"):
                print(
                    f"[FETCH_BYTES CDP] fail status={resource.get('httpStatusCode')} "
                    f"err={resource.get('netErrorName')} {url}"
                )
                return None

            handle = resource.get("stream")
            if not handle:
                return None

            chunks: list[bytes] = []
            try:
                while True:
                    part = self._cdp("IO.read", {"handle": handle, "size": 262144})
                    payload = part.get("data") or ""
                    if part.get("base64Encoded"):
                        chunks.append(base64.b64decode(payload))
                    elif isinstance(payload, bytes):
                        chunks.append(payload)
                    else:
                        chunks.append(payload.encode("latin-1", errors="replace"))
                    if part.get("eof"):
                        break
            finally:
                try:
                    self._cdp("IO.close", {"handle": handle})
                except Exception:
                    pass

            blob = b"".join(chunks)
            return blob or None
        except Exception as error:
            if is_dead_session(error):
                raise DeadBrowser(str(error)) from error
            print(f"[FETCH_BYTES CDP] {type(error).__name__}: {error}")
            return None

    def _fetch_bytes_http(self, url: str) -> Optional[bytes]:
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/128.0.0.0 Safari/537.36"
            ),
            "Referer": "https://wtr-lab.com/",
            "Accept": "image/jpeg,image/png,image/webp;q=0.4,*/*;q=0.1",
        }
        try:
            ua = self.driver.execute_script("return navigator.userAgent")
            if ua:
                headers["User-Agent"] = ua
            cookies = self.driver.get_cookies() or []
            cookie_header = "; ".join(
                f"{item['name']}={item['value']}"
                for item in cookies
                if item.get("name")
            )
            if cookie_header:
                headers["Cookie"] = cookie_header
        except Exception:
            pass

        request = urllib.request.Request(url, headers=headers, method="GET")
        try:
            with urllib.request.urlopen(request, timeout=45) as response:
                data = response.read()
                content_type = str(response.headers.get("Content-Type") or "")
        except urllib.error.HTTPError as error:
            print(f"[FETCH_BYTES HTTP] HTTP {error.code} for {url}")
            return None
        except Exception as error:
            print(f"[FETCH_BYTES HTTP] {type(error).__name__}: {error}")
            return None

        if not data:
            return None
        if "text/html" in content_type.lower() and not sniff_image_suffix(data):
            print(f"[FETCH_BYTES HTTP] HTML instead of image for {url}")
            return None
        return data

    def _fetch_bytes_page(self, url: str) -> Optional[bytes]:
        try:
            result = self.driver.execute_async_script(
                """
                const url = arguments[0];
                const done = arguments[arguments.length - 1];
                (async () => {
                    try {
                        const response = await fetch(url, {credentials: "include"});
                        if (!response.ok) { done(null); return; }
                        const buffer = await response.arrayBuffer();
                        const bytes = new Uint8Array(buffer);
                        const chunk = 0x8000;
                        let binary = "";
                        for (let i = 0; i < bytes.length; i += chunk) {
                            binary += String.fromCharCode.apply(
                                null,
                                bytes.subarray(i, i + chunk)
                            );
                        }
                        done(btoa(binary));
                    } catch (_) {
                        done(null);
                    }
                })();
                """,
                url,
            )
        except Exception as error:
            if is_dead_session(error):
                raise DeadBrowser(str(error)) from error
            print(f"[FETCH_BYTES PAGE] {type(error).__name__}: {error}")
            return None
        return base64.b64decode(result) if result else None

    def page_looks_challenged(self) -> bool:
        """True if the current page is a Cloudflare / Turnstile interstitial."""
        try:
            title = (self.driver.get_title() or "").lower()
            source = (self.driver.get_page_source() or "").lower()
            return (
                "cloudflare" in title
                or "just a moment" in title
                or "turnstile" in source
                or "cf-challenge" in source
                or "cf-turnstile" in source
                or "verify you are human" in source
            )
        except Exception as error:
            if is_dead_session(error):
                raise DeadBrowser(str(error)) from error
            return False

    def wait_for_manual_access(
        self,
        novel_url: str,
        reason: str,
        *,
        task_id: Optional[int] = None,
        chat_id: Optional[int] = None,
        user_id: Optional[int] = None,
        chapter_no: Optional[int] = None,
    ):
        """
        Leave Chrome on the blocked URL and wait until:
          - anyone sends /solved, or
          - the challenge clears on a 10-minute auto-recheck, or
          - the task is cancelled.
        Never auto-clicks Turnstile (that path crashes Chrome on this server).
        """
        global captcha_wait

        url = (novel_url or "").strip() or "https://wtr-lab.com/"

        # Redirected to the login page = the account is logged out. That is
        # NOT a captcha: wait for a re-login instead of retrying the API.
        opened = False
        try:
            self.open(url)
            opened = True
        except Exception as error:
            if is_dead_session(error):
                raise DeadBrowser(str(error)) from error
            print(f"[TURNSTILE] open failed: {error}")
        if opened and self.is_login_page():
            if not self.is_logged_in():  # confirm on the profile page
                self.wait_for_login(
                    url,
                    reason,
                    task_id=task_id,
                    chat_id=chat_id,
                    user_id=user_id,
                    chapter_no=chapter_no,
                )
                return
            opened = False  # false alarm; profile loaded, reopen the page below

        if HEADLESS:
            self._notify(
                "turnstile_failed",
                "Cloudflare blocked headless Chrome. Set HEADLESS=0 so the "
                "admin can solve Turnstile over VNC.",
            )
            raise ManualChallengeRequired(
                "Cloudflare blocked the headless browser. "
                "Set HEADLESS=0 in .env, restart, and solve it over VNC."
            )

        print("\n" + "=" * 78)
        print("[WTR-LAB MANUAL TURNSTILE — NO AUTO-CLICK]")
        print(reason)
        print(f"Opening: {url}")
        print("Solve the checkbox in Chrome over VNC, then send /solved in Telegram")
        print("(or wait up to 10 minutes for an automatic recheck).")
        print("=" * 78 + "\n")

        if not opened:
            try:
                self.open(url)
            except Exception as error:
                if is_dead_session(error):
                    raise DeadBrowser(str(error)) from error
                print(f"[TURNSTILE] open failed: {error}")

        try:
            self.driver.switch_to.window(self.driver.current_window_handle)
        except Exception:
            pass
        self._keep_one_tab()

        # IP ban: stop hot loop, mark server, do not spam /solved path.
        try:
            body = ""
            try:
                body = self.driver.page_source or ""
            except Exception:
                pass
            title = ""
            try:
                title = self.driver.title or ""
            except Exception:
                pass
            if page_looks_ip_blocked(body, title):
                set_login_status("ip_blocked", "Cloudflare/WAF IP block page")
                print("[BLOCK] IP blocked by WTR/Cloudflare — pausing worker challenges")
                raise WtrError(
                    "This server IP appears blocked by Cloudflare/WTR-Lab. "
                    "Admin must change IP or wait; do not keep retrying."
                )
        except WtrError:
            raise
        except Exception as e:
            print(f"[BLOCK CHECK] {e}")

        # API 403 often has no classic CF HTML — always wait for /solved.
        if not self.page_looks_challenged():
            print(
                "[TURNSTILE] No classic CF interstitial — still waiting "
                "for /solved or 10-min recheck (API may still be blocked)."
            )

        solved_event.clear()
        captcha_wait = {
            "task_id": task_id,
            "chat_id": chat_id,
            "user_id": user_id,
            "url": url,
            "reason": reason,
            "chapter_no": chapter_no,
            "since": time.time(),
        }

        chapter_bit = (
            f" (chapter {chapter_no})" if chapter_no is not None else ""
        )
        self._notify(
            "turnstile_waiting",
            (
                f"Cloudflare captcha detected{chapter_bit}. "
                "Waiting for admin to clear it. Progress is saved."
            ),
        )

        admin_lines = [
            "🛡️ <b>Cloudflare Turnstile</b> — manual solve required",
            f"📌 {html.escape(reason[:300])}",
        ]
        if task_id is not None:
            admin_lines.append(f"🆔 Task: <code>{task_id}</code>")
        if chapter_no is not None:
            admin_lines.append(f"📖 Chapter: {chapter_no}")
        if user_id is not None:
            admin_lines.append(f"👤 User: <code>{user_id}</code>")
        admin_lines.append(f'🌐 <a href="{html.escape(url, quote=True)}">{html.escape(url)}</a>')
        admin_lines.append(
            "Solve the checkbox in the Chrome window over VNC, then send "
            "<b>/solved</b> to resume immediately (or wait ≤10 min for auto-recheck)."
        )
        notify_admin("\n".join(admin_lines))

        remote_enabled = bool(
            REMOTE_SOLVE
            and not HEADLESS
            and chat_id is not None
            and user_id is not None
        )
        remote: Optional[RemoteSolve] = None
        remote_links = 0

        def issue_link() -> Optional[RemoteSolve]:
            nonlocal remote_links
            remote_links += 1
            is_admin_user = bool(user_id is not None and is_admin(int(user_id)))
            session = RemoteSolve(
                self,
                task_id=task_id,
                user_id=user_id,
                allowed_url=url,
                enforce=not is_admin_user,
            )
            link = session.start()
            if not link:
                notify_admin(
                    "⚠️ Could not start the one-time solve link (see bot.log). "
                    "Solve it over VNC, then send /solved."
                )
                return None
            expiry_text = (
                f"The link works once and closes after /solved "
                f"(or after {REMOTE_LINK_MAX_MINUTES} minutes)."
                if REMOTE_LINK_MAX_MINUTES > 0
                else "The link works once and stays open until /solved is sent "
                "or the download is cancelled."
            )
            if is_admin_user:
                body = (
                    "🛡️ <b>Cloudflare check needed</b>\n\n"
                    f"🔗 <a href=\"{html.escape(link, quote=True)}\">Open the one-time link</a>\n"
                    f"{html.escape(link)}\n\n"
                    "1️⃣ Tick the Cloudflare checkbox.\n"
                    "2️⃣ Send <b>/solved</b> here.\n\n"
                    "Your activity in that window is logged (not restricted).\n\n"
                    f"{expiry_text}"
                )
            else:
                body = (
                    "🛡️ <b>Cloudflare check needed</b>\n\n"
                    "You can tick the checkbox yourself, or just wait and an "
                    "admin will do it for you.\n\n"
                    f"🔗 <a href=\"{html.escape(link, quote=True)}\">Open the one-time link</a>\n"
                    f"{html.escape(link)}\n\n"
                    "1️⃣ Tick the Cloudflare checkbox.\n"
                    "2️⃣ Send <b>/solved</b> here.\n\n"
                    "⚠️ <b>Everything you do in that window is logged</b> "
                    "(refreshing, opening tabs or other sites, DevTools, "
                    "logging out, closing Chrome) and reviewed by an admin. "
                    "Only solve the Cloudflare check. Anything suspicious "
                    "gets you banned from this bot.\n\n"
                    f"{expiry_text}"
                )
            try:
                send_notice(chat_id, body, user_id=user_id, parse_mode="HTML")
            except Exception as error:
                print(f"[REMOTE] could not send link: {error}")
            if str(chat_id) != str(ADMIN_CHAT_ID):
                notify_admin(
                    f"🔗 One-time solve link sent to user <code>{user_id}</code> "
                    f"(session <code>{session.session_id}</code>, task "
                    f"<code>{task_id}</code>).\n{html.escape(link)}\n"
                    "Review activity with /actions."
                )
            # Also post to OUTPUT_GROUPS so anyone online can clear the captcha
            # and keep the queue moving (not only the task owner / admin).
            group_body = (
                "🛡️ <b>Cloudflare check — help keep the queue moving</b>\n\n"
                f"Task <code>{task_id}</code>"
                + (f" · chapter {chapter_no}" if chapter_no is not None else "")
                + (f" · user <code>{user_id}</code>" if user_id is not None else "")
                + "\n\n"
                f"🔗 <a href=\"{html.escape(link, quote=True)}\">Open the one-time solve link</a>\n"
                f"{html.escape(link)}\n\n"
                "1️⃣ Open the link (rotate phone to landscape if needed).\n"
                "2️⃣ Tick the Cloudflare checkbox; wait until the chapter loads.\n"
                "3️⃣ Send <b>/solved</b> to this bot (anyone can).\n\n"
                "⚠️ Only solve the captcha. Do not log out, open other sites, "
                "or use DevTools — activity is logged.\n\n"
                f"{expiry_text}"
            )
            for group in OUTPUT_GROUPS:
                try:
                    telegram_call(
                        bot.send_message,
                        group,
                        group_body,
                        parse_mode="HTML",
                        link_preview_options=LINK_PREVIEW_OFF,
                    )
                except Exception as error:
                    print(f"[REMOTE] could not send link to group {group}: {error}")
            return session

        if remote_enabled:
            remote = issue_link()

        last_recheck = time.time()
        try:
            while not stop_event.is_set():
                if task_id is not None and is_cancelled(task_id):
                    captcha_wait = {}
                    solved_event.clear()
                    raise TaskCancelled()

                # /solved or timed recheck
                remaining = TURNSTILE_RECHECK_SECONDS - (time.time() - last_recheck)
                wait_slice = max(1.0, min(15.0, remaining if remaining > 0 else 15.0))
                if remote is not None:
                    wait_slice = min(wait_slice, 2.0)
                if solved_event.wait(timeout=wait_slice):
                    print("[TURNSTILE] /solved received — checking page…")
                    solved_event.clear()
                    if remote is not None:
                        remote.stop("user sent /solved")
                        remote = None
                    try:
                        self.open(url)
                        time.sleep(2)
                        self._keep_one_tab()
                    except Exception as error:
                        if is_dead_session(error):
                            captcha_wait = {}
                            raise DeadBrowser(str(error)) from error
                        print(f"[TURNSTILE] reload after /solved: {error}")
                    if not self.page_looks_challenged():
                        print("[TURNSTILE] Challenge cleared after /solved.")
                        captcha_wait = {}
                        self._notify("turnstile_cleared", "Captcha cleared — resuming.")
                        return
                    print("[TURNSTILE] Still challenged after /solved — keep waiting.")
                    self._notify(
                        "turnstile_waiting",
                        "Still seeing the captcha after /solved. Finish the "
                        "checkbox, then /solved again.",
                    )
                    last_recheck = time.time()
                    if remote_enabled and remote_links < 3:
                        remote = issue_link()  # fresh random link
                    continue

                if remote is not None:
                    remote.poll()
                    if remote.expired():
                        remote.stop("link expired")
                        remote = None
                        last_recheck = time.time()
                        try:
                            send_notice(
                                chat_id,
                                "⌛ The solve link expired. An admin will solve the "
                                "Cloudflare check for you; no action needed.",
                                user_id=user_id,
                            )
                        except Exception:
                            pass
                    continue  # no auto-reload while someone is solving

                if time.time() - last_recheck >= TURNSTILE_RECHECK_SECONDS:
                    last_recheck = time.time()
                    print(
                        f"[TURNSTILE] 10-min recheck — reloading {url}"
                    )
                    try:
                        self.open(url)
                        time.sleep(2)
                        self._keep_one_tab()
                    except Exception as error:
                        if is_dead_session(error):
                            captcha_wait = {}
                            raise DeadBrowser(str(error)) from error
                        print(f"[TURNSTILE] recheck open failed: {error}")
                        continue
                    if not self.page_looks_challenged():
                        print("[TURNSTILE] Challenge cleared on auto-recheck.")
                        captcha_wait = {}
                        self._notify("turnstile_cleared", "Captcha cleared — resuming.")
                        return
                    print("[TURNSTILE] Still challenged on auto-recheck — waiting another 10 min.")
                    self._notify(
                        "turnstile_waiting",
                        "Still waiting on Cloudflare captcha (auto-recheck). "
                        "Admin can send /solved after clearing it in Chrome.",
                    )
        finally:
            if remote is not None:
                remote.stop("download resumed or ended")

        captcha_wait = {}
        raise TaskCancelled()

    def wait_for_login(
        self,
        url: str,
        reason: str,
        *,
        task_id: Optional[int] = None,
        chat_id: Optional[int] = None,
        user_id: Optional[int] = None,
        chapter_no: Optional[int] = None,
    ):
        """
        The shared WTR-Lab account is logged out. Do not retry the API (that
        hammers the site and burns attempts). Tell the admin and the task's
        user once, then wait here for a re-login: a /login magic link (run on
        this worker thread) or someone logging in through the Chrome window.
        """
        global login_wait, session_state
        session_state = "logged_out"
        login_wait = {
            "task_id": task_id,
            "chat_id": chat_id,
            "user_id": user_id,
            "since": time.time(),
        }
        chapter_bit = f" (chapter {chapter_no})" if chapter_no is not None else ""
        sid = html.escape(str(SERVER_ID))
        print(f"[LOGIN] WTR-Lab account is logged out on {SERVER_ID}{chapter_bit}. {reason}")
        set_login_status("logged_out", reason[:300])
        self._notify(
            "login_required",
            f"WTR-Lab account on server {SERVER_ID} is logged out. Waiting for /login.",
        )
        task_bit = f"Task <code>{task_id}</code>" if task_id is not None else "A task"
        if "when the task started" in (reason or "").lower():
            headline = (
                f"{task_bit} didn't start because server <code>{sid}</code> "
                "is logged out."
            )
        else:
            headline = (
                f"{task_bit} is paused because server <code>{sid}</code> "
                f"got logged out{html.escape(chapter_bit)}."
            )
        admin_lines = [
            f"🔐 <b>Server {sid} is logged out</b>",
            headline,
            f"📌 {html.escape(reason[:300])}",
            f"The crawler on {sid} is paused (not a captcha). Send "
            f"<code>/login {sid}</code>, then your email and the magic link, "
            "or log in through that server's Chrome window.",
        ]
        if user_id is not None:
            admin_lines.append(f"👤 User: <code>{user_id}</code>")
        login_markup = telebot.types.InlineKeyboardMarkup()
        login_markup.add(
            telebot.types.InlineKeyboardButton(
                f"🔐 Login on {SERVER_ID}", callback_data=f"login_srv:{SERVER_ID}"
            )
        )
        notify_admin("\n".join(admin_lines), reply_markup=login_markup)
        if chat_id is not None:
            try:
                send_notice(
                    chat_id,
                    (
                        "🔐 <b>WTR-Lab login needed</b>\n\n"
                        f"Server <code>{sid}</code> is logged out, so the "
                        "download is paused (progress is saved).\n"
                        f"Send /login, reply with <code>{sid}</code>, then your "
                        "email address to log it back in. The download resumes "
                        "by itself afterwards."
                    ),
                    user_id=user_id,
                    parse_mode="HTML",
                )
            except Exception as error:
                print(f"[LOGIN] notice failed: {error}")
        if user_id is not None:
            session_start(int(user_id), step="login_email", from_task=task_id)

        last_check = time.time()
        try:
            while not stop_event.is_set():
                if task_id is not None and is_cancelled(task_id):
                    raise TaskCancelled()
                if not self.is_alive():
                    raise DeadBrowser("Chrome died while waiting for login")
                try:
                    request = login_requests.get(timeout=5)
                except queue.Empty:
                    request = None
                if request is not None:
                    do_magic_login(
                        request["chat_id"],
                        request["user_id"],
                        request["email"],
                        browser=self,
                    )
                    last_check = 0.0  # verify right away
                if time.time() - last_check >= LOGIN_RECHECK_SECONDS:
                    last_check = time.time()
                    if self.is_logged_in():
                        session_state = "ok"
                        print("[LOGIN] Logged in again — resuming.")
                        self._notify("login_ok", "Logged in again — resuming.")
                        notify_admin(
                            f"✅ Server <code>{sid}</code> is logged in again. "
                            "Crawler resuming."
                        )
                        if chat_id is not None:
                            send_notice(
                                chat_id,
                                "✅ WTR-Lab is logged in again — resuming your download.",
                                user_id=user_id,
                            )
                        state = pending_download.get(user_id) if user_id is not None else None
                        if state and str(state.get("step", "")).startswith("login"):
                            pending_download.pop(user_id, None)
                        return
            raise TaskCancelled()
        finally:
            login_wait = {}


# ---------------------------------------------------------------------------
# Magic-link login# ---------------------------------------------------------------------------
# Magic-link login (self-service for any allowed user)
# ---------------------------------------------------------------------------

def do_magic_login(
    chat_id: int,
    user_id: int,
    email: str,
    browser: Optional["WtrBrowser"] = None,
) -> bool:
    """
    Drive the shared Chrome profile through WTR-Lab magic-link login.
    The user pastes the magic link back into Telegram; we open it in Chrome.
    """
    global current_login_user, session_state
    owned = browser is None
    try:
        with login_lock:
            current_login_user = user_id

        if owned:
            browser = WtrBrowser()

        remote_magic_links.pop(user_id, None)  # drop a stale forwarded link
        browser.open("https://wtr-lab.com/en/profile")
        time.sleep(2)

        if not browser.is_login_page():
            session_state = "ok"
            set_login_status("logged_in", "already logged in (login request)")
            send_notice(
                chat_id,
                f"✅ Server <code>{html.escape(SERVER_ID)}</code> is already "
                "logged in — no magic link needed.",
                user_id=user_id,
                parse_mode="HTML",
            )
            return True

        wait = WebDriverWait(browser.driver, 25)
        email_input = wait.until(
            EC.presence_of_element_located(
                (
                    By.CSS_SELECTOR,
                    'input[placeholder*="email" i], input[type="email"]',
                )
            )
        )
        email_input.clear()
        email_input.send_keys(email)

        continue_btn = wait.until(
            EC.element_to_be_clickable(
                (
                    By.XPATH,
                    '//*[contains(text(),"Continue with Email") or '
                    'contains(text(),"Continue with email")]',
                )
            )
        )
        continue_btn.click()

        send_notice(
            chat_id,
            (
                f"📬 Magic link requested for <code>{html.escape(email)}</code> "
                f"on server <code>{html.escape(SERVER_ID)}</code>.\n"
                "Check your email (and spam) and paste the <b>full magic link</b> here."
            ),
            user_id=user_id,
            parse_mode="HTML",
        )

        deadline = time.time() + 360
        magic_url = None
        while time.time() < deadline:
            state = pending_download.get(user_id)
            if state and state.get("magic_url"):
                magic_url = state["magic_url"]
                break
            forwarded = remote_magic_links.pop(user_id, None)
            if forwarded:
                magic_url = forwarded
                break
            time.sleep(1.5)

        if not magic_url:
            send_notice(
                chat_id,
                "⏰ Timed out waiting for the magic link (6 minutes).",
                user_id=user_id,
            )
            return False

        print(f"[LOGIN] Opening magic link for user {user_id}")
        browser.open(magic_url)
        time.sleep(6)

        browser.open("https://wtr-lab.com/en/profile")
        time.sleep(3)

        if browser.is_login_page():
            set_login_status("logged_out", "magic link did not log in")
            send_notice(
                chat_id,
                f"⚠️ Login on <code>{html.escape(SERVER_ID)}</code> may have "
                "failed — still seeing the login form.",
                user_id=user_id,
                parse_mode="HTML",
            )
            return False

        session_state = "ok"
        set_login_status("logged_in", f"magic link login by user {user_id}")
        send_notice(
            chat_id,
            (
                f"✅ <b>Login successful on {html.escape(SERVER_ID)}!</b>\n"
                "The shared Chrome profile is now authenticated.\n"
                "You and other users can download novels normally."
            ),
            user_id=user_id,
            parse_mode="HTML",
        )
        notify_admin(
            f"✅ User <code>{user_id}</code> logged in server "
            f"<code>{html.escape(SERVER_ID)}</code> via magic link."
        )
        return True

    except Exception as e:
        print(f"[LOGIN ERROR] {type(e).__name__}: {e}")
        send_notice(chat_id, f"❌ Login failed: {e}", user_id=user_id)
        return False
    finally:
        with login_lock:
            current_login_user = None
        state = pending_download.get(user_id)
        if state and str(state.get("step", "")).startswith("login"):
            pending_download.pop(user_id, None)
        if owned and browser:
            browser.close()


# ---------------------------------------------------------------------------
# WTR-Lab WebToEpub-style parser
# ---------------------------------------------------------------------------

AES_KEY = b"IJAFUUxjM25hyzL2AZrn0wl7cESED6Ru"


@dataclass
class ChapterInfo:
    order: int
    chapter_id: str
    serie_id: str
    title: str


@dataclass
class NovelInfo:
    novel_id: str
    source_url: str
    title: str
    author: str
    synopsis: str
    cover_url: str
    language: str
    slug: str
    chapters: list[ChapterInfo]
    story_terms: dict[str, str]
    user_terms: list[tuple[str, str]]
    status_label: str = "Unknown"
    unlock_count: int = 0
    chapter_count: int = 0
    raw_title: str = ""


def nested(data: Any, *keys: str, default=None):
    current = data
    for key in keys:
        if not isinstance(current, dict):
            return default
        current = current.get(key)
    return default if current is None else current


def first_nonempty(*values: Any) -> str:
    for value in values:
        text = str(value or "").strip()
        if text:
            return text
    return ""


def pick_english(*values: Any) -> str:
    texts = [str(value).strip() for value in values if str(value or "").strip()]
    for text in texts:
        if not looks_mostly_cjk(text):
            return text
    return texts[0] if texts else ""


def _shrink_for_debug(obj: Any, depth: int = 0) -> Any:
    """Truncate long strings/lists so a response can be dumped safely."""
    if depth > 8:
        return "<max depth>"
    if isinstance(obj, dict):
        return {str(k): _shrink_for_debug(v, depth + 1) for k, v in list(obj.items())[:40]}
    if isinstance(obj, list):
        items = [_shrink_for_debug(v, depth + 1) for v in obj[:5]]
        if len(obj) > 5:
            items.append(f"<{len(obj) - 5} more items>")
        return items
    if isinstance(obj, str):
        return obj if len(obj) <= 200 else obj[:200] + f"...<{len(obj)} chars>"
    return obj


def locate_chapter_body(response: Any) -> tuple[dict, Any]:
    """
    Find the chapter body in a /api/reader/get response.

    Preferred layout is response["data"]["data"]["body"]. If WTR-Lab moves
    it, fall back to any nearby "body" key, then a bounded recursive search.
    Returns (container_dict, body). body is None when nothing usable exists.
    """
    def usable(value: Any) -> bool:
        return isinstance(value, (list, str)) and len(value) > 0

    for keys in (
        ("data", "data"),
        ("data",),
        ("chapter",),
        (),
    ):
        container = nested(response, *keys, default=None) if keys else response
        if isinstance(container, dict) and usable(container.get("body")):
            return container, container.get("body")

    def walk(obj: Any, depth: int = 0):
        if depth > 6:
            return None
        if isinstance(obj, dict):
            if usable(obj.get("body")):
                return obj, obj.get("body")
            for value in obj.values():
                found = walk(value, depth + 1)
                if found:
                    return found
        elif isinstance(obj, list):
            for value in obj[:10]:
                found = walk(value, depth + 1)
                if found:
                    return found
        return None

    found = walk(response)
    if found:
        return found

    inner = nested(response, "data", "data", default={})
    return (inner if isinstance(inner, dict) else {}), None


def dump_chapter_debug(order: int, response: Any) -> Optional[Path]:
    try:
        debug_dir = DATA_DIR / "debug"
        debug_dir.mkdir(parents=True, exist_ok=True)
        path = debug_dir / f"chapter_{order}_response.json"
        path.write_text(
            json.dumps(_shrink_for_debug(response), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return path
    except Exception as error:
        print(f"[DEBUG DUMP ERROR] {error}")
        return None


def decrypt_body(encrypted: Any) -> list[str]:
    """
    Same AES-GCM body format handled by the current WTR-Lab crawler.
    """
    if isinstance(encrypted, list):
        return [str(item) for item in encrypted]

    if not isinstance(encrypted, str):
        raise WtrError("Unknown chapter body type")

    is_array = encrypted.startswith("arr:")
    if is_array:
        encrypted = encrypted[4:]
    elif encrypted.startswith("str:"):
        encrypted = encrypted[4:]
    else:
        raise WtrError("Unknown encrypted chapter-body format")

    parts = encrypted.split(":")
    if len(parts) != 3:
        raise WtrError("Invalid encrypted chapter-body format")

    iv_b64, tag_b64, ciphertext_b64 = parts
    iv = base64.b64decode(iv_b64)
    tag = base64.b64decode(tag_b64)
    ciphertext = base64.b64decode(ciphertext_b64)

    plaintext = AESGCM(AES_KEY).decrypt(iv, ciphertext + tag, None).decode("utf-8")
    decoded = json.loads(plaintext) if is_array else plaintext

    return [str(item) for item in decoded] if isinstance(decoded, list) else [str(decoded)]


def term_text(value: Any) -> str:
    """
    Normalize a WTR-Lab glossary slot to a single string.

    Chapter glossary entries are typically:
      [ ["English name", ...optional alts], "original" ]
    Story glossary entries from /api/v2/reader/terms use the same shape.
    """
    if value is None:
        return ""
    if isinstance(value, list):
        for item in value:
            text = term_text(item)
            if text:
                return text
        return ""
    if isinstance(value, dict):
        for key in ("en", "to", "replacement", "text", "value", "title"):
            if key in value:
                text = term_text(value[key])
                if text:
                    return text
        return ""
    return str(value).strip()


def make_story_terms(payload: Any) -> dict[str, str]:
    """
    WebToEpub's /api/v2/reader/terms/<novel>.json behavior.
    Story terms override a chapter's built-in glossary translation.
    """
    result: dict[str, str] = {}

    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except Exception:
            payload = {}

    for glossary in (payload or {}).get("glossaries", []) or []:
        for term in nested(glossary, "data", "terms", default=[]) or []:
            if isinstance(term, dict):
                replacement = term_text(
                    term.get("en") or term.get("to") or term.get("replacement")
                )
                original = term_text(
                    term.get("zh") or term.get("from") or term.get("original")
                )
            elif isinstance(term, list) and len(term) >= 2:
                replacement = term_text(term[0])
                original = term_text(term[1])
            else:
                continue

            if replacement and original:
                result[original] = replacement

    return result


def make_user_terms(payload: Any, serie_id: str) -> list[tuple[str, str]]:
    """
    WebToEpub's /api/v2/user/config behavior.
    Uses personal WTR-Lab glossary settings if your logged-in account has any.
    """
    output: list[tuple[str, str]] = []
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except Exception:
            payload = {}

    terms = nested(payload, "config", "terms", default=[]) or []

    for item in terms:
        if not isinstance(item, list) or len(item) < 3:
            continue

        # item[4] is an optional list of serie ids this term applies to.
        # Missing / null means the term is global (WebToEpub treats it that way).
        applies_to = item[4] if len(item) > 4 else None
        if applies_to is not None and serie_id not in applies_to:
            continue

        replacement = term_text(item[1])
        originals = term_text(item[2]).split("|")

        for original in originals:
            original = original.strip()
            if original and replacement:
                output.append((original, replacement))

    return output


# WTR-Lab inserts glossary slots into AI chapter text as:
#   ※{index}⛬   U+203B REFERENCE MARK + index + U+26EC WHITE FLAG
#   ※{index}〓   U+203B + index + U+3013 GETA MARK
# WebToEpub replaces both. Older dumps of this worker also used 〘 (U+3018)
# by mistake, and UTF-8-read-as-Latin-1 mojibake of the same glyphs.
_GLOSSARY_OPEN = r"(?:※|â€»|&#8251;|&#x203[bB];)"
_GLOSSARY_CLOSE = r"(?:⛬|〓|〘|〙|â›¬|ã€“|ã€˜|&#x26[eE][cC];|&#x3013;)"
_GLOSSARY_GAP = r"[\s\u200b\u200c\u200d\ufeff]*"
PLACEHOLDER_RE = re.compile(
    _GLOSSARY_OPEN + _GLOSSARY_GAP + r"(\d+)" + _GLOSSARY_GAP + _GLOSSARY_CLOSE
)
BARE_PLACEHOLDER_RE = re.compile(
    _GLOSSARY_OPEN + _GLOSSARY_GAP + r"(\d+)(?!\d)"
)


def xhtml_has_unresolved_placeholders(text: str) -> bool:
    return bool(text and PLACEHOLDER_RE.search(text))


def prepare_chapter_terms(
    terms: Any,
    story_terms: dict[str, str],
    user_terms: list[tuple[str, str]],
) -> list[str]:
    """
    Build the index → replacement table WebToEpub uses.

    Story glossary wins over the chapter's built-in translation; the user's
    personal WTR-Lab terms win over both.
    """
    if isinstance(terms, str):
        try:
            terms = json.loads(terms)
        except Exception:
            terms = []
    if isinstance(terms, dict):
        numeric_keys = [int(k) for k in terms if str(k).isdigit()]
        if numeric_keys:
            terms = [terms.get(i, terms.get(str(i))) for i in range(max(numeric_keys) + 1)]
        else:
            terms = list(terms.values())
    if not isinstance(terms, list):
        terms = []

    prepared: list[str] = []
    for item in terms:
        replacement = ""
        original = ""
        if isinstance(item, list) and item:
            replacement = term_text(item[0])
            original = term_text(item[1]) if len(item) > 1 else ""
        elif isinstance(item, dict):
            replacement = term_text(
                item.get("en")
                or item.get("to")
                or item.get("replacement")
                or item.get("0")
            )
            original = term_text(
                item.get("zh")
                or item.get("from")
                or item.get("original")
                or item.get("1")
            )
        else:
            replacement = term_text(item)

        if original and original in story_terms:
            replacement = story_terms[original]
        if original:
            for user_original, user_replacement in user_terms:
                if original == user_original:
                    replacement = user_replacement
                    break
        prepared.append(replacement)
    return prepared


def apply_glossary_to_text(
    text: str,
    prepared_terms: list[str],
    user_terms: list[tuple[str, str]],
    patches: Any = None,
) -> str:
    """
    Same replacement order as WebToEpub's WtrlabParser.buildChapter:
      1. ※{n}⛬ / ※{n}〓  → chapter/story/user glossary slot
      2. user custom string replacements
      3. chapter `patch` zh→en substitutions
    """
    text = html.unescape(str(text or ""))

    def slot_for(match: re.Match) -> str:
        index = int(match.group(1))
        if 0 <= index < len(prepared_terms):
            return prepared_terms[index]
        return match.group(0)

    text = PLACEHOLDER_RE.sub(slot_for, text)

    # Explicit WebToEpub replaceAll pass, including mojibake / spacing variants
    # the regex might not have caught.
    for index, replacement in enumerate(prepared_terms):
        if not replacement:
            continue
        for placeholder in (
            f"※{index}⛬",
            f"※{index}〓",
            f"※{index}〘",
            f"※ {index} ⛬",
            f"※ {index} 〓",
            f"â€»{index}â›¬",
            f"â€»{index}ã€“",
            f"â€»{index}ã€˜",
        ):
            text = text.replace(placeholder, replacement)

    # Bare ※12 leftovers (no closer glyph) after the closed forms are gone.
    text = BARE_PLACEHOLDER_RE.sub(slot_for, text)

    for original, replacement in user_terms:
        if original:
            text = text.replace(original, replacement)

    for patch in patches or []:
        if isinstance(patch, dict) and patch.get("zh"):
            text = text.replace(str(patch["zh"]), " " + str(patch.get("en") or ""))

    return text


def wtr_api_message(body: Any) -> str:
    """Best-effort human message from a WTR-Lab API JSON body."""
    if not isinstance(body, dict):
        return str(body).strip()[:300] if body is not None else ""
    for key in ("message", "error", "detail", "msg", "reason"):
        val = body.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()
        if isinstance(val, dict):
            nested_msg = val.get("message") or val.get("error") or val.get("detail")
            if isinstance(nested_msg, str) and nested_msg.strip():
                return nested_msg.strip()
    data = body.get("data")
    if isinstance(data, dict):
        for key in ("message", "error", "detail"):
            val = data.get(key)
            if isinstance(val, str) and val.strip():
                return val.strip()
    return ""


def is_chapter_waiting_crawl(status: int, body: Any) -> bool:
    """Chapter exists but WTR-Lab has not finished crawling/translating it yet."""
    msg = wtr_api_message(body).lower()
    if not msg:
        return False
    return (
        "waiting for crawling" in msg
        or "waiting for crawl" in msg
        or "check back later" in msg
        or "not yet crawled" in msg
        or "still being crawled" in msg
    )


def is_chapter_locked(status: int, body: Any) -> bool:
    if isinstance(body, dict):
        code = str(body.get("code") or "")
        if code == "CHAPTER_LOCKED":
            return True
        msg = wtr_api_message(body).lower()
        if "chapter_locked" in msg or "isn't ai translated" in msg:
            return True
        if "chapter locked" in msg or "ai-lock" in msg:
            return True
        if "ai translated" in msg and ("not" in msg or "isn't" in msg or "isnt" in msg):
            return True
    return False


def is_origin_timeout(status: int, body: Any) -> bool:
    """
    Cloudflare 522 / origin unreachable or script timeout.
    Not a Turnstile challenge — the origin (or path to it) did not answer in time.
    """
    if status == 522:
        return True

    if isinstance(body, dict):
        if body.get("timeout"):
            return True
        err = str(body.get("error") or body.get("message") or "").lower()
        if "timeout" in err or "script timeout" in err:
            return True
        raw = str(body.get("raw_text") or "")
        if raw:
            lower = raw.lower()
            if (
                "error code 522" in lower
                or ("522" in lower and "timed out" in lower)
                or ("cloudflare" in lower and "connection timed out" in lower)
            ):
                return True

    if isinstance(body, str):
        lower = body.lower()
        if (
            "error code 522" in lower
            or ("522" in lower and "timed out" in lower)
            or ("cloudflare" in lower and "connection timed out" in lower)
            or "script timeout" in lower
        ):
            return True

    return False


def is_challenge(status: int, body: Any) -> bool:
    if is_chapter_locked(status, body):
        return False

    # Origin timeout is not a Turnstile challenge — handled with long backoff.
    if is_origin_timeout(status, body):
        return False

    # 503 is a temporary origin/nginx blip — not Turnstile. Handled with backoff
    # in download_chapter, same idea as origin timeout but shorter waits.
    if status in (401, 403, 429):
        return True

    if isinstance(body, dict):
        if body.get("requireTurnstile"):
            return True
        code = body.get("code")
        if code in (1401, "1401"):
            return True
        msg = str(body.get("message") or body.get("error") or "").lower()
        if "turnstile" in msg or "challenge" in msg:
            return True

    if isinstance(body, str):
        lower = body.lower()
        if "turnstile" in lower or "challenge" in lower:
            return True

    return False


def parse_chapter_range(value: Optional[str], total: int) -> tuple[int, int]:
    """
    Accepts: all, 40-60, 40 60, 40 to 60, or one number.
    """
    text = (value or "all").strip().lower()

    if text in ("", "all", "full"):
        return 1, total

    numbers = [int(number) for number in re.findall(r"\d+", text)]

    if len(numbers) == 1:
        start = end = numbers[0]
    elif len(numbers) >= 2:
        start, end = numbers[0], numbers[1]
    else:
        raise WtrError(f"Invalid chapter range: {value!r}")

    start = max(1, start)
    end = min(total, end)

    if start > end:
        raise WtrError(f"Invalid chapter range: {value!r}")

    return start, end


def safe_filename(text: str, max_len: int = 150) -> str:
    text = re.sub(r'[<>:"/\\|?*]', " ", text)
    text = "".join(ch if ch.isprintable() and ord(ch) >= 32 else " " for ch in text)
    text = re.sub(r"\s+", " ", text).strip(" .")
    if max_len > 0:
        text = text[:max_len].rstrip(" .")
    return text or "WTR-Lab Novel"


def epub_stem(title: str, start: int, end: int) -> str:
    """
    Filename stem that always keeps the chapter range, even when the novel
    title is long. Telegram's document name and the on-disk EPUB both use this.
    """
    range_suffix = f" c{start}-{end}"
    budget = max(40, 150 - len(range_suffix))
    base = safe_filename(title, max_len=budget)
    return f"{base}{range_suffix}"

def extract_slug(source_url: str, current_url: str, soup: BeautifulSoup) -> str:
    """
    From novel page URL or on-page chapter links:
      /en/novel/66196/some-slug
      /en/novel/66196/some-slug/chapter-12
    → some-slug
    """
    for candidate in (current_url, source_url):
        match = re.search(r"/novel/\d+/([^/]+)", candidate or "", re.I)
        if match:
            part = match.group(1).strip("/")
            if not part.lower().startswith("chapter-"):
                return part

    for a in soup.select('a[href*="/novel/"]'):
        href = a.get("href") or ""
        match = re.search(r"/novel/\d+/([^/]+)/chapter-\d+", href, re.I)
        if match:
            return match.group(1)

    return "novel"

class WtrLabClient:
    def __init__(self, browser: WtrBrowser):
        self.browser = browser
        # Set by process_task so clear_turnstile / download_chapter can wait
        # with cancel checks and correct Telegram targets.
        self.task_id: Optional[int] = None
        self.chat_id: Optional[int] = None
        self.user_id: Optional[int] = None

    def load_novel(self, source_url: str) -> NovelInfo:
        self.browser.open(source_url)

        current_url = ""
        try:
            current_url = self.browser.driver.current_url or ""
        except Exception:
            pass

        soup = BeautifulSoup(self.browser.html(), "html.parser")
        next_tag = soup.select_one("script#__NEXT_DATA__")
        if not next_tag or not next_tag.string:
            # Missing app data is often logout or Cloudflare — don't burn the queue.
            if self.browser.is_login_page():
                self.browser.wait_for_login(
                    source_url,
                    "Novel page showed login form (no __NEXT_DATA__)",
                    task_id=self.task_id,
                    chat_id=self.chat_id,
                    user_id=self.user_id,
                )
                self.browser.open(source_url)
                soup = BeautifulSoup(self.browser.html(), "html.parser")
                next_tag = soup.select_one("script#__NEXT_DATA__")
            elif self.browser.page_looks_challenged():
                raise ManualChallengeRequired(
                    "Cloudflare challenge on novel page (no __NEXT_DATA__). "
                    "Solve it over VNC or the remote link, then /solved / /continue."
                )
            if not next_tag or not next_tag.string:
                raise WtrError(
                    "WTR-Lab page did not contain __NEXT_DATA__ "
                    "(empty, blocked, or logged-out page)"
                )

        page_data = json.loads(next_tag.string)
        query = page_data.get("query", {})
        serie = nested(page_data, "props", "pageProps", "serie", default={}) or {}
        serie_data = nested(serie, "serie_data", default={}) or {}
        raw = nested(serie_data, "data", "raw", default={}) or {}
        public_data = nested(serie_data, "data", default={}) or {}
        names = serie.get("names") or []
        name0 = names[0] if names and isinstance(names[0], dict) else {}

        novel_id = str(query.get("raw_id") or serie_data.get("raw_id") or "")
        if not novel_id:
            match = re.search(r"/novel/(\d+)", source_url) or re.search(
                r"/novel/(\d+)", current_url
            )
            if not match:
                raise WtrError("Could not determine WTR-Lab novel ID")
            novel_id = match.group(1)

        language = str(query.get("locale") or "en")
        h1 = soup.select_one("h1")
        h1_title = h1.get_text(" ", strip=True) if h1 else ""
        desc_span = soup.select_one("span.description")
        page_synopsis = desc_span.get_text(" ", strip=True) if desc_span else ""

        raw_title = first_nonempty(raw.get("title"), name0.get("raw_title"))
        title = pick_english(
            public_data.get("title"),
            name0.get("title"),
            h1_title,
            raw_title,
        ) or "Untitled"
        author = first_nonempty(
            public_data.get("author"),
            serie_data.get("author"),
            raw.get("author"),
            "Unknown",
        )
        synopsis = pick_english(
            public_data.get("description"),
            page_synopsis,
            raw.get("description"),
        )
        og_image = ""
        og_tag = soup.select_one('meta[property="og:image"], meta[name="og:image"]')
        if og_tag:
            og_image = str(og_tag.get("content") or "")
        cover_url = first_nonempty(
            public_data.get("image"),
            nested(serie_data, "data", "image"),
            nested(raw, "image"),
            og_image,
            cover_url_from_soup(soup),
        )
        slug = extract_slug(source_url, current_url, soup) or str(serie_data.get("slug") or "novel")
        status_label = novel_status_label(serie_data.get("status"))
        chapter_count = int(serie_data.get("chapter_count") or 0)
        unlock_count = int(serie_data.get("unlock_count") or 0)

        # Normalize to full novel URL (DB tasks may omit the slug).
        source_url = f"https://wtr-lab.com/{language}/novel/{novel_id}/{slug}"

        status, chapter_payload = self.browser.fetch_json(
            f"https://wtr-lab.com/api/chapters/{novel_id}"
        )

        if is_challenge(status, chapter_payload):
            self.browser.wait_for_manual_access(
                source_url,
                "WTR-Lab blocked the chapter-list request.",
                task_id=self.task_id,
                chat_id=self.chat_id,
                user_id=self.user_id,
            )
            status, chapter_payload = self.browser.fetch_json(
                f"https://wtr-lab.com/api/chapters/{novel_id}"
            )

        if status != 200 or not isinstance(chapter_payload, dict):
            raise WtrError(f"Could not load WTR-Lab chapter list (HTTP {status})")

        raw_chapters = chapter_payload.get("chapters") or []
        if not raw_chapters:
            raise WtrError("WTR-Lab returned no chapters")

        chapters = [
            ChapterInfo(
                order=int(item["order"]),
                chapter_id=str(item.get("id") or ""),
                serie_id=str(item.get("serie_id") or novel_id),
                title=str(
                    item.get("title")
                    or item.get("name")
                    or f"Chapter {item['order']}"
                ),
            )
            for item in raw_chapters
        ]
        chapters.sort(key=lambda chapter: chapter.order)

        serie_id = chapters[0].serie_id

        _, story_payload = self.browser.fetch_json(
            f"https://wtr-lab.com/api/v2/reader/terms/{novel_id}.json"
        )
        _, user_payload = self.browser.fetch_json(
            "https://wtr-lab.com/api/v2/user/config"
        )

        return NovelInfo(
            novel_id=novel_id,
            source_url=source_url,
            title=title,
            author=author,
            synopsis=synopsis,
            cover_url=cover_url,
            language=language,
            slug=slug,
            chapters=chapters,
            story_terms=make_story_terms(story_payload),
            user_terms=make_user_terms(user_payload, serie_id),
            status_label=status_label,
            unlock_count=unlock_count if unlock_count > 0 else len(chapters),
            chapter_count=chapter_count if chapter_count > 0 else len(chapters),
            raw_title=raw_title,
        )


    def chapter_open_url(self, novel: NovelInfo, chapter: ChapterInfo) -> str:
        lang = (novel.language or "en").strip("/") or "en"
        slug = novel.slug or "novel"
        return (
            f"https://wtr-lab.com/{lang}/novel/{novel.novel_id}/"
            f"{slug}/chapter-{chapter.order}"
        )

    def clear_turnstile(
        self,
        novel: NovelInfo,
        chapter: ChapterInfo,
        reason: str,
        *,
        task_id: Optional[int] = None,
        chat_id: Optional[int] = None,
        user_id: Optional[int] = None,
    ) -> None:
        """
        Open the chapter page that hit Turnstile and wait for a manual solve.
        No auto-click — that path crashes Chrome on the Oracle Linux / VNC host.
        """
        url = self.chapter_open_url(novel, chapter)
        print(f"[TURNSTILE] {reason}")
        print(f"[TURNSTILE] Opening {url} (manual solve only)")
        self.browser.wait_for_manual_access(
            url,
            reason,
            task_id=task_id,
            chat_id=chat_id,
            user_id=user_id,
            chapter_no=chapter.order,
        )

    def download_chapter(
        self,
        novel: NovelInfo,
        chapter: ChapterInfo,
        novel_dir: Path,
        *,
        task_id: Optional[int] = None,
        chat_id: Optional[int] = None,
        user_id: Optional[int] = None,
    ) -> tuple[str, str]:
        payload = {
            "translate": "ai",
            "language": novel.language,
            "raw_id": chapter.serie_id,
            "chapter_no": chapter.order,
            "retry": False,
            "force_retry": False,
            "chapter_id": chapter.chapter_id,
        }

        status, response = None, None
        max_attempts = 8
        # Origin 522 / timeout and HTTP 503: wait 2 min, then 5 min, then 10 min.
        origin_timeout_waits = (120, 300, 600)  # seconds
        origin_timeout_index = 0

        for attempt in range(1, max_attempts + 1):
            status, response = self.browser.fetch_json(
                "https://wtr-lab.com/api/reader/get",
                method="POST",
                payload=payload,
            )

            ok = (
                status == 200
                and isinstance(response, dict)
                and response.get("success")
            )
            if ok:
                break

            if is_chapter_locked(status, response):
                raise ChapterLocked(chapter.order)

            if is_chapter_waiting_crawl(status, response):
                site_msg = wtr_api_message(response) or (
                    "This chapter is waiting for crawling. Please check back later."
                )
                raise WtrError(
                    f"Chapter {chapter.order}: {site_msg}"
                )

            is_503 = status == 503
            if is_origin_timeout(status, response) or is_503:
                label = "HTTP 503" if is_503 else "522/timeout"
                if origin_timeout_index < len(origin_timeout_waits):
                    wait = origin_timeout_waits[origin_timeout_index]
                    origin_timeout_index += 1
                    mins = wait // 60
                    print(
                        f"[ORIGIN TIMEOUT] Chapter {chapter.order}: "
                        f"{label} (attempt {attempt}/{max_attempts}) — "
                        f"waiting {mins} min before retry "
                        f"({origin_timeout_index}/{len(origin_timeout_waits)})"
                    )
                    self.browser._notify(
                        "origin_timeout",
                        (
                            f"Chapter {chapter.order}: {label}. "
                            f"Retrying in {mins} minute(s)…"
                        ),
                    )
                    time.sleep(wait)
                    continue
                print(
                    f"[ORIGIN TIMEOUT] Chapter {chapter.order}: "
                    f"exhausted 2 + 5 + 10 min retries ({label})"
                )
                raise WtrError(
                    f"Chapter {chapter.order} failed: {label} "
                    "after 2 min, 5 min and 10 min retries"
                )

            blocked = is_challenge(status, response) or (
                isinstance(response, dict)
                and "turnstile" in str(response.get("message", "")).lower()
            )

            if blocked:
                self.clear_turnstile(
                    novel,
                    chapter,
                    reason=(
                        f"Chapter {chapter.order} blocked "
                        f"(attempt {attempt}/{max_attempts})"
                    ),
                    task_id=task_id if task_id is not None else self.task_id,
                    chat_id=chat_id if chat_id is not None else self.chat_id,
                    user_id=user_id if user_id is not None else self.user_id,
                )
                continue

            # Login-style rejection (not a captcha): wait for a re-login
            # instead of failing the chapter or retrying blindly.
            login_text = (
                str(response.get("message") or response.get("error") or "").lower()
                if isinstance(response, dict)
                else ""
            )
            if any(
                marker in login_text
                for marker in ("log in", "login", "sign in", "unauthorized", "unauthenticated")
            ):
                # Confirm on profile before pausing the whole queue — API text
                # can be stale/wrong while the session is still valid.
                if self.browser.is_logged_in():
                    print(
                        f"[LOGIN] Chapter {chapter.order}: API said login "
                        f"({login_text[:60]}) but profile is logged in — retrying chapter"
                    )
                    time.sleep(3.0)
                    continue
                self.browser.wait_for_login(
                    self.chapter_open_url(novel, chapter),
                    f"Chapter {chapter.order}: site asked for login ({login_text[:80]})",
                    task_id=task_id if task_id is not None else self.task_id,
                    chat_id=chat_id if chat_id is not None else self.chat_id,
                    user_id=user_id if user_id is not None else self.user_id,
                    chapter_no=chapter.order,
                )
                continue

            # Temporary site glitch: retry a few times before giving up this chapter.
            if attempt < max_attempts:
                wait = min(8.0, 2.0 * attempt)
                print(
                    f"[CHAPTER] {chapter.order}: attempt {attempt}/{max_attempts} "
                    f"failed (HTTP {status}) — retry in {wait:.0f}s"
                )
                time.sleep(wait)
                continue
            break

        if not (
            status == 200
            and isinstance(response, dict)
            and response.get("success")
        ):
            if is_origin_timeout(status, response):
                raise WtrError(
                    f"Chapter {chapter.order} failed: origin timeout (522) "
                    "after retries"
                )
            site_msg = wtr_api_message(response)
            if site_msg:
                message = site_msg
            elif isinstance(response, dict):
                keys = list(response.keys())
                message = (
                    f"empty/unknown API body (HTTP {status}, "
                    f"success={response.get('success')!r}, keys={keys[:12]})"
                )
            else:
                message = f"HTTP {status}, non-JSON body: {str(response)[:180]}"
            raise WtrError(f"Chapter {chapter.order} failed: {message}")

        global session_state
        session_state = "ok"

        data, raw_body = locate_chapter_body(response)

        # WTR-Lab now often returns only a pointer ("content_url") instead of
        # the body. Fetch it with the same browser session, then look for the
        # body (and glossary/images/patch) inside that second response.
        content_url = (
            response.get("content_url") if isinstance(response, dict) else None
        )
        if raw_body is None and isinstance(content_url, str) and content_url.strip():
            content_response = self._fetch_content_url(
                novel,
                chapter,
                content_url.strip(),
                task_id=task_id,
                chat_id=chat_id,
                user_id=user_id,
            )
            data, raw_body = self._body_from_content_response(content_response)
            if raw_body is None:
                dump_chapter_debug(chapter.order, {
                    "first_response": response,
                    "content_response": content_response,
                })
                top_keys = (
                    list(content_response.keys())
                    if isinstance(content_response, dict)
                    else type(content_response).__name__
                )
                print(
                    f"[NO BODY] Chapter {chapter.order}: content_url response "
                    f"keys={top_keys}"
                )
                raise WtrError(
                    f"Chapter {chapter.order}: content_url returned no readable "
                    f"body (keys: {top_keys}). Debug copy saved under data/debug/."
                )

        if raw_body is None:
            dump_path = dump_chapter_debug(chapter.order, response)
            top_keys = list(response.keys()) if isinstance(response, dict) else []
            inner_keys = list(data.keys()) if isinstance(data, dict) else []
            print(
                f"[NO BODY] Chapter {chapter.order}: response keys={top_keys} "
                f"data.data keys={inner_keys} dump={dump_path}"
            )
            raise WtrError(
                f"Chapter {chapter.order}: site returned no readable body "
                f"(response keys: {top_keys}, data keys: {inner_keys}). "
                "Debug copy saved under data/debug/."
            )
        body_lines = decrypt_body(raw_body)
        actual_title = str(
            nested(response, "chapter", "title", default=None)
            or chapter.title
        )

        xhtml = self.build_xhtml(
            novel=novel,
            chapter=chapter,
            title=actual_title,
            body_lines=body_lines,
            data=data,
            novel_dir=novel_dir,
        )
        return actual_title, xhtml

    def _fetch_content_url(
        self,
        novel: NovelInfo,
        chapter: ChapterInfo,
        content_url: str,
        *,
        task_id: Optional[int] = None,
        chat_id: Optional[int] = None,
        user_id: Optional[int] = None,
    ) -> Any:
        """GET the chapter content pointer returned by /api/reader/get."""
        if content_url.startswith("//"):
            url = "https:" + content_url
        elif content_url.startswith("/"):
            url = "https://wtr-lab.com" + content_url
        else:
            url = content_url

        status, content = None, None
        for attempt in range(1, 4):
            status, content = self.browser.fetch_json(url, method="GET")
            if status == 200:
                return content

            blocked = is_challenge(status, content) or (
                isinstance(content, dict)
                and "turnstile" in str(content.get("message", "")).lower()
            )
            if blocked:
                self.clear_turnstile(
                    novel,
                    chapter,
                    reason=(
                        f"Chapter {chapter.order} content blocked "
                        f"(attempt {attempt}/3)"
                    ),
                    task_id=task_id if task_id is not None else self.task_id,
                    chat_id=chat_id if chat_id is not None else self.chat_id,
                    user_id=user_id if user_id is not None else self.user_id,
                )
                continue

            if status in (0, 502, 503, 504, 522, 524):
                time.sleep(5 * attempt)
                continue
            break

        dump_chapter_debug(
            chapter.order, {"content_url": url, "status": status, "response": content}
        )
        raise WtrError(
            f"Chapter {chapter.order}: content request failed "
            f"(HTTP {status}). Debug copy saved under data/debug/."
        )

    @staticmethod
    def _body_from_content_response(content: Any) -> tuple[dict, Any]:
        """
        The content endpoint may return JSON in several shapes, or the raw
        encrypted string itself ("arr:..." / "str:...", wrapped in raw_text).
        """
        if isinstance(content, dict) and set(content.keys()) == {"raw_text"}:
            text = str(content.get("raw_text") or "").strip()
            if text.startswith(("arr:", "str:")):
                return {}, text
            # Plain JSON-in-text or plain text.
            try:
                content = json.loads(text)
            except Exception:
                return {}, ([text] if text else None)

        if isinstance(content, str):
            text = content.strip()
            return {}, (text if text else None)

        if isinstance(content, list):
            return {}, (content if content else None)

        return locate_chapter_body(content)

    def build_xhtml(
        self,
        novel: NovelInfo,
        chapter: ChapterInfo,
        title: str,
        body_lines: list[str],
        data: dict,
        novel_dir: Path,
    ) -> str:
        glossary_data = data.get("glossary_data") or {}
        if isinstance(glossary_data, str):
            try:
                glossary_data = json.loads(glossary_data)
            except Exception:
                glossary_data = {}
        if not isinstance(glossary_data, dict):
            glossary_data = {}

        prepared_terms = prepare_chapter_terms(
            glossary_data.get("terms") or [],
            novel.story_terms,
            novel.user_terms,
        )

        images = data.get("images", []) or []
        image_index = 0
        fragments = [
            f"<h1>{html.escape(str(chapter.order))}: {html.escape(title)}</h1>"
        ]
        unresolved = 0

        for raw_line in body_lines:
            if raw_line == "[image]":
                image_url = images[image_index] if image_index < len(images) else ""
                image_index += 1

                if image_url:
                    embedded = self.cache_image(str(image_url), novel_dir)
                    source = embedded if embedded else str(image_url)
                    fragments.append(
                        f'<p><img src="{html.escape(source, quote=True)}" alt="image" /></p>'
                    )
                continue

            text = apply_glossary_to_text(
                str(raw_line),
                prepared_terms,
                novel.user_terms,
                data.get("patch") or [],
            )
            if PLACEHOLDER_RE.search(text):
                unresolved += 1
            fragments.append(f"<p>{html.escape(text)}</p>")

        if unresolved:
            print(
                f"[GLOSSARY] Chapter {chapter.order}: {unresolved} line(s) still "
                f"contain placeholder glyphs after replacement "
                f"({len(prepared_terms)} glossary slots)"
            )
        elif prepared_terms:
            print(
                f"[GLOSSARY] Chapter {chapter.order}: applied "
                f"{len(prepared_terms)} glossary slot(s)"
            )

        # Body fragment only — ebooklib EpubHtml wraps content itself.
        return "\n".join(fragments)

    def cache_image(self, image_url: str, novel_dir: Path) -> Optional[str]:
        try:
            image_bytes = self.browser.fetch_bytes(image_url)
            if not image_bytes:
                return None

            suffix = Path(image_url.split("?")[0]).suffix.lower()
            if suffix not in (".jpg", ".jpeg", ".png", ".webp", ".gif"):
                suffix = ".jpg"

            file_name = hashlib.sha1(image_url.encode("utf-8")).hexdigest() + suffix
            file_path = novel_dir / "images" / file_name

            if not file_path.exists():
                atomic_write_bytes(file_path, image_bytes)

            # Chapter XHTML lives in EPUB's chapters/ folder.
            return f"../images/{file_name}"
        except Exception as error:
            print(f"[IMAGE WARNING] {image_url}: {error}")
            return None


# ---------------------------------------------------------------------------
# EPUB construction from permanent XHTML cache
# ---------------------------------------------------------------------------

# Chapters per volume in the EPUB TOC (expand/collapse in readers).
EPUB_CHAPTERS_PER_VOLUME = 100

# Footer branding on the EPUB intro page (same bot/group as the other crawler).
EPUB_BOT_URL = "https://t.me/patior_bot"
EPUB_BOT_LABEL = "@patior_bot"
EPUB_GROUP_URL = "https://t.me/novelsFinder"
EPUB_GROUP_LABEL = "Webnovels and Wuxia Novels"


def _chapter_placeholder_fragment(chapter_no: int, title: str, reason: str) -> str:
    """Body fragment only — ebooklib wraps EpubHtml itself; full documents break it."""
    safe_title = html.escape(str(title or f"Chapter {chapter_no}")[:200])
    safe_reason = html.escape(reason[:200])
    return (
        f"<h1>{safe_title}</h1>\n"
        f"<p><em>Chapter content unavailable ({safe_reason}).</em></p>"
    )


def _decode_chapter_bytes(raw: bytes) -> str:
    raw = (raw or b"").replace(b"\x00", b"")
    if raw.startswith(b"\xff\xfe") or raw.startswith(b"\xfe\xff"):
        try:
            return raw.decode("utf-16")
        except Exception:
            return raw.decode("utf-8", errors="replace")
    if raw.startswith(b"\xef\xbb\xbf"):
        raw = raw[3:]
    return raw.decode("utf-8", errors="replace")


def _chapter_text_is_empty(text: str) -> bool:
    """True if file has no usable body (whitespace / tags-only count as empty)."""
    if not text or not text.strip():
        return True
    # Strip tags; if almost nothing remains, treat as empty.
    plain = re.sub(r"<[^>]+>", " ", text)
    plain = re.sub(r"\s+", " ", plain).strip()
    return len(plain) < 3


def chapter_cache_is_bad(path: Path) -> bool:
    """Disk chapter unusable for EPUB (missing, empty, or whitespace/NUL-only)."""
    try:
        if not path.is_file():
            return True
        raw = path.read_bytes()
        if not raw:
            return True
        return _chapter_text_is_empty(_decode_chapter_bytes(raw))
    except Exception:
        return True


def purge_chapter_cache(novel_id: str, chapter_no: int, path: Optional[Path] = None):
    """Delete a bad chapter file and its SQLite row so it can be re-downloaded."""
    if path is None:
        path = LIBRARY_DIR / str(novel_id) / "chapters" / f"{chapter_no:05}.xhtml"
    try:
        if path.is_file():
            path.unlink()
            print(f"[CACHE] Deleted bad chapter file {path.name}")
    except OSError as error:
        print(f"[CACHE] Could not delete {path}: {error}")
    if use_chapter_pg():
        try:
            conn = chapter_db()
            try:
                with conn.cursor() as cur:
                    cur.execute(
                        "DELETE FROM chapters WHERE novel_id=%s AND chapter_no=%s",
                        (str(novel_id), int(chapter_no)),
                    )
                conn.commit()
            finally:
                conn.close()
        except Exception as error:
            print(f"[CACHE] PG purge error ch {chapter_no}: {error}")
    else:
        conn = local_db()
        try:
            conn.execute(
                "DELETE FROM chapters WHERE novel_id=? AND chapter_no=?",
                (str(novel_id), int(chapter_no)),
            )
            conn.commit()
        except Exception as error:
            print(f"[CACHE] DB purge error ch {chapter_no}: {error}")
        finally:
            conn.close()


def find_bad_cached_chapters(novel_id: str, start: int, end: int) -> list[int]:
    """
    Return chapter numbers in [start, end] that were previously cached (DB row
    and/or a file on disk) but are empty/corrupt and need a free re-download.

    Purely missing chapters (never downloaded) are NOT included — those are
    normal first-time fetches gated by CHAPTER_CAP in the main download loop.
    Repair must not treat "no file" as corrupt or it bypasses the daily limit.
    """
    bad: list[int] = []
    present = {}
    if use_chapter_pg():
        try:
            conn = chapter_db()
            try:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        SELECT chapter_no, xhtml_path FROM chapters
                        WHERE novel_id=%s AND chapter_no BETWEEN %s AND %s
                        ORDER BY chapter_no ASC
                        """,
                        (str(novel_id), start, end),
                    )
                    for row in cur.fetchall():
                        present[int(row[0])] = Path(row[1])
            finally:
                conn.close()
        except Exception as error:
            print(f"[CACHE BAD PG] {error}")
    else:
        conn = local_db()
        try:
            rows = conn.execute(
                """
                SELECT chapter_no, xhtml_path FROM chapters
                WHERE novel_id=? AND chapter_no BETWEEN ? AND ?
                ORDER BY chapter_no ASC
                """,
                (str(novel_id), start, end),
            ).fetchall()
            for row in rows:
                present[int(row["chapter_no"])] = Path(row["xhtml_path"])
        finally:
            conn.close()

    for n in range(start, end + 1):
        db_path = present.get(n)
        disk_path = LIBRARY_DIR / str(novel_id) / "chapters" / f"{n:05}.xhtml"
        # Never downloaded (no DB row, no file) → not a repair candidate.
        if db_path is None and not disk_path.is_file():
            continue
        candidate = db_path if db_path is not None else disk_path
        if chapter_cache_is_bad(candidate):
            bad.append(n)
    return bad


def repair_bad_chapters(
    client: "WtrLabClient",
    novel: "NovelInfo",
    novel_dir: Path,
    start: int,
    end: int,
    progress_message=None,
) -> list[int]:
    """
    Re-download chapters that were already cached but are empty/corrupt on disk.

    Does not pull chapters that were never downloaded (CHAPTER_CAP applies there
    in the main loop). Re-fetches here are free retries for bot/cache faults;
    register_chapter_pull INSERT OR IGNORE means a prior successful pull is not
    double-counted.

    Returns list of chapter numbers still bad after the attempt.
    """
    bad_nos = find_bad_cached_chapters(novel.novel_id, start, end)
    if not bad_nos:
        return []

    print(
        f"[REPAIR] {len(bad_nos)} bad/empty chapter(s) in {start}-{end}: "
        f"{bad_nos[:20]}{'…' if len(bad_nos) > 20 else ''}"
    )
    edit_progress(
        progress_message,
        (
            f"📖 {novel.title}\n\n"
            f"🔧 Found {len(bad_nos)} empty/corrupt chapter(s).\n"
            "Deleting and re-downloading before building EPUB..."
        ),
    )

    by_order = {ch.order: ch for ch in novel.chapters}
    still_bad: list[int] = []

    for i, chapter_no in enumerate(bad_nos, start=1):
        path = novel_dir / "chapters" / f"{chapter_no:05}.xhtml"
        purge_chapter_cache(novel.novel_id, chapter_no, path)

        chapter = by_order.get(chapter_no)
        if not chapter:
            print(f"[REPAIR] Chapter {chapter_no} not in novel TOC — skip")
            still_bad.append(chapter_no)
            continue

        try:
            title, xhtml = client.download_chapter(novel, chapter, novel_dir)
            if not xhtml or _chapter_text_is_empty(xhtml):
                raise WtrError(
                    f"Re-download of chapter {chapter_no} returned empty body"
                )
            atomic_write_text(path, xhtml)
            cache_chapter(
                novel.novel_id,
                chapter.order,
                chapter.chapter_id,
                title,
                path,
            )
            if chapter_cache_is_bad(path):
                print(f"[REPAIR] Chapter {chapter_no} still bad after download")
                still_bad.append(chapter_no)
            else:
                print(
                    f"[REPAIR] Re-downloaded chapter {chapter_no} "
                    f"({i}/{len(bad_nos)})"
                )
        except ChapterLocked:
            print(f"[REPAIR] Chapter {chapter_no} is AI-locked — cannot fill")
            still_bad.append(chapter_no)
        except Exception as error:
            print(
                f"[REPAIR] Chapter {chapter_no} failed: "
                f"{type(error).__name__}: {error}"
            )
            still_bad.append(chapter_no)

        edit_progress(
            progress_message,
            (
                f"📖 {novel.title}\n\n"
                f"🔧 Repairing empty/corrupt chapters…\n\n"
                f"📊 Progress: {i}/{len(bad_nos)}\n"
                f"📖 Currently at: Chapter {chapter_no}\n"
                f"📌 Still to check: {max(0, len(bad_nos) - i)}"
            ),
        )

        # Same full throttle as the main download loop (no artificial 5s cap).
        wait = next_chapter_throttle()
        if wait > 0 and i < len(bad_nos):
            print(
                f"[REPAIR PACER] Waiting {wait:.1f}s before next repair chapter"
            )
            time.sleep(wait)

    if still_bad:
        print(f"[REPAIR] Still bad after re-download: {still_bad}")
    else:
        print("[REPAIR] All bad chapters repaired")
    return still_bad


def _load_chapter_xhtml(path: Path, chapter_no: int, title: str) -> str:
    """
    Load chapter body HTML for ebooklib EpubHtml.

    Returns a non-empty HTML *fragment* (not a full document). ebooklib wraps
    fragments; feeding full <html> documents can yield ParserError: Document is empty.
    """
    try:
        raw = path.read_bytes()
    except Exception as error:
        print(f"[EPUB] Could not read chapter {chapter_no}: {error}")
        return _chapter_placeholder_fragment(chapter_no, title, "read error")

    text = _decode_chapter_bytes(raw).strip()
    if _chapter_text_is_empty(text):
        print(f"[EPUB] Chapter {chapter_no} empty after cleanup — placeholder")
        return _chapter_placeholder_fragment(chapter_no, title, "empty file")

    # If a full document was stored, use body inner HTML only.
    lower = text[:400].lower()
    if "<html" in lower or "<?xml" in lower or "<body" in lower:
        try:
            from lxml import html as lxml_html

            doc = lxml_html.fromstring(text.encode("utf-8"))
            body = doc.find(".//body")
            if body is not None:
                inner = "".join(
                    [
                        lxml_html.tostring(c, encoding="unicode")
                        if not isinstance(c, str)
                        else c
                        for c in body
                    ]
                )
                if body.text:
                    inner = body.text + inner
                if inner and not _chapter_text_is_empty(inner):
                    return inner
        except Exception:
            pass

    return text


def build_epub(
    novel: NovelInfo,
    novel_dir: Path,
    start: int,
    end: int,
) -> Path:
    artifacts_dir = novel_dir / "artifacts"
    artifacts_dir.mkdir(parents=True, exist_ok=True)

    selected: list[tuple[int, str, Path]] = []

    rows = []
    if use_chapter_pg():
        try:
            conn = chapter_db()
            try:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        SELECT chapter_no, title, xhtml_path
                        FROM chapters
                        WHERE novel_id=%s AND chapter_no BETWEEN %s AND %s
                        ORDER BY chapter_no ASC
                        """,
                        (novel.novel_id, start, end),
                    )
                    rows = cur.fetchall()
            finally:
                conn.close()
            for row in rows:
                path = Path(row[2])
                ch_no, title = int(row[0]), row[1]
                if path.is_file() and path.stat().st_size > 0:
                    selected.append((ch_no, title, path))
                elif path.is_file():
                    print(f"[EPUB] Zero-byte chapter file: {path.name} (ch {ch_no})")
                    selected.append((ch_no, title, path))
        except Exception as error:
            print(f"[EPUB PG] {error}")
    else:
        conn = local_db()
        try:
            rows = conn.execute(
                """
                SELECT chapter_no, title, xhtml_path
                FROM chapters
                WHERE novel_id=? AND chapter_no BETWEEN ? AND ?
                ORDER BY chapter_no ASC;
                """,
                (novel.novel_id, start, end),
            ).fetchall()
            for row in rows:
                path = Path(row["xhtml_path"])
                if path.is_file() and path.stat().st_size > 0:
                    selected.append((row["chapter_no"], row["title"], path))
                elif path.is_file():
                    print(
                        f"[EPUB] Zero-byte chapter file: {path.name} "
                        f"(ch {row['chapter_no']})"
                    )
                    selected.append((row["chapter_no"], row["title"], path))
        finally:
            conn.close()

    if not selected:
        raise WtrError("No cached chapters were available to build an EPUB")

    display_title = f"{novel.title} c{start}-{end}"
    output_path = artifacts_dir / f"{epub_stem(novel.title, start, end)}.epub"
    temporary_path = output_path.with_suffix(".epub.tmp")
    book = epub.EpubBook()
    book.set_identifier(f"wtr-{novel.novel_id}-{start}-{end}")
    book.set_title(display_title)
    book.set_language("en")
    book.add_author(novel.author or "Unknown")

    if novel.synopsis:
        book.add_metadata("DC", "description", novel.synopsis)

    style = epub.EpubItem(
        uid="style",
        file_name="style/style.css",
        media_type="text/css",
        content=b"""
            body { line-height: 1.55; margin: 5%; }
            h1 { text-align: center; margin: 1.5em 0; }
            h2 { text-align: center; margin: 1.2em 0; opacity: 0.9; }
            p { margin: 0.8em 0; }
            img { max-width: 100%; height: auto; }
            .footer { margin-top: 2em; font-size: 0.95em; opacity: 0.9; }
        """,
    )
    book.add_item(style)

    intro = epub.EpubHtml(
        uid="intro",
        file_name="intro.xhtml",
        title="Info",
        content=(
            f"<h1>{html.escape(novel.title)}</h1>"
            f"<p><b>Author:</b> {html.escape(novel.author or 'Unknown')}</p>"
            f"<p><b>Status:</b> {html.escape(novel.status_label)}</p>"
            f"<p><b>AI-Unlock:</b> {novel.unlock_count}/"
            f"{novel.chapter_count or len(novel.chapters)}</p>"
            f"<p><b>Source:</b> "
            f'<a href="{html.escape(novel.source_url, quote=True)}">'
            f"{html.escape(novel.source_url)}</a></p>"
            f"<h2>Synopsis</h2>"
            f"<p>{html.escape(novel.synopsis or 'No synopsis available.')}</p>"
            f'<div class="footer">'
            f"<p><b>Source:</b> "
            f'<a href="{html.escape(novel.source_url, quote=True)}">'
            f"{html.escape(novel.source_url)}</a></p>"
            f"<p>Made by Telegram bot:<br/>"
            f'<a href="{html.escape(EPUB_BOT_URL, quote=True)}">'
            f"{html.escape(EPUB_BOT_LABEL)}</a></p>"
            f"<p>Telegram group:<br/>"
            f'<a href="{html.escape(EPUB_GROUP_URL, quote=True)}">'
            f"{html.escape(EPUB_GROUP_LABEL)}</a></p>"
            f"</div>"
        ),
    )
    intro.add_link(href="style/style.css", rel="stylesheet", type="text/css")
    book.add_item(intro)

    # Add cached inline images to EPUB (skip empty files).
    images_dir = novel_dir / "images"
    if images_dir.exists():
        for image_path in images_dir.iterdir():
            if not image_path.is_file() or image_path.stat().st_size <= 0:
                continue

            mime, _ = mimetypes.guess_type(image_path.name)
            book.add_item(
                epub.EpubItem(
                    uid=f"image-{image_path.stem}",
                    file_name=f"images/{image_path.name}",
                    media_type=mime or "image/jpeg",
                    content=image_path.read_bytes(),
                )
            )

    # Cover is optional and cached locally.
    cover_path = find_cached_cover(novel_dir)
    if cover_path and cover_path.is_file() and cover_path.stat().st_size > 0:
        book.set_cover("cover" + cover_path.suffix, cover_path.read_bytes())

    # Group chapters into volumes of EPUB_CHAPTERS_PER_VOLUME for a nested TOC
    # (readers can expand/collapse each volume like a folded code block).
    volume_sections: list[tuple[Any, list]] = []
    spine_items: list = []
    per_vol = max(1, EPUB_CHAPTERS_PER_VOLUME)

    current_vol_no: int | None = None
    current_vol_page = None
    current_chapter_items: list = []

    def flush_volume():
        nonlocal current_vol_page, current_chapter_items
        if current_vol_page is None or not current_chapter_items:
            current_vol_page = None
            current_chapter_items = []
            return
        volume_sections.append(
            (current_vol_page, list(current_chapter_items))
        )
        current_vol_page = None
        current_chapter_items = []

    for chapter_no, chapter_title, xhtml_path in selected:
        # Volume index from absolute chapter number so ranges stay stable
        # across partial downloads (e.g. ch 101 always lands in Volume 2).
        vol_no = ((max(1, chapter_no) - 1) // per_vol) + 1
        vol_start = (vol_no - 1) * per_vol + 1
        vol_end = vol_no * per_vol

        if vol_no != current_vol_no:
            flush_volume()
            current_vol_no = vol_no
            vol_title = f"Volume {vol_no} (Ch. {vol_start}–{vol_end})"
            current_vol_page = epub.EpubHtml(
                uid=f"volume-{vol_no}",
                file_name=f"volumes/volume_{vol_no:03}.xhtml",
                title=vol_title,
                content=(
                    f"<h1>{html.escape(vol_title)}</h1>"
                    f"<p>{html.escape(novel.title)}</p>"
                ),
            )
            current_vol_page.add_link(
                href="../style/style.css",
                rel="stylesheet",
                type="text/css",
            )
            book.add_item(current_vol_page)
            spine_items.append(current_vol_page)

        item = epub.EpubHtml(
            uid=f"chapter-{chapter_no}",
            file_name=f"chapters/{chapter_no:05}.xhtml",
            title=f"{chapter_no}: {chapter_title}",
            content=_load_chapter_xhtml(
                xhtml_path, chapter_no, chapter_title or f"Chapter {chapter_no}"
            ),
        )
        item.add_link(href="../style/style.css", rel="stylesheet", type="text/css")
        book.add_item(item)
        current_chapter_items.append(item)
        spine_items.append(item)

    flush_volume()

    # Nested TOC: Info, then each Volume with its chapters underneath.
    toc_entries: list = [intro]
    for vol_page, ch_items in volume_sections:
        toc_entries.append(
            (epub.Section(vol_page.title), (vol_page, *ch_items))
        )

    book.toc = toc_entries
    book.spine = ["cover", intro, "nav", *spine_items]
    book.add_item(epub.EpubNcx())
    book.add_item(epub.EpubNav())

    try:
        epub.write_epub(str(temporary_path), book, {})
    except Exception as error:
        # Surface a clearer clue than bare "Document is empty".
        print(
            f"[EPUB WRITE ERROR] {type(error).__name__}: {error} "
            f"(novel={novel.novel_id} chapters={len(selected)} "
            f"range={start}-{end})"
        )
        if temporary_path.exists():
            try:
                temporary_path.unlink()
            except OSError:
                pass
        raise

    shutil.move(str(temporary_path), str(output_path))

    return output_path


# ---------------------------------------------------------------------------
# Telegram delivery + auto-delete (mirrors main.py intent)
# ---------------------------------------------------------------------------

# Short notices + task notices → 24h (per operator request).
DELETE_AFTER_NOTICE = 24 * 60 * 60
# Long listings (/queue, /logs, /mytasks, /start help, etc.) → 1 hour.
DELETE_AFTER_LIST = 60 * 60

# user_id -> list[(chat_id, message_id)]
_bot_messages: dict[int, list[tuple[int, int]]] = {}


def telegram_call(function, *args, **kwargs):
    try:
        return function(*args, **kwargs)
    except Exception as error:
        log.exception("[TELEGRAM ERROR] %s: %s", type(error).__name__, error)
        return None


def delete_message_later(chat_id, message_id, delay: int):
    def _run():
        time.sleep(max(0, int(delay)))
        try:
            bot.delete_message(chat_id, message_id)
        except Exception:
            pass
        for uid, entries in list(_bot_messages.items()):
            _bot_messages[uid] = [
                (c, m) for c, m in entries if not (c == chat_id and m == message_id)
            ]

    threading.Thread(target=_run, daemon=True).start()


def track_and_autodelete(msg, user_id: int | None, delay: int):
    """Remember a bot message and delete it after *delay* seconds."""
    if not msg:
        return msg
    try:
        chat_id = msg.chat.id
        mid = msg.message_id
    except Exception:
        return msg
    if user_id is not None:
        _bot_messages.setdefault(int(user_id), []).append((chat_id, mid))
    delete_message_later(chat_id, mid, delay)
    return msg


def cleanup_bot_message(chat_id, message_id):
    """Delete immediately (e.g. progress message when the job ends)."""
    if not message_id:
        return
    try:
        bot.delete_message(chat_id, message_id)
    except Exception:
        pass


def send_notice(
    chat_id,
    text: str,
    *,
    user_id: int | None = None,
    parse_mode: str | None = None,
    delay: int = DELETE_AFTER_NOTICE,
):
    """Short / task notice — auto-deletes (default 24h)."""
    msg = telegram_call(
        bot.send_message,
        chat_id,
        text,
        parse_mode=parse_mode,
        link_preview_options=LINK_PREVIEW_OFF,
    )
    return track_and_autodelete(msg, user_id, delay)


def reply_notice(
    message,
    text: str,
    *,
    parse_mode: str | None = None,
    delay: int = DELETE_AFTER_NOTICE,
    **kwargs,
):
    """reply_to variant for short/task notices (24h). Accepts reply_markup etc."""
    kwargs.setdefault("link_preview_options", LINK_PREVIEW_OFF)
    try:
        msg = bot.reply_to(message, text, parse_mode=parse_mode, **kwargs)
    except Exception as error:
        log.exception("reply_notice failed (%s: %s)", type(error).__name__, error)
        return None
    uid = message.from_user.id if message.from_user else None
    return track_and_autodelete(msg, uid, delay)


def send_list_temp(
    chat_id,
    text: str,
    *,
    user_id: int | None = None,
    parse_mode: str | None = None,
    delay: int = DELETE_AFTER_LIST,
):
    """Long listing — auto-deletes after 1 hour."""
    msg = telegram_call(
        bot.send_message,
        chat_id,
        text,
        parse_mode=parse_mode,
        link_preview_options=LINK_PREVIEW_OFF,
    )
    return track_and_autodelete(msg, user_id, delay)


def edit_progress(message, text: str):
    if not message:
        return

    try:
        bot.edit_message_text(
            text,
            chat_id=message.chat.id,
            message_id=message.message_id,
            link_preview_options=LINK_PREVIEW_OFF,
        )
    except Exception:
        pass


def send_cover_photo(chat_id, cover_path: Path, caption: str):
    """
    Telegram sendPhoto is picky: WEBP sometimes fails, HTML captions can fail,
    and the file name from a Path.open() is the full local path. Try photo
    first, then the same bytes as a document so the cover still arrives.
    """
    suffix = cover_path.suffix.lower() or ".jpg"
    if suffix == ".jpeg":
        suffix = ".jpg"
    file_name = f"cover{suffix}"
    plain_caption = re.sub(r"<[^>]+>", "", caption)

    attempts = [
        {"parse_mode": "HTML", "caption": caption, "as_photo": True},
        {"parse_mode": None, "caption": plain_caption, "as_photo": True},
        {"parse_mode": "HTML", "caption": caption, "as_photo": False},
    ]

    for attempt in attempts:
        try:
            with cover_path.open("rb") as handle:
                payload = (file_name, handle)
                if attempt["as_photo"]:
                    message = bot.send_photo(
                        chat_id,
                        payload,
                        caption=attempt["caption"],
                        parse_mode=attempt["parse_mode"],
                        timeout=120,
                    )
                else:
                    message = bot.send_document(
                        chat_id,
                        payload,
                        caption=attempt["caption"],
                        parse_mode=attempt["parse_mode"],
                        visible_file_name=file_name,
                        timeout=120,
                    )
            if message:
                return message
        except Exception as error:
            print(
                f"[COVER SEND] "
                f"{'photo' if attempt['as_photo'] else 'document'} "
                f"failed: {type(error).__name__}: {error}"
            )
    return None


def format_skipped_chapters_note(
    skipped: list[tuple[int, str]],
    *,
    limit: int = 40,
    streak_stopped: bool = False,
) -> str:
    """
    Chapters left out of the EPUB and why (site-side, not a bot bug).

    - Download continued after gaps → list each skipped chapter.
    - Stopped on a long consecutive streak → only the first failure in that
      streak (e.g. locked from ch 12), not ch 12–19 one by one.
    """
    if not skipped:
        return ""

    if streak_stopped:
        # First chapter of the trailing consecutive run
        i = len(skipped) - 1
        while i > 0 and skipped[i][0] == skipped[i - 1][0] + 1:
            i -= 1
        first_order, first_reason = skipped[i]
        short = (first_reason or "unavailable").strip()
        if len(short) > 140:
            short = short[:137] + "…"
        return (
            f"ℹ️ Stopped from <b>chapter {first_order}</b>: "
            f"{html.escape(short)}\n\n"
            "Later chapters in a row failed the same way, so they are not listed "
            "one by one.\n\n"
            "Please open that chapter on <b>wtr-lab.com</b> and confirm it is "
            "actually accessible (unlocked / finished crawling). "
            "If the site shows the same error, it is not a bot bug. "
            "When it works on the site, /continue or resend the link."
        )

    lines = [
        "ℹ️ <b>Some chapters were skipped</b> (left out of this EPUB on purpose):",
        "",
    ]
    for order, reason in skipped[:limit]:
        short = (reason or "unavailable").strip()
        if len(short) > 120:
            short = short[:117] + "…"
        lines.append(f"• Ch {order}: {html.escape(short)}")
    if len(skipped) > limit:
        lines.append(f"• …and {len(skipped) - limit} more")
    lines.append("")
    lines.append(
        "Please open those chapters on <b>wtr-lab.com</b> and confirm they are "
        "actually accessible (unlocked / finished crawling). "
        "If the site shows the same error, it is not a bot bug. "
        "Unlock or wait, then /continue or resend the link to fill gaps."
    )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Large EPUBs: split for Telegram + full-file fallback hosts (Gofile / Buzzheavier)
# ---------------------------------------------------------------------------

def _with_retries(fn, file_path, attempts: int = 2, pause: float = 10.0):
    """Run an uploader (returns (url, err)); retry once on failure."""
    result = (None, "not run")
    for attempt in range(1, attempts + 1):
        result = fn(file_path)
        if result[0]:
            return result
        print(f"[EXT UPLOAD] {fn.__name__} attempt {attempt}/{attempts} failed: {result[1]}")
        if attempt < attempts:
            time.sleep(pause)
    return result


def upload_to_gofile(file_path) -> tuple[Optional[str], Optional[str]]:
    try:
        with open(file_path, "rb") as file_handle:
            response = requests.post(
                "https://upload.gofile.io/uploadfile",
                files={"file": (os.path.basename(file_path), file_handle)},
                timeout=(30, 1800),
            )
        response.raise_for_status()
        data = response.json().get("data", {})
        url = data.get("downloadPage") or data.get("pageLink") or data.get("page_link")
        if not url:
            raise RuntimeError(f"Gofile returned no download URL: {data}")
        return url, None
    except Exception as error:
        return None, str(error)


def upload_to_buzzheavier(file_path) -> tuple[Optional[str], Optional[str]]:
    filename = urllib.parse.quote(os.path.basename(file_path), safe="")
    upload_url = f"https://w.buzzheavier.com/{filename}"
    try:
        with open(file_path, "rb") as file_handle:
            response = requests.put(
                upload_url,
                data=file_handle,
                timeout=(30, 1800),
            )
        response.raise_for_status()
        data = response.json().get("data", {})
        file_id = data.get("id")
        if not file_id:
            raise RuntimeError(f"Buzzheavier returned no file ID: {data}")
        return f"https://buzzheavier.com/{file_id}", None
    except Exception as error:
        return None, str(error)


def start_external_upload(file_path) -> dict:
    """Start Gofile + Buzzheavier uploads in background threads (in parallel)."""
    state: dict = {
        "gofile": (None, "not started"),
        "buzzheavier": (None, "not started"),
        "threads": [],
    }

    def _run(key, fn):
        try:
            state[key] = _with_retries(fn, file_path)
        except Exception as error:
            state[key] = (None, str(error))

    for key, fn in (("gofile", upload_to_gofile), ("buzzheavier", upload_to_buzzheavier)):
        thread = threading.Thread(
            target=_run, args=(key, fn), name=f"ext-upload-{key}", daemon=True
        )
        thread.start()
        state["threads"].append(thread)
    return state


def finish_external_upload(state: dict) -> tuple[Optional[str], Optional[str]]:
    """Wait for both uploads. Returns (gofile_url, buzzheavier_url)."""
    for thread in state.get("threads", []):
        thread.join()
    gofile_url, gofile_err = state["gofile"]
    buzz_url, buzz_err = state["buzzheavier"]
    if gofile_err:
        print(f"[GOFILE] {gofile_err}")
    if buzz_err:
        print(f"[BUZZHEAVIER] {buzz_err}")
    # Users only see the links that worked; tell the admin why one is missing.
    failures = []
    if not gofile_url:
        failures.append(f"Gofile: {gofile_err}")
    if not buzz_url:
        failures.append(f"Buzzheavier: {buzz_err}")
    if failures:
        try:
            notify_admin(
                f"⚠️ <b>External upload failed</b> on <code>{html.escape(str(SERVER_ID))}</code>\n"
                + "\n".join(html.escape(str(f))[:300] for f in failures)
            )
        except Exception:
            pass
    return gofile_url, buzz_url


def build_epub_parts(
    novel: NovelInfo,
    novel_dir: Path,
    start: int,
    end: int,
    total_bytes: int,
    max_bytes: int,
) -> list[tuple[Path, int, int]]:
    """Rebuild a big EPUB as several smaller EPUBs by chapter range.

    Starts with an even split by chapter number, then halves any part that is
    still over max_bytes. Returns [(path, part_start, part_end), ...] in order.
    """
    parts: list[tuple[Path, int, int]] = []

    def build_span(s: int, e: int) -> None:
        path = build_epub(novel, novel_dir, s, e)
        if path.stat().st_size > max_bytes and e > s:
            path.unlink(missing_ok=True)
            mid = (s + e) // 2
            build_span(s, mid)
            build_span(mid + 1, e)
        else:
            parts.append((path, s, e))

    n_parts = max(2, math.ceil(total_bytes / (max_bytes * 0.9)))
    span = max(1, math.ceil((end - start + 1) / n_parts))
    s = start
    while s <= end:
        e = min(end, s + span - 1)
        try:
            build_span(s, e)
        except WtrError as error:
            # A span with no cached chapters (gap) is simply skipped.
            print(f"[EPUB SPLIT] skipping c{s}-{e}: {error}")
        s = e + 1
    parts.sort(key=lambda item: item[1])
    return parts


def send_completed_files(
    task: dict,
    novel: NovelInfo,
    novel_dir: Path,
    epub_path: Path,
    display_title: str,
    skipped_chapters: Optional[list[tuple[int, str]]] = None,
):
    chat_id = task["chat_id"]
    source_url = novel.source_url

    posted_ids: list[int] = []

    cover_path = find_cached_cover(novel_dir)
    status_text = novel.status_label or "Unknown"
    info_caption = (
        f"📖 <b>{html.escape(novel.title)}</b>\n"
        f"📌 <b>Status:</b> {html.escape(status_text)}\n"
        f"👤 <b>Author:</b> {html.escape(novel.author or 'Unknown')}\n"
        f"🔍 <b>Chapters:</b> {html.escape(display_title)}\n"
        f'🌐 <b>Source:</b> <a href="{html.escape(source_url, quote=True)}">'
        f"{html.escape(source_url)}</a>\n"
        "🤖 <b>Crawler:</b> WTR Local"
    )
    if len(info_caption) > 1024:
        info_caption = (
            f"📖 <b>{html.escape(novel.title[:200])}</b>\n"
            f"📌 <b>Status:</b> {html.escape(status_text)}\n"
            f"🔍 {html.escape(display_title[:200])}"
        )

    if cover_path:
        cover_message = send_cover_photo(chat_id, cover_path, info_caption)
        if cover_message:
            posted_ids.append(cover_message.message_id)
        else:
            print(f"[COVER SEND] failed to send {cover_path}")

    if not posted_ids:
        info_message = telegram_call(
            bot.send_message,
            chat_id,
            info_caption,
            parse_mode="HTML",
            link_preview_options=LINK_PREVIEW_OFF,
        )
        if info_message:
            posted_ids.append(info_message.message_id)

    synopsis = novel.synopsis.strip() or "No synopsis available."
    synopsis_message = telegram_call(
        bot.send_message,
        chat_id,
        f"<b>Synopsis:</b> {html.escape(synopsis[:3900])}",
        parse_mode="HTML",
        link_preview_options=LINK_PREVIEW_OFF,
    )
    if synopsis_message:
        posted_ids.append(synopsis_message.message_id)

    range_match = re.search(r"c(\d+)-(\d+)\s*$", display_title, re.I)
    if range_match:
        file_name = (
            f"{epub_stem(novel.title, int(range_match.group(1)), int(range_match.group(2)))}.epub"
        )
    else:
        file_name = epub_path.name

    def send_one_document(path: Path, name: str, caption: str):
        for attempt in range(2):
            try:
                with path.open("rb") as document:
                    return bot.send_document(
                        chat_id,
                        document,
                        visible_file_name=name,
                        caption=caption[:1024],
                        timeout=300,
                    )
            except Exception as error:
                print(f"[EPUB SEND {attempt + 1}/2 ERROR] {path.name}: {error}")
                time.sleep(5)
        return None

    epub_size = epub_path.stat().st_size
    size_mb = epub_size / (1024 * 1024)
    oversize = epub_size > TELEGRAM_BOT_UPLOAD_LIMIT
    external: Optional[dict] = None
    document_messages: list = []
    part_paths: list[Path] = []
    delivered = False

    if oversize:
        est_parts = max(2, math.ceil(epub_size / (TELEGRAM_SPLIT_TARGET_BYTES * 0.9)))
        send_notice(
            chat_id,
            (
                f"📦 This EPUB is {size_mb:.1f} MB, which is larger than the 50 MB "
                "limit Telegram allows bots to send.\n\n"
                "• I'm uploading the full file to Gofile and Buzzheavier (links "
                "will follow).\n"
                f"• I'm also splitting it into about {est_parts} smaller EPUB parts "
                "and sending them here now."
            ),
            user_id=task.get("user_id"),
        )
        # Full file goes up in the background while the parts are built/sent.
        external = start_external_upload(epub_path)

        parts: list[tuple[Path, int, int]] = []
        if range_match:
            try:
                parts = build_epub_parts(
                    novel,
                    novel_dir,
                    int(range_match.group(1)),
                    int(range_match.group(2)),
                    epub_size,
                    TELEGRAM_SPLIT_TARGET_BYTES,
                )
            except Exception as error:
                print(f"[EPUB SPLIT ERROR] {error}")
                parts = []

        delivered = bool(parts)
        for idx, (part_path, part_start, part_end) in enumerate(parts, 1):
            part_paths.append(part_path)
            message = send_one_document(
                part_path,
                part_path.name,
                f"{novel.title[:200]} c{part_start}-{part_end} · part {idx}/{len(parts)}",
            )
            if message:
                document_messages.append(message)
            else:
                delivered = False
    else:
        message = send_one_document(epub_path, file_name, display_title)
        if message:
            document_messages.append(message)
            delivered = True
        else:
            # Telegram refused/failed: still get the file to the user via links.
            external = start_external_upload(epub_path)

    links_message = None
    gofile_url = buzz_url = None
    if external is not None:
        gofile_url, buzz_url = finish_external_upload(external)
        if gofile_url or buzz_url:
            if oversize and delivered:
                header = (
                    f"📦 <b>Full EPUB ({size_mb:.1f} MB, one file)</b>\n"
                    f"📖 <b>{html.escape(display_title)}</b>\n\n"
                    "Download the complete file from:"
                )
            else:
                header = (
                    "📥 <b>Telegram EPUB delivery failed — download it here:</b>\n"
                    f"📖 <b>{html.escape(display_title)}</b>\n"
                )
            link_lines = [header]
            if gofile_url:
                link_lines.append(f'• <a href="{html.escape(gofile_url, quote=True)}">Gofile</a>')
            if buzz_url:
                link_lines.append(
                    f'• <a href="{html.escape(buzz_url, quote=True)}">Buzzheavier</a>'
                )
            links_message = telegram_call(
                bot.send_message,
                chat_id,
                "\n".join(link_lines),
                parse_mode="HTML",
                link_preview_options=LINK_PREVIEW_OFF,
            )
        else:
            print(f"[EXTERNAL UPLOAD FAILED] task {task['id']}")

    for part_path in part_paths:
        try:
            part_path.unlink(missing_ok=True)
        except Exception as error:
            print(f"[EPUB PART CLEANUP ERROR] {error}")

    if not delivered:
        mark_upload_failed(task["id"])
        failure_text = (
            "⚠️ The EPUB was created and remains safely stored on the local WTR worker, "
            "but Telegram could not accept the upload right now."
        )
        if links_message:
            failure_text += "\n\nUse the download links above to get the file."
        send_notice(chat_id, failure_text, user_id=task.get("user_id"))
        if links_message:
            posted_ids.append(links_message.message_id)
        return False

    posted_ids.extend(m.message_id for m in document_messages)
    if links_message:
        posted_ids.append(links_message.message_id)

    # Tell the user which chapters are missing and why (only when download
    # continued past gaps — not a full stop).
    skip_note = format_skipped_chapters_note(skipped_chapters or [])
    if skip_note:
        skip_msg = telegram_call(
            bot.send_message,
            chat_id,
            skip_note,
            parse_mode="HTML",
            link_preview_options=LINK_PREVIEW_OFF,
        )
        if skip_msg:
            posted_ids.append(skip_msg.message_id)

    # copy_message preserves the content/caption but does not show "forwarded from".
    for group in OUTPUT_GROUPS:
        for message_id in posted_ids:
            telegram_call(
                bot.copy_message,
                chat_id=group,
                from_chat_id=chat_id,
                message_id=message_id,
            )
            time.sleep(4)

    if ADMIN_CHAT_ID:
        for message_id in posted_ids:
            telegram_call(
                bot.copy_message,
                chat_id=ADMIN_CHAT_ID,
                from_chat_id=chat_id,
                message_id=message_id,
            )
            time.sleep(2)

    # Upload succeeded and all copies are posted: the EPUB can be rebuilt from
    # cached chapters anytime, so drop it to save disk space.
    try:
        epub_path.unlink(missing_ok=True)
    except Exception as error:
        print(f"[EPUB CLEANUP ERROR] {error}")

    return True


# ---------------------------------------------------------------------------
# Download work
# ---------------------------------------------------------------------------

def sniff_image_suffix(data: bytes) -> Optional[str]:
    if not data or len(data) < 12:
        return None
    if data.startswith(b"\xff\xd8\xff"):
        return ".jpg"
    if data.startswith(b"\x89PNG"):
        return ".png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return ".webp"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return ".gif"
    if data[4:8] == b"ftyp" and any(
        tag in data[8:16] for tag in (b"avif", b"avis", b"mif1", b"heic", b"heif")
    ):
        return ".avif"
    return None


def cover_url_from_soup(soup: BeautifulSoup) -> str:
    for img in soup.select("img"):
        src = str(img.get("src") or img.get("data-src") or "").strip()
        if any(marker in src for marker in ("/api/v2/img", "cdn/series", "wtrimg", "/series/")):
            return src
    return ""


def cover_url_candidates(cover_url: str) -> list[str]:
    """
    WTR-Lab stores covers at img.wtr-lab.com/cdn/series/<file>, which 307s to
    /api/v2/img?src=series/<file>, which 307s to a signed imgproxy URL.
    In-page fetch() cannot follow that chain (no CORS), so we try the proxy
    and the original CDN URL through Chrome/HTTP instead.
    """
    ordered: list[str] = []

    def add(url: str):
        url = (url or "").strip()
        if not url:
            return
        if url.startswith("//"):
            url = "https:" + url
        elif url.startswith("/"):
            url = "https://wtr-lab.com" + url
        if url not in ordered:
            ordered.append(url)

    parsed = urllib.parse.urlparse(cover_url or "")
    query = urllib.parse.parse_qs(parsed.query)

    src_values = query.get("src") or []
    filenames: list[str] = []
    for src in src_values:
        src = urllib.parse.unquote(src)
        match = re.search(r"(?:series/)([^/?#]+)$", src)
        if match:
            filenames.append(match.group(1))
        elif re.search(r"\.(?:jpe?g|png|webp|gif|avif)$", src, re.I):
            filenames.append(src.rsplit("/", 1)[-1])

    match = re.search(
        r"(?:cdn/series/|series/)([^/?#]+\.(?:jpe?g|png|webp|gif|avif))",
        cover_url or "",
        re.I,
    )
    if match:
        filenames.append(match.group(1))

    seen_names = set()
    for filename in filenames:
        filename = filename.strip()
        if not filename or filename in seen_names:
            continue
        seen_names.add(filename)
        add(f"https://wtr-lab.com/api/v2/img?src=series/{filename}&w=800&f=jpeg")
        add(f"https://wtr-lab.com/api/v2/img?src=s3://wtrimg/series/{filename}&w=800&f=jpeg")
        add(f"https://wtr-lab.com/api/v2/img?src=series/{filename}&w=800")
        add(f"https://img.wtr-lab.com/cdn/series/{filename}")

    if cover_url and "/api/v2/img" in cover_url:
        if "f=" not in cover_url:
            add(cover_url + ("&" if "?" in cover_url else "?") + "f=jpeg")
        if "w=" not in cover_url:
            add(cover_url + ("&" if "?" in cover_url else "?") + "w=800&f=jpeg")

    add(cover_url)
    return ordered


def find_cached_cover(novel_dir: Path) -> Optional[Path]:
    for name in ("cover.jpg", "cover.jpeg", "cover.png", "cover.webp", "cover.gif"):
        path = novel_dir / name
        if path.is_file() and path.stat().st_size > 100:
            return path
    return None


def cache_cover(browser: WtrBrowser, novel: NovelInfo, novel_dir: Path):
    existing = find_cached_cover(novel_dir)
    if existing:
        print(f"[COVER CACHE] already have {existing.name} ({existing.stat().st_size} bytes)")
        return

    candidates = cover_url_candidates(novel.cover_url)
    if not candidates:
        print("[COVER CACHE] no cover URL found on the novel page")
        return

    print(f"[COVER CACHE] trying {len(candidates)} URL(s); primary={candidates[0]}")
    for image_url in candidates:
        try:
            cover = browser.fetch_bytes(image_url)
            if not cover:
                print(f"[COVER CACHE] empty response for {image_url}")
                continue
            suffix = sniff_image_suffix(cover)
            if not suffix:
                print(f"[COVER CACHE] not an image ({len(cover)} bytes) from {image_url}")
                continue
            if suffix == ".avif":
                print(f"[COVER CACHE] skipping AVIF from {image_url} (Telegram cannot send it)")
                continue
            atomic_write_bytes(novel_dir / f"cover{suffix}", cover)
            print(f"[COVER CACHE] saved cover{suffix} ({len(cover)} bytes) from {image_url}")
            return
        except DeadBrowser:
            raise
        except Exception as error:
            print(f"[COVER CACHE WARNING] {image_url}: {error}")

    print("[COVER CACHE] failed to download a cover")


def write_novel_metadata(novel: NovelInfo, novel_dir: Path):
    metadata = {
        "novel_id": novel.novel_id,
        "source_url": novel.source_url,
        "title": novel.title,
        "author": novel.author,
        "synopsis": novel.synopsis,
        "cover_url": novel.cover_url,
        "chapter_count": len(novel.chapters),
        "unlock_count": novel.unlock_count,
        "status": novel.status_label,
        "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    atomic_write_text(
        novel_dir / "metadata.json",
        json.dumps(metadata, ensure_ascii=False, indent=2),
    )

    conn = local_db()
    try:
        conn.execute(
            """
            INSERT INTO novels(novel_id, source_url, title, author, synopsis, cover_url)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(novel_id)
            DO UPDATE SET
                source_url=excluded.source_url,
                title=excluded.title,
                author=excluded.author,
                synopsis=excluded.synopsis,
                cover_url=excluded.cover_url,
                updated_at=CURRENT_TIMESTAMP;
            """,
            (
                novel.novel_id,
                novel.source_url,
                novel.title,
                novel.author,
                novel.synopsis,
                novel.cover_url,
            ),
        )
        conn.commit()
    finally:
        conn.close()


def format_progress_duration(seconds: float) -> str:
    """MM:SS, or HH:MM:SS once past an hour (same style as main.py)."""
    seconds = max(0, int(round(seconds)))
    minutes, secs = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours:02d}:{minutes:02d}:{secs:02d}"
    return f"{minutes:02d}:{secs:02d}"


def process_task(task: dict, browser: WtrBrowser):
    global session_state
    task_id = task["id"]
    chat_id = task["chat_id"]
    novel = None
    novel_dir = None
    start = 1
    end = 1
    completed = 0

    progress = send_notice(
        chat_id,
        "📥 WTR-Lab download started.\n\n📊 Progress: preparing...",
        user_id=task.get("user_id"),
        delay=DELETE_AFTER_NOTICE,
    )

    def on_status(kind: str, detail: str = ""):
        title = novel.title if novel else "WTR-Lab"
        if kind in ("turnstile", "turnstile_waiting"):
            edit_progress(
                progress,
                (
                    f"📖 {title}\n\n"
                    "🛡️ Bot hit a Cloudflare captcha — waiting for admin to clear it.\n\n"
                    f"{detail or 'Progress so far is saved. Admin will solve it in Chrome.'}\n\n"
                    "Already-downloaded chapters stay cached."
                ),
            )
        elif kind == "turnstile_failed":
            edit_progress(
                progress,
                (
                    f"📖 {title}\n\n"
                    "🛡️ Cloudflare captcha needs a visible Chrome window (HEADLESS=0).\n\n"
                    "Already-downloaded chapters stay cached. After this task ends, "
                    "retry the same link to receive an EPUB of what was saved."
                ),
            )
        elif kind == "turnstile_cleared":
            edit_progress(
                progress,
                (
                    f"📖 {title}\n\n"
                    "✅ Captcha cleared — resuming download…\n\n"
                    f"{detail or ''}"
                ),
            )
        elif kind == "login_required":
            edit_progress(
                progress,
                (
                    f"📖 {title}\n\n"
                    "🔐 WTR-Lab account on the server is logged out — paused.\n\n"
                    "Send /login to log it back in. Progress is saved and the "
                    "download resumes automatically."
                ),
            )
        elif kind == "login_ok":
            edit_progress(
                progress,
                f"📖 {title}\n\n✅ Logged in again — resuming download…",
            )
        elif kind == "origin_timeout":
            edit_progress(
                progress,
                (
                    f"📖 {title}\n\n"
                    "⏳ Origin timeout / temporary upstream error — not a captcha.\n\n"
                    f"{detail or 'Waiting, then retrying this chapter…'}\n\n"
                    "Already-downloaded chapters stay cached."
                ),
            )

    browser.on_status = on_status

    def send_partial_if_possible(reason: str) -> bool:
        if not novel or not novel_dir or completed <= 0:
            return False
        last = start + completed - 1
        try:
            edit_progress(
                progress,
                f"📖 {novel.title}\n\n📚 Packing EPUB of unlocked chapters {start}-{last}...",
            )
            try:
                repair_bad_chapters(
                    client, novel, novel_dir, start, last, progress
                )
            except Exception as repair_error:
                print(f"[REPAIR PARTIAL ERROR] {repair_error}")
            epub_path = build_epub(novel, novel_dir, start, last)
            display_title = f"{novel.title} c{start}-{last}"
            save_task_cache(task_id, novel.novel_id, epub_path, display_title)
            send_notice(
                chat_id,
                reason,
                user_id=task.get("user_id"),
            )
            if progress:
                cleanup_bot_message(chat_id, progress.message_id)
            delivered = send_completed_files(
                task, novel, novel_dir, epub_path, display_title
            )
            if delivered:
                uid = task.get("user_id")
                if uid is not None:
                    send_notice(chat_id, daily_usage_summary(int(uid)), user_id=uid)
            return delivered
        except Exception as pack_error:
            print(f"[PARTIAL EPUB ERROR] {pack_error}")
            return False

    try:
        # Shared Chrome profile must be logged in for chapter API access.
        if not browser.is_logged_in():
            # Wait here (notifies admin + user once); no requeue / re-check loop.
            browser.wait_for_login(
                "https://wtr-lab.com/en/profile",
                "Logged out when the task started",
                task_id=task_id,
                chat_id=chat_id,
                user_id=task.get("user_id"),
            )
        else:
            session_state = "ok"

        client = WtrLabClient(browser)
        client.task_id = task_id
        client.chat_id = chat_id
        client.user_id = int(task["user_id"]) if task.get("user_id") is not None else None
        novel = client.load_novel(task["url"])

        novel_dir = LIBRARY_DIR / novel.novel_id
        (novel_dir / "chapters").mkdir(parents=True, exist_ok=True)
        (novel_dir / "images").mkdir(parents=True, exist_ok=True)
        (novel_dir / "artifacts").mkdir(parents=True, exist_ok=True)

        write_novel_metadata(novel, novel_dir)
        cache_cover(browser, novel, novel_dir)
        update_task_metadata(task_id, novel.title, len(novel.chapters))

        total_listed = len(novel.chapters)
        unlocked_cap = novel.unlock_count or total_listed
        if unlocked_cap < total_listed:
            print(
                f"[UNLOCK] AI-Unlock counter {unlocked_cap}/{total_listed} "
                f"status={novel.status_label} "
                "(hint only — will probe past gaps; users may unlock non-sequentially)"
            )

        start, end = parse_chapter_range(task.get("chapter_range"), total_listed)
        # Do NOT hard-cap the range to unlock_count. That counter is often
        # sequential ("first N unlocked") while readers unlock scattered
        # chapters (e.g. 31–32 locked, 33+ open). Probe the full requested range.

        requested = [
            chapter
            for chapter in novel.chapters
            if start <= chapter.order <= end
        ]

        if not requested:
            raise WtrError(
                f"No chapters in the requested range "
                f"({start}–{end} of {total_listed})."
            )

        user_id = int(task["user_id"])
        unlimited_pulls = is_admin(user_id)
        daily_cap = get_chapter_cap(user_id)

        # Only fetch chapters that are missing/empty (gap-only).
        to_fetch = [
            ch
            for ch in requested
            if not cache_has_chapter(novel.novel_id, ch.order)
        ]
        cached_count = len(requested) - len(to_fetch)
        completed = cached_count
        last_progress = 0.0
        last_hb = 0.0
        last_chapter_request = 0.0
        stopped_by_pull_cap = False
        network_fetches_this_run = 0

        unlock_line = (
            f"🔓 AI-Unlock: {unlocked_cap}/{total_listed} · {novel.status_label}"
        )
        # Chapters soft-skipped while still finishing the run (AI-lock / not crawled).
        skipped_chapters: list[tuple[int, str]] = []

        if to_fetch:
            sample = [ch.order for ch in to_fetch[:12]]
            more = "…" if len(to_fetch) > 12 else ""
            print(
                f"[RESUME] novel={novel.novel_id} "
                f"cached={cached_count}/{len(requested)} "
                f"missing={len(to_fetch)} {sample}{more} "
                f"pulls_left={pulls_remaining(user_id, unlimited_pulls)}"
            )
            edit_progress(
                progress,
                (
                    f"📖 {novel.title}\n\n"
                    f"📥 Resuming local WTR-Lab worker...\n\n"
                    f"📌 Already on server: {cached_count}/{len(requested)}\n"
                    f"▶️ Need from site: {len(to_fetch)} chapter(s)\n"
                    f"🔍 Range: from {start} to {end}\n"
                    f"{unlock_line}\n"
                    f"{fresh_downloads_line(user_id, unlimited_pulls, daily_cap)}"
                ),
            )
        else:
            print(
                f"[RESUME] novel={novel.novel_id} "
                f"cached={cached_count}/{len(requested)} next=None"
            )
            edit_progress(
                progress,
                (
                    f"📖 {novel.title}\n\n"
                    f"📥 All needed chapters are already on the server — "
                    f"preparing EPUB...\n\n"
                    f"🔍 Range: from {start} to {end}\n"
                    f"{unlock_line}\n"
                    f"{fresh_downloads_line(user_id, unlimited_pulls, daily_cap)}"
                ),
            )

        crawl_started_at = time.monotonic()

        for chapter in to_fetch:
            if is_cancelled(task_id):
                raise TaskCancelled()

            # CHAPTER_CAP = daily first-time network fetches per user,
            # UNIVERSAL across all novels (does not reset per novel).
            if pulls_remaining(user_id, unlimited_pulls) <= 0:
                stopped_by_pull_cap = True
                print(
                    f"[PULL CAP] user={user_id} novel={novel.novel_id} "
                    f"cap={daily_cap} — stopping network fetches at ch {chapter.order}"
                )
                break

            xhtml_path = novel_dir / "chapters" / f"{chapter.order:05}.xhtml"
            if xhtml_path.is_file() and chapter_cache_is_bad(xhtml_path):
                purge_chapter_cache(novel.novel_id, chapter.order, xhtml_path)

            elapsed = time.monotonic() - last_chapter_request
            target_wait = next_chapter_throttle()
            remaining = target_wait - elapsed

            if last_chapter_request and remaining > 0:
                print(
                    f"[CHAPTER PACER] Waiting {remaining:.1f}s "
                    f"(target {target_wait:.1f}s) before chapter {chapter.order}"
                )
                time.sleep(remaining)

            last_chapter_request = time.monotonic()
            # Outer retry for transient failures (works moments later in VNC).
            # download_chapter already retries internally; this adds a cool-down.
            try:
                title, xhtml = None, None
                chapter_attempts = 2
                for ch_try in range(1, chapter_attempts + 1):
                    try:
                        title, xhtml = client.download_chapter(
                            novel,
                            chapter,
                            novel_dir,
                            task_id=task_id,
                            chat_id=chat_id,
                            user_id=user_id,
                        )
                        break
                    except (
                        ChapterLocked,
                        ManualChallengeRequired,
                        LoginRequired,
                        DeadBrowser,
                    ):
                        raise
                    except WtrError as ch_err:
                        err_l = str(ch_err).lower()
                        if (
                            "waiting for crawling" in err_l
                            or "check back later" in err_l
                            or "ai-lock" in err_l
                            or "chapter locked" in err_l
                        ):
                            raise
                        if ch_try < chapter_attempts:
                            print(
                                f"[CHAPTER RETRY] ch {chapter.order} failed "
                                f"({ch_err}); cool-down 8s then retry "
                                f"{ch_try}/{chapter_attempts}"
                            )
                            time.sleep(8.0)
                            last_chapter_request = time.monotonic()
                            continue
                        raise
                if title is None or xhtml is None:
                    raise WtrError(
                        f"Chapter {chapter.order}: empty result after retries"
                    )
            except ChapterLocked:
                # Sparse unlocks: skip this chapter and keep probing. Only stop
                # after several consecutive locks with no further success.
                consecutive_locked = getattr(client, "_consecutive_locked", 0) + 1
                client._consecutive_locked = consecutive_locked
                max_probe = 8
                skip_reason = "AI-locked on WTR-Lab (not unlocked for this account)"
                skipped_chapters.append((chapter.order, skip_reason))
                print(
                    f"[UNLOCK] Chapter {chapter.order} AI-locked — "
                    f"skipping, probe {consecutive_locked}/{max_probe}"
                )
                if consecutive_locked >= max_probe:
                    last_ok = max(
                        (
                            ch.order
                            for ch in requested
                            if ch.order < chapter.order
                            and cache_has_chapter(novel.novel_id, ch.order)
                        ),
                        default=start - 1,
                    )
                    try:
                        send_error_screenshot(
                            browser,
                            chat_id,
                            (
                                f"⚠️ Task #{task_id} · chapter {chapter.order}\n"
                                f"{skip_reason}\n\n"
                                "Open this chapter on wtr-lab.com to confirm."
                            ),
                            user_id=user_id,
                            task_id=task_id,
                            to_admin=False,
                            open_url=client.chapter_open_url(novel, chapter),
                        )
                    except Exception as shot_error:
                        print(f"[SCREENSHOT] {shot_error}")
                    if completed > 0 and last_ok >= start:
                        # Streak stop: only explain from the first failure in the streak
                        skip_note = format_skipped_chapters_note(
                            skipped_chapters, streak_stopped=True
                        )
                        send_partial_if_possible(
                            (
                                f"🔓 Download stopped — several chapters in a row "
                                f"are still AI-locked on WTR-Lab.\n"
                                f"Sending what was downloaded "
                                f"({unlocked_cap}/{total_listed} on the unlock counter).\n\n"
                                + (f"{skip_note}\n\n" if skip_note else "")
                                + "If later chapters are unlocked on the site, resend "
                                "the link or /continue after unlocking more."
                            )
                        )
                        mark_failed(
                            task_id,
                            f"AI-locked streak at chapter {chapter.order}",
                        )
                        return
                    raise
                continue
            except ManualChallengeRequired:
                raise
            except LoginRequired:
                raise
            except DeadBrowser:
                raise
            except WtrError as chapter_error:
                # Soft-skip chapter-level site errors and probe the next ones
                # (same idea as AI-lock / crawl-wait). Hard-failing here left
                # later unlocked chapters unfetched and screenshotted the wrong tab.
                consecutive_locked = getattr(client, "_consecutive_locked", 0) + 1
                client._consecutive_locked = consecutive_locked
                max_probe = 8
                site_reason = str(chapter_error).strip()
                if ":" in site_reason:
                    site_reason = site_reason.split(":", 1)[-1].strip()
                if not site_reason:
                    site_reason = "WTR-Lab chapter error"
                skipped_chapters.append((chapter.order, site_reason))
                err_l = site_reason.lower()
                kind = (
                    "CRAWL WAIT"
                    if ("waiting for crawling" in err_l or "check back later" in err_l)
                    else "CHAPTER SKIP"
                )
                print(
                    f"[{kind}] Chapter {chapter.order}: {chapter_error} — "
                    f"skipping, probe {consecutive_locked}/{max_probe}"
                )
                if consecutive_locked >= max_probe:
                    last_ok = max(
                        (
                            ch.order
                            for ch in requested
                            if ch.order < chapter.order
                            and cache_has_chapter(novel.novel_id, ch.order)
                        ),
                        default=start - 1,
                    )
                    fail_url = client.chapter_open_url(novel, chapter)
                    shot_caption = (
                        f"⚠️ Task #{task_id} · chapter {chapter.order}\n"
                        f"{site_reason}\n\n"
                        "Please open this chapter on wtr-lab.com and confirm it "
                        "is accessible. Resend the link or /continue after."
                    )
                    try:
                        send_error_screenshot(
                            browser,
                            chat_id,
                            shot_caption,
                            user_id=user_id,
                            task_id=task_id,
                            to_admin=True,
                            open_url=fail_url,
                        )
                    except Exception as shot_error:
                        print(f"[SCREENSHOT] {shot_error}")
                    if completed > 0 and last_ok >= start:
                        skip_note = format_skipped_chapters_note(
                            skipped_chapters, streak_stopped=True
                        )
                        send_partial_if_possible(
                            (
                                "⚠️ Download stopped — several chapters in a row "
                                "failed on WTR-Lab.\n"
                                "Sending what was downloaded.\n\n"
                                + (f"{skip_note}\n\n" if skip_note else "")
                                + "Check the site, then /continue or resend the link."
                            )
                        )
                        mark_failed(
                            task_id,
                            f"chapter error streak at chapter {chapter.order}: {site_reason[:120]}",
                        )
                        return
                    raise
                continue

            client._consecutive_locked = 0
            atomic_write_text(xhtml_path, xhtml)
            cache_chapter(
                novel.novel_id,
                chapter.order,
                chapter.chapter_id,
                title,
                xhtml_path,
            )
            # Count only first-time network success for this user/novel/chapter.
            if register_chapter_pull(user_id, novel.novel_id, chapter.order):
                network_fetches_this_run += 1
            completed += 1

            now = time.monotonic()
            if now - last_hb >= HEARTBEAT_SECONDS:
                last_hb = now
                try:
                    heartbeat_once(task.get("id"))
                    touch_progress(task.get("id") or task_id)
                except Exception as _hb_err:
                    print(f"[HEARTBEAT] {_hb_err}")
            if (
                now - last_progress >= PROGRESS_UPDATE_SECONDS
                or completed == len(requested)
            ):
                last_progress = now
                try:
                    touch_progress(task.get("id") or task_id)
                except Exception:
                    pass
                percent = completed / len(requested) * 100
                # Timing is based only on chapters fetched in THIS run, so
                # cache hits (instant) don't make the estimate wildly optimistic.
                crawl_elapsed = max(0.0, now - crawl_started_at)
                handled = (completed - cached_count) + len(skipped_chapters)
                sec_per_chapter = crawl_elapsed / handled if handled else 0.0
                left_to_fetch = max(0, len(to_fetch) - handled)
                crawl_remaining = sec_per_chapter * left_to_fetch
                edit_progress(
                    progress,
                    (
                        f"📖 {novel.title}\n\n"
                        f"📥 Downloading with local WTR-Lab worker...\n\n"
                        f"📊 Progress: {percent:.1f}%\n"
                        f"📖 Currently At: Chapter {chapter.order}\n"
                        f"🔍 Range: from {start} to {end}\n"
                        f"{unlock_line}\n"
                        f"{fresh_downloads_line(user_id, unlimited_pulls, daily_cap)}\n\n"
                        f"📌 Status: {completed}/{len(requested)} chapters available\n\n"
                        f"⏱ Elapsed: {format_progress_duration(crawl_elapsed)}\n"
                        f"⌛ Remaining: {format_progress_duration(crawl_remaining)}\n"
                        f"⚡ Speed: {sec_per_chapter:.2f} s/chapter"
                    ),
                )

        # EPUB range: requested span (All = 1..unlocked includes free cache).
        pack_start, pack_end = start, end
        if stopped_by_pull_cap:
            # Pack what we actually have in range (cache + this run).
            have_nos = [
                ch.order
                for ch in requested
                if cache_has_chapter(novel.novel_id, ch.order)
            ]
            if not have_nos:
                # Cap exhausted and nothing on disk — do not "repair" the full
                # missing range (that used to bypass CHAPTER_CAP).
                if progress:
                    cleanup_bot_message(chat_id, progress.message_id)
                send_notice(
                    chat_id,
                    (
                        f"⚠️ Daily chapter limit reached "
                        f"({daily_cap if daily_cap > 0 else 'cap'} from the site / 24h).\n"
                        "No chapters for this novel are on the server yet, so "
                        "nothing could be packed.\n\n"
                        "📌 Resend the same link or /continue after the limit resets. "
                        "Send /cap to check your limits. "
                        "Contact admin if you need help or another task."
                    ),
                    user_id=user_id,
                )
                mark_failed(
                    task_id,
                    f"Daily chapter limit ({daily_cap}); no cached chapters to pack",
                )
                send_notice(chat_id, daily_usage_summary(user_id), user_id=user_id)
                return
            pack_end = max(have_nos)

        edit_progress(
            progress,
            f"📖 {novel.title}\n\n📚 Checking cache / creating EPUB...",
        )

        # Repair only previously cached empty/corrupt files (not pure misses).
        # Free retries for bot/cache faults; does not expand into never-fetched
        # chapters and does not bypass CHAPTER_CAP.
        repair_bad_chapters(client, novel, novel_dir, pack_start, pack_end, progress)

        edit_progress(
            progress,
            f"📖 {novel.title}\n\n📚 Creating EPUB from cached XHTML...",
        )
        epub_path = build_epub(novel, novel_dir, pack_start, pack_end)
        display_title = f"{novel.title} c{pack_start}-{pack_end}"
        save_task_cache(task_id, novel.novel_id, epub_path, display_title)

        if progress:
            cleanup_bot_message(chat_id, progress.message_id)

        if stopped_by_pull_cap:
            # Partial relative to request — failed so same-URL continue works later.
            send_notice(
                chat_id,
                (
                    f"⚠️ Daily chapter limit reached "
                    f"({daily_cap if daily_cap > 0 else 'cap'} from the site / 24h).\n"
                    f"Chapters already on the server are included free.\n"
                    f"Sending what is available: c{pack_start}-{pack_end}.\n\n"
                    "📌 A different novel will hit the same limit until it resets. "
                    "Resend the same link or /continue after the limit resets. "
                    "Send /cap to check your limits. "
                    "Contact admin if you need help or another task."
                ),
                user_id=user_id,
            )
            if send_completed_files(
                task,
                novel,
                novel_dir,
                epub_path,
                display_title,
                skipped_chapters=skipped_chapters or None,
            ):
                mark_failed(
                    task_id,
                    f"Daily chapter limit ({daily_cap}); packed c{pack_start}-{pack_end}",
                )
                send_notice(chat_id, daily_usage_summary(user_id), user_id=user_id)
            else:
                mark_failed(task_id, f"Daily chapter limit ({daily_cap}); upload failed")
            return

        if send_completed_files(
            task,
            novel,
            novel_dir,
            epub_path,
            display_title,
            skipped_chapters=skipped_chapters or None,
        ):
            mark_done(task_id)
            send_notice(chat_id, daily_usage_summary(user_id), user_id=user_id)

    except LoginRequired:
        set_login_status("logged_out", f"task #{task_id} needed a login")
        notify_admin(
            f"🔐 <b>Server <code>{html.escape(str(SERVER_ID))}</code> needs a login</b>\n"
            f"Task <code>{task_id}</code> was re-queued because the account on "
            f"<code>{html.escape(str(SERVER_ID))}</code> is logged out.\n"
            f"Send <code>/login {html.escape(str(SERVER_ID))}</code>."
        )
        send_notice(
            chat_id,
            (
                f"🔐 Login required on server {SERVER_ID}. Send /login, reply "
                f"with {SERVER_ID}, then your email address to start "
                "magic-link login."
            ),
            user_id=task.get("user_id"),
        )
        session_start(
            int(task["user_id"]),
            step="login_email",
            from_task=task_id,
        )
        requeue_task(task_id)

    except TaskCancelled:
        print(f"[CANCELLED] Task {task_id}")
        if progress:
            cleanup_bot_message(chat_id, progress.message_id)

    except DeadBrowser:
        print(f"[DEAD BROWSER] Task {task_id}")
        send_partial_if_possible(
            "⚠️ Chrome closed or the session died.\n"
            "Sending any chapters already cached.\n\n"
            "The worker is reopening Chrome and will resume this download "
            "automatically from cache."
        )
        mark_failed(task_id, "Chrome session died")
        raise

    except Exception as error:
        if is_dead_session(error):
            # Same as DeadBrowser: Chrome died outside a wrapped call. Resume
            # automatically instead of leaving the task failed.
            print(f"[DEAD BROWSER] Task {task_id}: {type(error).__name__}: {error}")
            send_partial_if_possible(
                "⚠️ Chrome closed or the session died.\n"
                "Sending any chapters already cached.\n\n"
                "The worker is reopening Chrome and will resume this download "
                "automatically from cache."
            )
            mark_failed(task_id, "Chrome session died")
            raise DeadBrowser(str(error)) from error

        log.exception("[WTR TASK ERROR] task=%s: %s: %s", task_id, type(error).__name__, error)
        reason = user_facing_error(error)
        routine = is_routine_failure(reason)

        # Prefer opening the chapter page the error refers to (not whatever tab
        # was left open, e.g. novel info).
        error_open_url = None
        try:
            ch_match = re.search(r"chapter\s+(\d+)", str(error), re.I)
            if ch_match and novel is not None:
                error_open_url = (
                    f"https://wtr-lab.com/{(novel.language or 'en').strip('/')}/novel/"
                    f"{novel.novel_id}/{(novel.slug or 'novel')}/chapter-{ch_match.group(1)}"
                )
            elif novel is not None and getattr(novel, "source_url", None):
                error_open_url = novel.source_url
        except Exception:
            error_open_url = None

        shot_caption = (
            f"⚠️ Task #{task_id}\n{reason}\n\n"
            "📌 Resend the same link or /continue to resume from cache."
        )
        shot_sent = False
        try:
            shot_sent = send_error_screenshot(
                browser,
                chat_id,
                shot_caption,
                user_id=task.get("user_id"),
                task_id=task_id,
                to_admin=not routine,
                open_url=error_open_url,
            )
        except Exception as shot_error:
            print(f"[SCREENSHOT] {shot_error}")

        if not routine:
            try:
                notify_admin(
                    "⚠️ <b>Task error</b>\n"
                    f"🆔 Task <code>{task_id}</code>\n"
                    f"👤 User <code>{task.get('user_id')}</code>\n"
                    f"📌 {html.escape(reason[:500])}"
                    + ("\n🖼 Screenshot sent." if shot_sent else "")
                )
            except Exception:
                pass

        partial_sent = send_partial_if_possible(
            (
                f"⚠️ Download stopped: {reason}\n\n"
                "Sending the chapters already cached.\n\n"
                "📌 Resend the same link (or /continue) to resume from cache. "
                "A different novel is blocked while this failed task holds "
                "your daily slot."
            )
        )

        if progress and not partial_sent:
            edit_progress(progress, f"⚠️ WTR-Lab download failed.\n\n{reason}")

        if not partial_sent and not shot_sent:
            send_notice(
                chat_id,
                (
                    f"⚠️ WTR-Lab could not complete this task.\n\n"
                    f"{reason}\n\n"
                    "Cached chapters stay on disk.\n"
                    "📌 Resend the same novel link (or /continue) to resume."
                ),
                user_id=task.get("user_id"),
            )
        mark_failed(task_id, reason)
    finally:
        browser.on_status = None



# ---------------------------------------------------------------------------
# Telegram bot commands (task intake)
# ---------------------------------------------------------------------------

def deny_if_needed(message) -> bool:
    uid = message.from_user.id
    ban = None if is_admin(uid) else get_ban(uid)
    if ban:
        until = ban["until"]
        reply_notice(
            message,
            "🚫 You are banned from using this bot"
            + (f" until {until} UTC." if until else " permanently.")
            + (f"\nReason: {ban['reason']}" if ban["reason"] else "")
            + "\nContact the admin if you think this is a mistake.",
        )
        return True
    if user_allowed(uid):
        return False
    reply_notice(
        message,
        "🔒 This bot is currently locked by its owner.\n"
        "You don’t have access to use it.",
    )
    return True


@bot.message_handler(commands=["help"])
def cmd_start(message):
    if deny_if_needed(message):
        return
    cap = get_chapter_cap(message.from_user.id)
    limit_line = (
        f"• Daily task limit: {DAILY_TASK_LIMIT}\n"
        if DAILY_TASK_LIMIT > 0
        else "• Daily task limit: none\n"
    )
    cap_line = (
        f"• Up to {cap} chapters/day from the site "
        f"(already on the server are free)\n"
        if cap > 0
        else "• No daily chapter limit from the site\n"
    )
    admin_line = ""
    if is_admin(message.from_user.id):
        admin_line = (
            "\n<b>Admin</b>\n"
            "/logs — full logs (with names)\n"
            "/trial — all tasks\n"
            "/continue &lt;id&gt; — re-queue failed/cancelled task\n"
            "/pause &lt;id&gt; / /stop &lt;id&gt; — pause (auto-pending ~2 min)\n"
            "/cancel &lt;id&gt; — cancel any task\n"
            "/start &lt;id&gt; — next in line (one-shot)\n"
            "/set KEY value — edit .env (with confirm)\n"
            "/restart — restart bot via Start for Linux.sh\n"
        )
    try:
        msg = bot.reply_to(
            message,
            "📚 <b>Personal WTR-Lab downloader</b>\n\n"
            "Runs on this server. Tasks are stored in a local SQLite database.\n\n"
            "<b>Commands</b>\n"
            "/download — queue a wtr-lab.com novel\n"
            "/login — magic-link login (if the shared profile is logged out)\n"
            "/queue — global wait list (no names)\n"
            "/tasks — recent tasks (no names)\n"
            "/mytasks — your tasks only\n"
            "/continue — re-queue your latest failed task\n"
            "/cancel — cancel your pending/running tasks\n"
            "/status — worker status\n"
            "/cap — show daily chapter limit\n"
            "/premium — subscription info & pay (Stars/PayPal)\n"
            "/paysupport — payment help · /terms — Premium terms\n"
            "/stopautocontinue — stop auto-continue for a novel\n"
            f"{admin_line}\n"
            f"{limit_line}"
            f"{cap_line}"
            "Chat steps (/download, /login, /set) expire after 10 minutes.\n"
            "Each finished task uses your daily task slot. "
            "Chapters already downloaded on the server are reused for free. "
            "Failed/partial: resend the <b>same</b> link or /continue. "
            "Contact admin if you need help or another task.\n\n"
            f"⭐ Premium: 1000 chapters/day, {PREMIUM_TASKS_PER_DAY} tasks/day, priority, Auto-Continue — /premium\n\n"
            "Only links from <code>wtr-lab.com</code> are accepted.",
            parse_mode="HTML",
            link_preview_options=LINK_PREVIEW_OFF,
        )
        track_and_autodelete(msg, message.from_user.id, DELETE_AFTER_LIST)
    except Exception as error:
        print(f"[TELEGRAM ERROR] {error}")


def _is_remote_server(sid: str) -> bool:
    return bool(USE_SHARED_TASKS and str(sid or "").lower() != str(SERVER_ID).lower())


def _login_server_menu(fleet: list) -> str:
    lines = [
        "🔐 <b>Please reply with the server you want to log in at</b> "
        "(e.g. <code>s5</code>):\n"
    ]
    for info in fleet:
        sid = html.escape(str(info["server_id"]))
        extra = "" if info["online"] else f" · offline (seen {fmt_ago(info['last_seen'])})"
        lines.append(f"<code>{sid}</code> — {html.escape(login_text(info))}{extra}")
    lines.append(f"\n(Expires in {SESSION_SECONDS // 60} min.)")
    return "\n".join(lines)


def _login_server_buttons(fleet: list):
    markup = telebot.types.InlineKeyboardMarkup()
    row = []
    for info in fleet:
        sid = str(info["server_id"])
        icon = {"logged_in": "✅", "logged_out": "❌", "ip_blocked": "🚫"}.get(
            info["login"], "❓"
        )
        row.append(
            telebot.types.InlineKeyboardButton(
                f"{icon} {sid}", callback_data=f"login_srv:{sid}"
            )
        )
        if len(row) == 3:
            markup.row(*row)
            row = []
    if row:
        markup.row(*row)
    return markup


def _begin_login(uid: int, sid: str, fleet: list) -> tuple[bool, str]:
    """Validate the chosen server and open the 'send your email' step."""
    info = next(
        (i for i in fleet if str(i["server_id"]).lower() == str(sid).lower()), None
    )
    if info is None:
        known = ", ".join(f"<code>{html.escape(str(i['server_id']))}</code>" for i in fleet)
        return False, f"⚠️ Unknown server <code>{html.escape(str(sid))}</code>. Known: {known}"
    real = str(info["server_id"])
    if _is_remote_server(real) and not info["online"]:
        return False, (
            f"⚠️ <code>{html.escape(real)}</code> is offline (last seen "
            f"{fmt_ago(info['last_seen'])}), so it can't run a login. "
            "Start it first."
        )
    session_start(uid, step="login_email", login_server=real)
    note = ""
    if info["login"] == "logged_in":
        note = (
            f"\nℹ️ {html.escape(real)} reports <b>logged in</b> "
            f"(checked {fmt_ago(info['login_checked'])}). I'll check it live: if "
            "it is already logged in, no magic link is needed."
        )
    elif info["login"] == "ip_blocked":
        note = "\n🚫 This server's IP looks blocked by Cloudflare; a login may not help."
    return True, (
        f"📧 Login for server <code>{html.escape(real)}</code>.{note}\n"
        "Send the email for the WTR-Lab magic link.\n"
        f"(Expires in {SESSION_SECONDS // 60} min.)"
    )


@bot.message_handler(commands=["login"])
def cmd_login(message):
    if deny_if_needed(message):
        return
    uid = message.from_user.id
    parts = (message.text or "").split()
    target = parts[1].strip() if len(parts) > 1 else None

    if not USE_SHARED_TASKS:  # single server: nothing to choose
        session_start(uid, step="login_email", login_server=SERVER_ID)
        reply_notice(
            message,
            f"📧 Login for <code>{html.escape(SERVER_ID)}</code>.\n"
            "Send the email for the WTR-Lab magic link.\n"
            f"(Expires in {SESSION_SECONDS // 60} min.)",
            parse_mode="HTML",
        )
        return

    try:
        fleet = fleet_overview()
    except Exception as error:
        log.exception("/login: fleet lookup failed")
        reply_notice(message, f"⚠️ Could not read server status: {error}")
        return
    if not fleet:
        session_start(uid, step="login_email", login_server=SERVER_ID)
        reply_notice(
            message,
            "No servers reported yet. Send the email for the magic link on "
            f"<code>{html.escape(SERVER_ID)}</code>.",
            parse_mode="HTML",
        )
        return

    if target:  # /login s5
        ok, text = _begin_login(uid, target, fleet)
        reply_notice(message, text, parse_mode="HTML")
        return

    session_start(uid, step="login_pick")
    reply_notice(
        message,
        _login_server_menu(fleet),
        parse_mode="HTML",
        reply_markup=_login_server_buttons(fleet),
    )

@bot.message_handler(commands=["servers"])
def cmd_servers(message):
    """Public: worker health + login state without user names/usernames."""
    if deny_if_needed(message):
        return
    if not USE_SHARED_TASKS:
        reply_notice(
            message,
            f"🖥 This host: <code>{html.escape(SERVER_ID)}</code> (local mode).",
            parse_mode="HTML",
        )
        return
    try:
        lines = fleet_lines()
    except Exception as e:
        log.exception("/servers failed")
        reply_notice(message, f"⚠️ {e}")
        return
    if not lines:
        reply_notice(message, "No heartbeats yet.")
        return
    reply_notice(
        message,
        "🖥 <b>Workers</b> (no user names)\n" + "\n".join(lines),
        parse_mode="HTML",
    )

@bot.message_handler(commands=["status"])
def cmd_status(message):
    if deny_if_needed(message):
        return
    cap = get_chapter_cap()
    fleet_block = ""
    if USE_SHARED_TASKS:
        try:
            _fl = fleet_lines()
            if _fl:
                fleet_block = "\n\n🖥 <b>Workers</b>\n" + "\n".join(_fl)
        except Exception as error:
            log.exception("/status: fleet lookup failed")
            fleet_block = f"\n\n⚠️ Worker status unavailable: {html.escape(str(error))}"
    try:
        msg = bot.reply_to(
            message,
            (
                "🖥 <b>WTR local worker</b>\n"
                f"SQLite: <code>{html.escape(str(SQLITE_PATH))}</code>\n"
                f"SERVER_ID: <code>{html.escape(SERVER_ID)}</code>\n"
                f"Chapters/day from site: "
                f"{'unlimited' if cap <= 0 else cap}\n"
                f"Daily task limit: "
                f"{'none' if DAILY_TASK_LIMIT <= 0 else DAILY_TASK_LIMIT}\n"
                f"Throttle: {CHAPTER_THROTTLE_MIN:.0f}–{CHAPTER_THROTTLE_MAX:.0f}s\n"
                f"Heartbeat: every {HEARTBEAT_THREAD_SECONDS}s (background)\n"
                "⭐ Premium: /premium"
                f"{fleet_block}"
            ),
            parse_mode="HTML",
            link_preview_options=LINK_PREVIEW_OFF,
        )
        track_and_autodelete(msg, message.from_user.id, DELETE_AFTER_LIST)
    except Exception as error:
        print(f"[TELEGRAM ERROR] {error}")


def _send_long_text(chat_id, text: str, user_id: int | None = None):
    """Split long listings; each chunk auto-deletes after 1 hour."""
    text = text or ""
    if not text:
        return
    max_len = 4000
    while text:
        chunk = text[:max_len]
        text = text[max_len:]
        send_list_temp(chat_id, chunk, user_id=user_id)


def _fetch_task_rows(
    *,
    user_id: Optional[int] = None,
    statuses: Optional[list] = None,
    newest_first: bool = False,
    queue_order: bool = False,
    limit: Optional[int] = None,
) -> list:
    """Task rows as dicts. Reads shared Postgres when USE_SHARED_TASKS, else local SQLite."""
    if USE_SHARED_TASKS:
        where, params = [], []
        if user_id is not None:
            where.append("user_id = %s")
            params.append(int(user_id))
        if statuses:
            where.append("status = ANY(%s)")
            params.append(list(statuses))
        sql = (
            "SELECT id, user_id, username, first_name, last_name, status, url, "
            "chapter_range, error, novel_title, created_at, completed_at, "
            "server_id, COALESCE(run_next, 0) AS run_next "
            "FROM wtrlab_tasks"
        )
        if where:
            sql += " WHERE " + " AND ".join(where)
        if queue_order:
            sql += " ORDER BY COALESCE(run_next, 0) DESC, priority DESC, id ASC"
        else:
            sql += " ORDER BY id " + ("DESC" if newest_first else "ASC")
        if limit:
            sql += f" LIMIT {int(limit)}"
        conn = shared_db()
        try:
            with conn.cursor() as cur:
                cur.execute(sql, params)
                cols = [d[0] for d in cur.description]
                return [dict(zip(cols, r)) for r in cur.fetchall()]
        finally:
            conn.close()

    where, params = [], []
    if user_id is not None:
        where.append("user_id = ?")
        params.append(int(user_id))
    if statuses:
        where.append("status IN (%s)" % ",".join("?" * len(statuses)))
        params.extend(statuses)
    sql = (
        "SELECT id, user_id, username, first_name, last_name, status, url, "
        "chapter_range, error, novel_title, created_at, completed_at, "
        "COALESCE(run_next, 0) AS run_next FROM tasks"
    )
    if where:
        sql += " WHERE " + " AND ".join(where)
    if queue_order:
        sql += " ORDER BY COALESCE(run_next, 0) DESC, id ASC"
    else:
        sql += " ORDER BY id " + ("DESC" if newest_first else "ASC")
    if limit:
        sql += f" LIMIT {int(limit)}"
    conn = local_db()
    try:
        return [dict(r) for r in conn.execute(sql, params).fetchall()]
    finally:
        conn.close()


def _task_server_label(t) -> str:
    return str(t.get("server_id") or "local")


def _public_task_block(t, *, include_error: bool = True) -> str:
    """Task listing without username/name (user id kept)."""
    uid = int(t["user_id"]) if t["user_id"] is not None else 0
    used = count_chapter_pulls(uid, hours=24) if uid else 0
    cap = get_chapter_cap(uid) if uid else get_chapter_cap()
    if cap > 0:
        pull_line = f"⬇️ Fresh downloads 24h: {used}/{cap}\n"
    else:
        pull_line = f"⬇️ Fresh downloads 24h: {used} (unlimited)\n"
    title = (t["novel_title"] or "").strip() or "—"
    err = ""
    if include_error:
        err_raw = (t["error"] or "").strip() if t["error"] is not None else ""
        err = f"❌ Error: {err_raw or 'N/A'}\n"
    return (
        f"🆔 Task ID: {t['id']}\n"
        f"👤 User ID: {t['user_id']}\n"
        f"🖥 Server: {_task_server_label(t)}\n"
        f"⚙️ Crawler: wtr_local\n"
        f"📌 Status: {t['status']}\n"
        f"{err}"
        f"📖 Title: {title}\n"
        f"🌐 URL: {t['url']}\n"
        f"📖 Range: {t['chapter_range']}\n"
        f"{pull_line}"
        f"🕒 Created: {t['created_at']}\n"
        f"✅ Completed: {t['completed_at'] or 'Not completed'}\n"
        f"-----------------------------\n"
    )


@bot.message_handler(commands=["mytasks"])
def cmd_mytasks(message):
    """Caller's own tasks only."""
    if deny_if_needed(message):
        return
    rows = _fetch_task_rows(user_id=message.from_user.id)

    if not rows:
        reply_notice(message, "ℹ️ You have no tasks.")
        return

    text = "📋 Your tasks:\n\n"
    for t in rows:
        text += _public_task_block(t)
    _send_long_text(message.chat.id, text, user_id=message.from_user.id)


@bot.message_handler(commands=["tasks"])
def cmd_tasks_public(message):
    """Everyone: all tasks, log-style, no username/name."""
    if deny_if_needed(message):
        return
    rows = _fetch_task_rows(newest_first=True, limit=80)

    if not rows:
        reply_notice(message, "ℹ️ No tasks yet.")
        return

    text = "📋 Tasks (no names · newest first, up to 80):\n\n"
    for t in rows:
        text += _public_task_block(t)
    _send_long_text(message.chat.id, text, user_id=message.from_user.id)


def _continue_shared(message, uid: int) -> None:
    """/continue for the shared Postgres queue."""
    _, active = _pg_exec(
        """
        SELECT id FROM wtrlab_tasks
        WHERE user_id = %s AND status IN ('pending', 'running', 'paused')
        LIMIT 1
        """,
        (uid,), fetch=True,
    )
    if active:
        reply_notice(
            message,
            f"⚠️ You already have task #{active[0][0]} pending/running. "
            "Wait or /cancel first.",
        )
        return
    _, rows = _pg_exec(
        """
        SELECT id, chat_id, url, novel_title, chapter_range, status, error,
               username, first_name, last_name
        FROM wtrlab_tasks
        WHERE user_id = %s AND status IN ('failed', 'upload_failed')
        ORDER BY id DESC LIMIT 1
        """,
        (uid,), fetch=True,
    )
    if not rows:
        reply_notice(
            message, "ℹ️ No failed task to continue. Use /download for a new novel."
        )
        return
    (tid, chat_id, url, title, crange, status, error,
     uname, fname, lname) = rows[0]
    label = title or url
    if (error or "").startswith("Daily chapter limit"):
        # New task goes to the back of the queue; retire the old row.
        new_id, err = insert_task(
            chat_id=int(chat_id), user_id=uid, url=url,
            chapter_range=crange or "all",
            username=uname, first_name=fname, last_name=lname,
        )
        if err or not new_id:
            reply_notice(message, f"❌ {err or 'Could not queue the task.'}")
            return
        _pg_exec(
            """
            UPDATE wtrlab_tasks
            SET status = 'cancelled', error = %s, completed_at = NOW()
            WHERE id = %s AND status IN ('failed', 'upload_failed')
            """,
            (f"Chapter limit; continued as task #{new_id}", tid),
        )
        reply_notice(
            message,
            f"✅ Queued as new task #{new_id} (previous #{tid} hit the daily "
            f"chapter limit).\n📖 {label}\n🔍 Range: {crange}\n\n"
            "It joins the end of the queue and resumes from cached chapters. "
            "Send /cap to check your remaining chapters.",
        )
        return
    requeue_task(tid)
    log.info("[CONTINUE] user=%s re-queued task #%s", uid, tid)
    reply_notice(
        message,
        f"✅ Re-queued task #{tid} [{status}→pending]\n"
        f"📖 {label}\n🔍 Range: {crange}\n\n"
        "Worker will resume from cached chapters shortly.",
    )


@bot.message_handler(commands=["continue"])
def cmd_continue(message):
    """User: latest own failed. Admin: /continue <id> for any failed/cancelled."""
    if deny_if_needed(message):
        return
    uid = message.from_user.id
    args = (message.text or "").split()[1:]
    if is_admin(uid) and args and args[0].isdigit():
        ok, msg = admin_requeue_task(int(args[0]))
        reply_notice(message, ("✅ " if ok else "⚠️ ") + msg)
        return

    if USE_SHARED_TASKS:
        _continue_shared(message, uid)
        return

    conn = local_db()
    try:
        active = conn.execute(
            """
            SELECT id FROM tasks
            WHERE user_id = ? AND status IN ('pending', 'running', 'paused')
            LIMIT 1
            """,
            (uid,),
        ).fetchone()
        if active:
            reply_notice(
                message,
                f"⚠️ You already have task #{active['id']} pending/running. "
                "Wait or /cancel first.",
            )
            return

        row = conn.execute(
            """
            SELECT id, chat_id, user_id, username, first_name, last_name,
                   url, novel_title, chapter_range, status, error
            FROM tasks
            WHERE user_id = ?
              AND status IN ('failed', 'upload_failed')
            ORDER BY id DESC
            LIMIT 1
            """,
            (uid,),
        ).fetchone()
        if not row:
            reply_notice(
                message,
                "ℹ️ No failed task to continue. Use /download for a new novel.",
            )
            return

        # Chapter-limit failures go to the BACK of the queue as a new task.
        # Reviving the old row would keep its low id and jump ahead of
        # everyone waiting (the worker picks the lowest pending id first).
        if (row["error"] or "").startswith("Daily chapter limit"):
            conn.execute("BEGIN IMMEDIATE")
            cur = conn.execute(
                """
                INSERT INTO tasks (
                    chat_id, user_id, username, first_name, last_name,
                    url, chapter_range, status
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, 'pending')
                """,
                (
                    row["chat_id"],
                    row["user_id"],
                    row["username"],
                    row["first_name"],
                    row["last_name"],
                    row["url"],
                    row["chapter_range"],
                ),
            )
            new_id = int(cur.lastrowid)
            # Retire the old row so it does not count twice toward
            # DAILY_TASK_LIMIT; chapters stay cached so nothing is lost.
            conn.execute(
                """
                UPDATE tasks
                SET status = 'cancelled',
                    error = ?,
                    completed_at = datetime('now')
                WHERE id = ? AND status IN ('failed', 'upload_failed')
                """,
                (f"Chapter limit; continued as task #{new_id}", row["id"]),
            )
            conn.commit()
            title = row["novel_title"] or row["url"]
            reply_notice(
                message,
                f"✅ Queued as new task #{new_id} (previous #{row['id']} hit the "
                "daily chapter limit).\n"
                f"📖 {title}\n"
                f"🔍 Range: {row['chapter_range']}\n\n"
                "It joins the end of the queue and resumes from cached chapters. "
                "Send /cap to check your remaining chapters.",
            )
            return

        conn.execute(
            """
            UPDATE tasks
            SET status = 'pending',
                completed_at = NULL,
                error = NULL
            WHERE id = ? AND status IN ('failed', 'upload_failed')
            """,
            (row["id"],),
        )
        conn.commit()
        title = row["novel_title"] or row["url"]
        reply_notice(
            message,
            f"✅ Re-queued task #{row['id']} [{row['status']}→pending]\n"
            f"📖 {title}\n"
            f"🔍 Range: {row['chapter_range']}\n\n"
            "Worker will resume from cached chapters shortly.",
        )
    finally:
        conn.close()


def _admin_only(message) -> bool:
    if is_admin(message.from_user.id):
        return True
    reply_notice(message, "⛔ Admins only.")
    return False


def _user_label(user_id: int) -> str:
    name = username_for(user_id)
    return f"{user_id}" + (f" (@{name})" if name else "")


@bot.message_handler(commands=["banuser"])
def cmd_banuser(message):
    """/banuser <user_id|@username> [days] [reason...] — no days = forever."""
    if not _admin_only(message):
        return
    args = [a for a in (message.text or "").split()[1:] if a != "-"]
    if not args:
        reply_notice(
            message,
            "Usage:\n/banuser <user_id|@username>  — permanent ban\n"
            "/banuser <user_id|@username> 7  — 7-day ban",
        )
        return
    uid = resolve_user_ref(args[0])
    if uid is None:
        reply_notice(
            message,
            "❓ I don't know that user. Use their numeric ID, or an @username "
            "they have used the bot with.",
        )
        return
    if is_admin(uid):
        reply_notice(message, "⛔ Admins can't be banned.")
        return
    days = None
    rest = args[1:]
    if rest and rest[0].isdigit():
        days = int(rest[0])
        rest = rest[1:]
        if days <= 0:
            reply_notice(message, "Days must be 1 or more (leave it out for a permanent ban).")
            return
    reason = " ".join(rest)[:200]
    until = ban_user(uid, days, message.from_user.id, reason)
    cancelled = cancel_user_tasks(uid)
    pending_download.pop(uid, None)
    log_action(None, None, uid, username_for(uid), "banned",
               f"by {message.from_user.id}; until={until or 'forever'}; {reason}")
    try:
        bot.send_message(
            uid,
            "🚫 You have been banned from using this bot"
            + (f" until {until} UTC." if until else " permanently.")
            + (f"\nReason: {reason}" if reason else ""),
        )
    except Exception:
        pass
    reply_notice(
        message,
        f"🚫 Banned {_user_label(uid)} "
        + (f"until {until} UTC" if until else "permanently")
        + f". Cancelled {cancelled} active task(s).",
    )


@bot.message_handler(commands=["unbanuser"])
def cmd_unbanuser(message):
    if not _admin_only(message):
        return
    args = [a for a in (message.text or "").split()[1:] if a != "-"]
    if not args:
        reply_notice(message, "Usage: /unbanuser <user_id|@username>")
        return
    uid = resolve_user_ref(args[0])
    if uid is None:
        reply_notice(message, "❓ I don't know that user.")
        return
    if unban_user(uid):
        log_action(None, None, uid, username_for(uid), "unbanned", f"by {message.from_user.id}")
        try:
            bot.send_message(uid, "✅ Your ban has been lifted. You can use the bot again.")
        except Exception:
            pass
        reply_notice(message, f"✅ Unbanned {_user_label(uid)}.")
    else:
        reply_notice(message, f"ℹ️ {_user_label(uid)} was not banned.")


@bot.message_handler(commands=["userlimit"])
def cmd_userlimit(message):
    """/userlimit <user_id|@username> <number|off|default> — daily fresh-chapter cap."""
    if not _admin_only(message):
        return
    args = [a for a in (message.text or "").split()[1:] if a != "-"]
    if len(args) < 2:
        reply_notice(
            message,
            "Usage: /userlimit <user_id|@username> <number|off|default>\n"
            "• number — custom daily fresh-chapter cap for that user\n"
            "• off — no chapter cap for that user\n"
            "• default — back to the server default",
        )
        return
    uid = resolve_user_ref(args[0])
    if uid is None:
        reply_notice(message, "❓ I don't know that user.")
        return
    value = args[1].lower()
    if value in ("off", "unlimited", "0"):
        set_user_cap_override(uid, 0)
        text = f"♾ {_user_label(uid)}: no daily chapter cap."
    elif value in ("default", "reset"):
        set_user_cap_override(uid, None)
        default_cap = get_default_chapter_cap()
        text = (
            f"↩️ {_user_label(uid)}: back to the default "
            f"({'unlimited' if default_cap <= 0 else str(default_cap) + ' chapters/day'})."
        )
    elif value.isdigit() and int(value) > 0:
        set_user_cap_override(uid, int(value))
        text = f"✅ {_user_label(uid)}: daily fresh-chapter cap is now {int(value)}."
    else:
        reply_notice(message, "Use a number, `off`, or `default`.")
        return
    log_action(None, None, uid, username_for(uid), "userlimit", f"{value} by {message.from_user.id}")
    reply_notice(message, text)


@bot.message_handler(commands=["actions"])
def cmd_actions(message):
    """/actions [user_id|@username] [count] — what users did in the remote Chrome."""
    if not _admin_only(message):
        return
    limit = 60
    uid = None
    for arg in (message.text or "").split()[1:]:
        if arg.isdigit() and len(arg) <= 3:
            limit = max(1, min(int(arg), 300))
        else:
            uid = resolve_user_ref(arg)
            if uid is None:
                reply_notice(message, "❓ I don't know that user.")
                return
    conn = local_db()
    try:
        if uid is None:
            rows = conn.execute(
                "SELECT ts, session_id, task_id, user_id, username, kind, detail "
                "FROM user_actions ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT ts, session_id, task_id, user_id, username, kind, detail "
                "FROM user_actions WHERE user_id = ? ORDER BY id DESC LIMIT ?",
                (uid, limit),
            ).fetchall()
    finally:
        conn.close()
    if not rows:
        reply_notice(message, "ℹ️ No logged actions yet.")
        return
    lines = ["📋 User actions (UTC, oldest first)"]
    last_session = object()
    for row in reversed(rows):
        if row["session_id"] != last_session:
            last_session = row["session_id"]
            who = f"{row['user_id']}" + (f" @{row['username']}" if row["username"] else "")
            lines.append("")
            lines.append(
                f"— session {row['session_id'] or '-'} · user {who} · task {row['task_id'] or '-'}"
            )
        lines.append(f"{(row['ts'] or '')[5:19]}  {row['kind']}: {row['detail'] or ''}")
    lines.append("")
    lines.append("Note: a 'reload' right after the checkbox is normal (Cloudflare reloads the page).")
    _send_long_text(message.chat.id, "\n".join(lines), user_id=message.from_user.id)


@bot.message_handler(commands=["solved"])
def cmd_solved(message):
    """
    Resume a crawler blocked on Cloudflare Turnstile.
    While a captcha is waiting, any allowed user may send /solved (including
    people in OUTPUT_GROUPS who opened the shared solve link).
    """
    if deny_if_needed(message):
        return
    global captcha_wait
    if not captcha_wait:
        reply_notice(
            message,
            "ℹ️ Nothing is waiting on a captcha right now.",
        )
        return
    ctx = dict(captcha_wait)
    sender = message.from_user.id
    # Anyone allowed to use the bot can clear the shared captcha — helps when
    # the link was posted to OUTPUT_GROUPS and a volunteer solves it.
    solved_event.set()
    detail = ""
    if ctx.get("chapter_no") is not None:
        detail = f" (chapter {ctx['chapter_no']})"
    if ctx.get("task_id") is not None:
        detail = f" task #{ctx['task_id']}{detail}"
    owner = ctx.get("user_id")
    who = f"user {sender}"
    if owner is not None and sender == owner:
        who = "task owner"
    elif is_admin(sender):
        who = "admin"
    reply_notice(
        message,
        f"✅ /solved received{detail} ({who}). Worker will recheck and resume "
        "if the challenge is gone.",
    )
    notify_admin(
        f"✅ <b>/solved</b> from <code>{sender}</code> ({html.escape(who)})"
        f"{html.escape(detail)}."
    )


@bot.message_handler(commands=["logs"])
def cmd_logs(message):
    """Admin: same layout as reference /logs (with user details)."""
    if deny_if_needed(message):
        return
    if not is_admin(message.from_user.id):
        reply_notice(message, "❌ Access Denied.")
        return
    rows = _fetch_task_rows(newest_first=True, limit=60)
    rows.reverse()  # newest last, so the latest tasks sit at the bottom of the chat

    if not rows:
        reply_notice(message, "ℹ️ No task logs found.")
        return

    cap = get_chapter_cap()
    text = "📜 Task Logs (with user details):\n\n"
    for t in rows:
        username = f"@{t['username']}" if t["username"] else "N/A"
        first = t["first_name"] or "N/A"
        last = t["last_name"] or "N/A"
        used = count_chapter_pulls(int(t["user_id"]), hours=24)
        if cap > 0:
            pull_line = f"⬇️ Fresh downloads 24h: {used}/{cap}\n"
        else:
            pull_line = f"⬇️ Fresh downloads 24h: {used} (unlimited)\n"
        err = (t["error"] or "").strip() if t["error"] is not None else ""
        error_line = err if err else "N/A"
        text += (
            f"🆔 Task ID: {t['id']}\n"
            f"👤 User ID: {t['user_id']}\n"
            f"🔗 Username: {username}\n"
            f"📛 Name: {first} {last}\n"
            f"🖥 Server: {_task_server_label(t)}\n"
            f"⚙️ Crawler: wtr_local\n"
            f"📌 Status: {t['status']}\n"
            f"❌ Error: {error_line}\n"
            f"🌐 URL: {t['url']}\n"
            f"📖 Range: {t['chapter_range']}\n"
            f"{pull_line}"
            f"🕒 Created: {t['created_at']}\n"
            f"✅ Completed: {t['completed_at'] or 'Not completed'}\n"
            f"-----------------------------\n"
        )
    _send_long_text(message.chat.id, text, user_id=message.from_user.id)


@bot.message_handler(commands=["trial"])
def cmd_trial(message):
    """Same layout as reference /trial — full task table."""
    if deny_if_needed(message):
        return
    if not is_admin(message.from_user.id):
        reply_notice(message, "❌ Access Denied.")
        return
    conn = local_db()
    try:
        rows = conn.execute(
            """
            SELECT id, user_id, username, status, url, chapter_range,
                   novel_title, created_at, completed_at
            FROM tasks
            ORDER BY id ASC
            """
        ).fetchall()
    finally:
        conn.close()

    if not rows:
        reply_notice(message, "ℹ️ Queue is empty.")
        return

    text = "📋 All trial in Table:\n\n"
    for t in rows:
        text += (
            f"🆔 Task ID: {t['id']}\n"
            f"👤 User ID: {t['user_id']}\n"
            f"🖥 Server: local\n"
            f"⚙️ Crawler: wtr_local\n"
            f"📌 Status: {t['status']}\n"
            f"🌐 URL: {t['url']}\n"
            f"📖 Range: {t['chapter_range']}\n"
            f"🕒 Created: {t['created_at']}\n"
            f"✅ Completed: {t['completed_at'] or 'Not completed'}\n"
            f"------------------------------\n"
        )
    _send_long_text(message.chat.id, text, user_id=message.from_user.id)




@bot.message_handler(commands=["premium"])
def cmd_premium(message):
    if deny_if_needed(message):
        return
    uid = message.from_user.id
    # Admins use the normal user path (so they can test buying Premium).
    # Their unlimited limits still come from ADMIN_USER_IDS and always win.
    prem = get_db_premium(uid)
    extend_markup = telebot.types.InlineKeyboardMarkup()
    extend_markup.add(
        telebot.types.InlineKeyboardButton(
            f"⭐ Add {PREMIUM_STARS_WEEKS * 7} days — {PREMIUM_STARS_PRICE} Stars",
            callback_data="pay_stars",
        )
    )
    if prem:
        exp = prem.get("expires_at")
        days, exp_s = "?", str(exp)
        try:
            import datetime as _dt
            if exp is not None:
                left = (exp - _dt.datetime.now(exp.tzinfo)) if getattr(exp, "tzinfo", None) else (exp - _dt.datetime.utcnow())
                days = max(0, int(left.total_seconds() // 86400))
        except Exception:
            pass
        reply_notice(
            message,
            (
                "⭐ <b>Premium active</b>\n\n"
                f"Plan: <code>{html.escape(str(prem.get('plan')))}</code>\n"
                f"Expires: <code>{html.escape(exp_s)}</code> (~{days} days left)\n\n"
                f"• Up to <b>{PREMIUM_CHAPTERS_PER_DAY}</b> chapters/day\n"
                f"• Up to <b>{PREMIUM_TASKS_PER_DAY}</b> download tasks/day\n"
                "• Priority queue · Auto-Continue (prompted when you queue)\n"
                "• /stopautocontinue — pick a novel to stop auto-continue\n\n"
                f"Want more time? Tap below to add {PREMIUM_STARS_WEEKS * 7} days "
                f"for {PREMIUM_STARS_PRICE} ⭐."
            ),
            parse_mode="HTML",
            reply_markup=extend_markup,
        )
        return
    text = (
        f"⭐ <b>Premium</b> — {PREMIUM_STARS_PRICE} Stars or $1 / week\n\n"
        f"• Up to <b>{PREMIUM_CHAPTERS_PER_DAY}</b> chapters per day\n"
        f"• Up to <b>{PREMIUM_TASKS_PER_DAY}</b> tasks per day\n"
        "• Priority queue + Auto-Continue\n\n"
        f"<b>Telegram Stars</b>: pay the exact {PREMIUM_STARS_PRICE} ⭐ and Premium "
        "activates automatically (7 days).\n"
        "<b>PayPal</b>: after paying, tap <b>I've paid</b> so admin is notified, "
        "then wait for activation.\n"
        f"{REFUND_POLICY_SHORT}\n\n"
        "Admin: <a href=\"https://t.me/patiorjunrick004\">@patiorjunrick004</a> · "
        "/paysupport · /terms\n\n"
        "Choose a payment method:"
    )
    if is_admin(uid):
        text += (
            "\n\n🛠 You are an admin: your limits stay unlimited (from .env) "
            "whatever you buy. Buying here is for testing the payment flow."
        )
    markup = telebot.types.InlineKeyboardMarkup()
    markup.add(
        telebot.types.InlineKeyboardButton(
            f"⭐ Pay {PREMIUM_STARS_PRICE} Stars", callback_data="pay_stars"
        )
    )
    markup.add(telebot.types.InlineKeyboardButton("PayPal", callback_data="pay_paypal"))
    msg = bot.send_message(
        message.chat.id, text, parse_mode="HTML",
        reply_markup=markup, link_preview_options=LINK_PREVIEW_OFF,
    )
    session_start(uid, step="premium_pay", premium_msg_id=msg.message_id, premium_chat_id=message.chat.id)

@bot.message_handler(commands=["grantpremium"])
def cmd_grantpremium(message):
    if deny_if_needed(message):
        return
    if not is_admin(message.from_user.id):
        reply_notice(message, "❌ Admin only.")
        return
    parts = (message.text or "").split()
    if len(parts) < 2 or not parts[1].lstrip("-").isdigit():
        reply_notice(message, "Usage: /grantpremium <user_id> [weeks]")
        return
    weeks = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 1
    ok, msg = grant_premium(int(parts[1]), weeks=weeks, granted_by=message.from_user.id)
    reply_notice(message, ("✅ " if ok else "⚠️ ") + msg)


@bot.message_handler(commands=["revokepremium"])
def cmd_revokepremium(message):
    if deny_if_needed(message):
        return
    if not is_admin(message.from_user.id):
        reply_notice(message, "❌ Admin only.")
        return
    parts = (message.text or "").split()
    if len(parts) < 2 or not parts[1].lstrip("-").isdigit():
        reply_notice(message, "Usage: /revokepremium <user_id>")
        return
    ok, msg = revoke_premium(int(parts[1]))
    reply_notice(message, ("✅ " if ok else "⚠️ ") + msg)


@bot.message_handler(commands=["stopautocontinue", "stopauto"])
def cmd_stopauto(message):
    if deny_if_needed(message):
        return
    if not USE_SHARED_TASKS:
        reply_notice(message, "Auto-continue requires multi-worker DB.")
        return
    uid = message.from_user.id
    try:
        conn = shared_db()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT id, url, chapter_range, runs_done, max_runs
                    FROM wtrlab_auto_continue
                    WHERE user_id = %s AND enabled = TRUE
                    ORDER BY id DESC LIMIT 20
                    """,
                    (uid,),
                )
                rows = cur.fetchall() or []
        finally:
            conn.close()
    except Exception as e:
        reply_notice(message, f"⚠️ {e}")
        return
    if not rows:
        reply_notice(message, "ℹ️ No active auto-continue jobs.")
        return
    markup = telebot.types.InlineKeyboardMarkup()
    for ac_id, url, cr, rd, mr in rows:
        short = (url or "")[-40:]
        markup.add(
            telebot.types.InlineKeyboardButton(
                f"Stop #{ac_id} …{short}",
                callback_data=f"stopac:{ac_id}",
            )
        )
    markup.add(telebot.types.InlineKeyboardButton("Stop all", callback_data="stopac:all"))
    bot.send_message(
        message.chat.id,
        "Pick which auto-continue to stop:",
        reply_markup=markup,
        link_preview_options=LINK_PREVIEW_OFF,
    )
    session_start(uid, step="stopac_pick")

@bot.message_handler(commands=["cap"])
def cmd_cap(message):
    if deny_if_needed(message):
        return
    uid = message.from_user.id
    unlimited = is_admin(uid)
    cap = get_chapter_cap(uid)

    if cap <= 0:
        chapter_block = (
            "⬇️ Fresh chapter downloads: <b>unlimited</b>.\n"
            "Chapters already on the server are always free."
        )
    else:
        remaining = pulls_remaining(uid, unlimited)
        chapter_block = (
            f"⬇️ Fresh chapter downloads: <b>{remaining}/{cap}</b> remaining.\n"
            "This is how many new chapters you can pull from the site right now "
            "(chapters already on the server are free and don't use this).\n"
            "Each download you make recovers on its own 24h timer once it ages "
            "out, so this number refills gradually rather than all at once."
        )

    cap_task_limit = DAILY_TASK_LIMIT
    if not unlimited and get_premium(uid):
        cap_task_limit = PREMIUM_TASKS_PER_DAY
    if cap_task_limit <= 0 or unlimited:
        task_block = "🗂 Tasks: <b>unlimited</b>."
    else:
        tasks_left = max(0, cap_task_limit - count_recent_tasks(uid))
        task_block = f"🗂 Tasks: <b>{tasks_left}/{cap_task_limit}</b> remaining today."

    text = (
        f"{chapter_block}\n\n"
        f"{task_block}\n\n"
        "Contact admin if you need help or another task.\n\n"
        f"⭐ Premium ({PREMIUM_STARS_PRICE} Stars or $1/week): 1000 chapters/day, {PREMIUM_TASKS_PER_DAY} tasks/day, priority, Auto-Continue — /premium"
    )
    reply_notice(message, text, parse_mode="HTML")


@bot.message_handler(commands=["queue"])
def cmd_queue(message):
    """Global wait list: pending/running/paused — no username/name."""
    if deny_if_needed(message):
        return
    if not USE_SHARED_TASKS:
        recover_paused_tasks()
    running = _fetch_task_rows(statuses=["running"])
    rows = _fetch_task_rows(statuses=["pending", "paused"], queue_order=True)

    if not running and not rows:
        reply_notice(message, "ℹ️ Queue is empty.")
        return

    text = "📋 Queue (no names · user id kept):\n\n"
    if running:
        text += "▶️ Running now:\n"
        for t in running:
            text += _public_task_block(t, include_error=False)
        text += "\n"
    if rows:
        text += "⏳ Waiting:\n"
        pos = 0
        for t in rows:
            pos += 1
            boost = " · next-in-line" if int(t["run_next"] or 0) else ""
            text += f"#{pos}{boost}\n"
            text += _public_task_block(t, include_error=False)
    _send_long_text(message.chat.id, text, user_id=message.from_user.id)


@bot.message_handler(commands=["cancel"])
def cmd_cancel(message):
    if deny_if_needed(message):
        return
    uid = message.from_user.id
    args = (message.text or "").split()[1:]
    if is_admin(uid) and args and args[0].isdigit():
        ok, msg = admin_cancel_task(int(args[0]))
        reply_notice(message, ("✅ " if ok else "⚠️ ") + msg)
        return
    n = cancel_user_tasks(uid)
    pending_download.pop(uid, None)
    if n:
        reply_notice(message, f"✅ Cancelled {n} task(s).")
    else:
        reply_notice(message, "ℹ️ Nothing to cancel.")


@bot.message_handler(commands=["pause", "stop"])
def cmd_pause(message):
    if deny_if_needed(message):
        return
    if not is_admin(message.from_user.id):
        reply_notice(message, "❌ Admin only.")
        return
    args = (message.text or "").split()[1:]
    if not args or not args[0].isdigit():
        reply_notice(message, "Usage: /pause <task_id> or /stop <task_id>")
        return
    ok, msg = admin_pause_task(int(args[0]))
    reply_notice(message, ("✅ " if ok else "⚠️ ") + msg)


@bot.message_handler(commands=["start"])
def cmd_start_task_or_help(message):
    """
    /start with no args → help (same as /help).
    /start <id> admin → next-in-line boost.
    """
    if deny_if_needed(message):
        return
    args = (message.text or "").split()[1:]
    if is_admin(message.from_user.id) and args and args[0].isdigit():
        ok, msg = admin_start_task(int(args[0]))
        reply_notice(message, ("✅ " if ok else "⚠️ ") + msg)
        return
    cmd_start(message)


@bot.message_handler(commands=["download"])
def cmd_download(message):
    if deny_if_needed(message):
        return
    args = (message.text or "").split()[1:]
    if is_admin(message.from_user.id) and args and args[0].isdigit():
        ok, msg = admin_start_task(int(args[0]))
        reply_notice(message, ("✅ " if ok else "⚠️ ") + msg)
        return
    session_start(message.from_user.id, step="url")
    reply_notice(
        message,
        "📌 Send a WTR-Lab novel URL.\n"
        "Example:\n"
        "https://wtr-lab.com/en/novel/12345/some-slug\n\n"
        f"(This step expires in {SESSION_SECONDS // 60} minutes.)",
    )


@bot.message_handler(commands=["set"])
def cmd_set(message):
    if deny_if_needed(message):
        return
    if not is_admin(message.from_user.id):
        reply_notice(message, "❌ Admin only.")
        return
    raw = (message.text or "").split()
    if len(raw) < 2:
        reply_notice(
            message,
            "Usage:\n"
            "/set KEY value\n"
            "/set KEY value all     ← all workers (shared config)\n"
            "/set KEY value s5      ← one worker via command bus\n"
            "/set KEY               ← empty on this host\n"
            "Example: /set CHAPTER_CAP 200 all",
        )
        return
    key = raw[1].strip().upper()
    target = "local"
    value_parts = raw[2:]
    if value_parts:
        last = value_parts[-1].lower()
        if last in ("all", "here", "self", "local") or (
            last.startswith("s") and last[1:].isdigit()
        ) or last.startswith("w-"):
            target = last
            value_parts = value_parts[:-1]
    value = " ".join(value_parts)
    if target in ("here", "self", "local", SERVER_ID.lower()):
        old = read_dotenv_key(key)
        session_start(
            message.from_user.id,
            step="set_confirm",
            set_key=key,
            set_value=value,
            set_old=old,
        )
        reply_notice(
            message,
            "Confirm config change on <b>this</b> host?\n\n"
            f"<code>{html.escape(key)}={html.escape(old)}</code>\n"
            "→\n"
            f"<code>{html.escape(key)}={html.escape(value)}</code>\n\n"
            "Reply <b>yes</b> or <b>no</b>.",
            parse_mode="HTML",
        )
        return
    if not USE_SHARED_TASKS:
        reply_notice(message, "Shared DB required for /set … all|server.")
        return
    if key in ("CHAPTER_CAP", "DAILY_TASK_LIMIT"):
        set_shared_config(key, value, message.from_user.id)
        apply_config_live(key, value)
        enqueue_server_command(target, "reload_config", "", message.from_user.id)
        reply_notice(
            message,
            f"✅ Shared <code>{html.escape(key)}</code> = <code>{html.escape(value)}</code> "
            f"(target={html.escape(target)}).",
            parse_mode="HTML",
        )
        return
    cid = enqueue_server_command(target, "set", f"{key}={value}", message.from_user.id)
    reply_notice(
        message,
        f"✅ Queued set on <code>{html.escape(target)}</code> (cmd #{cid}): "
        f"<code>{html.escape(key)}={html.escape(value)}</code>",
        parse_mode="HTML",
    )


@bot.message_handler(commands=["restart"])
def cmd_restart(message):
    if deny_if_needed(message):
        return
    if not is_admin(message.from_user.id):
        reply_notice(message, "❌ Admin only.")
        return
    parts = (message.text or "").split()
    target = parts[1].strip().lower() if len(parts) > 1 else SERVER_ID.lower()
    if target in (SERVER_ID.lower(), "here", "self", "local"):
        try:
            helper = ensure_restart_helper()
            log_path = BASE_DIR / "restart-bot.log"
            subprocess.Popen(
                ["bash", str(helper)],
                cwd=str(BASE_DIR),
                stdout=open(log_path, "a", encoding="utf-8"),
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
            reply_notice(
                message,
                (
                    f"✅ Restart scheduled on <code>{html.escape(SERVER_ID)}</code>. "
                    "pkill chrome/app then Start for Linux.sh."
                ),
                parse_mode="HTML",
            )
            def _exit_later():
                time.sleep(5)
                os._exit(0)
            threading.Thread(target=_exit_later, daemon=True).start()
        except Exception as error:
            reply_notice(message, f"⚠️ Restart failed: {error}")
        return
    if not USE_SHARED_TASKS:
        reply_notice(message, "Shared DB required for /restart <server_id>.")
        return
    cid = enqueue_server_command(target, "restart", "", message.from_user.id)
    if target == "all":
        reply_notice(
            message,
            f"✅ Restart queued for <b>all</b> servers (cmd #{cid}).",
            parse_mode="HTML",
        )
    else:
        reply_notice(
            message,
            f"✅ Restart queued for <code>{html.escape(target)}</code> (cmd #{cid}).",
            parse_mode="HTML",
        )



# Plain-text steps only. Commands must NOT match here: TeleBot uses the first
# matching handler, and this catch-all used to swallow every /command that was
# registered after it (/premiumusers, /paysupport, /terms, /activatestars,
# /refundstars) and silently return.
@bot.message_handler(
    func=lambda m: bool(getattr(m, "text", None)) and not m.text.startswith("/"),
    content_types=["text"],
)
def on_text(message):
    if deny_if_needed(message):
        return
    if not message.text or message.text.startswith("/"):
        return

    uid = message.from_user.id
    state = session_get(uid)
    if not state:
        return
    if session_expired(state):
        pending_download.pop(uid, None)
        reply_notice(
            message,
            "⏰ Session expired (10 minutes). Send the command again "
            "(/download, /login, or /set).",
        )
        return

    text = message.text.strip()
    session_touch(uid)

    if state.get("step") == "set_confirm":
        ans = text.lower().strip()
        yes = ans in ("yes", "y", "yeah", "yep", "positive", "ok", "okay", "confirm")
        no = ans in ("no", "n", "nope", "cancel", "negative")
        key = str(state.get("set_key") or "")
        value = str(state.get("set_value") if state.get("set_value") is not None else "")
        old = str(state.get("set_old") if state.get("set_old") is not None else "")
        if not yes and not no:
            reply_notice(message, "Reply <b>yes</b> or <b>no</b>.", parse_mode="HTML")
            return
        pending_download.pop(uid, None)
        if no:
            reply_notice(
                message,
                f"<code>{html.escape(key)}</code> remains "
                f"<code>{html.escape(old)}</code>.",
                parse_mode="HTML",
            )
            return
        try:
            write_dotenv_key(key, value)
            mode = apply_config_live(key, value)
        except Exception as error:
            reply_notice(
                message,
                f"⚠️ Failed to write config: {type(error).__name__}: {error}",
            )
            return
        if mode == "live":
            reply_notice(
                message,
                f"✅ <code>{html.escape(key)}</code> set to "
                f"<code>{html.escape(value)}</code> (applied live).",
                parse_mode="HTML",
            )
        else:
            reply_notice(
                message,
                f"✅ <code>{html.escape(key)}</code> set to "
                f"<code>{html.escape(value)}</code> in .env.\n"
                "Send <b>/restart</b> for this change to take full effect.",
                parse_mode="HTML",
            )
        return

    if state.get("step") == "login_pick":
        choice = text.strip().lower()
        try:
            fleet = fleet_overview()
        except Exception as error:
            reply_notice(message, f"⚠️ Could not read server status: {error}")
            return
        known = {str(i["server_id"]).lower() for i in fleet}
        if choice.isdigit() and f"s{choice}" in known:
            choice = f"s{choice}"
        if choice not in known:
            reply_notice(
                message,
                "I don't know that server. Reply with one of the names below.\n\n"
                + _login_server_menu(fleet),
                parse_mode="HTML",
                reply_markup=_login_server_buttons(fleet),
            )
            return
        ok, msg_text = _begin_login(uid, choice, fleet)
        if not ok:
            pending_download.pop(uid, None)
        reply_notice(message, msg_text, parse_mode="HTML")
        return

    if state.get("step") == "login_email":
        email = text.strip()
        if "@" not in email or "." not in email.split("@")[-1]:
            reply_notice(message, "Please send a valid email address.")
            return
        target = str(state.get("login_server") or SERVER_ID)
        state["email"] = email
        state["step"] = "login_waiting_link"
        request = {"chat_id": message.chat.id, "user_id": uid, "email": email}
        if _is_remote_server(target):
            cid = enqueue_server_command(
                target.lower(), "login_start", json.dumps(request), uid
            )
            if not cid:
                pending_download.pop(uid, None)
                reply_notice(
                    message,
                    f"⚠️ Could not send the login request to {target}. Try /login again.",
                )
                return
            log.info("[LOGIN] sent login_start for user=%s to server %s (cmd #%s)", uid, target, cid)
        else:
            # The worker thread runs the login on the existing Chrome. Starting
            # a second Chrome on the same profile kills the one that is crawling.
            login_requests.put(request)
        reply_notice(
            message,
            f"⏳ Starting magic-link login on <code>{html.escape(target)}</code> "
            f"for <code>{html.escape(email)}</code>…",
            parse_mode="HTML",
        )
        return

    if state.get("step") == "login_waiting_link":
        if not text.startswith("http"):
            reply_notice(
                message,
                "Please send the full magic link (it should start with https://).",
            )
            return
        target = str(state.get("login_server") or SERVER_ID)
        if _is_remote_server(target):
            cid = enqueue_server_command(
                target.lower(),
                "login_link",
                json.dumps({"user_id": uid, "url": text}),
                uid,
            )
            if not cid:
                reply_notice(
                    message,
                    f"⚠️ Could not forward the link to {target}. Send it again.",
                )
                return
        else:
            state["magic_url"] = text
        reply_notice(
            message,
            f"✅ Magic link received. Completing login on "
            f"<code>{html.escape(target)}</code>…",
            parse_mode="HTML",
        )
        return

    if state.get("step") == "url":
        if not is_wtr_url(text):
            reply_notice(
                message,
                "🚫 Only wtr-lab.com links work with this bot.\n"
                "Send a valid URL or /cancel.",
            )
            return
        state["url"] = text
        state["step"] = "range"
        markup = telebot.types.InlineKeyboardMarkup()
        all_label = "📚 All chapters"
        range_help = (
            "Select chapter range:\n\n"
            "📚 All chapters → from the start (uses cache + daily site limit)\n"
            "↔️ Custom range → e.g. 40-60 or 40 60\n\n"
            "Chapters already on the server are free."
        )
        markup.add(
            telebot.types.InlineKeyboardButton(
                all_label, callback_data="range_all"
            ),
            telebot.types.InlineKeyboardButton(
                "↔️ Custom range", callback_data="range_custom"
            ),
        )
        msg = telegram_call(
            bot.send_message,
            message.chat.id,
            range_help,
            reply_markup=markup,
        )
        track_and_autodelete(msg, uid, DELETE_AFTER_NOTICE)
        return

    if state.get("step") == "custom_range":
        url = state.get("url")
        pending_download.pop(uid, None)
        if not url:
            reply_notice(message, "Session expired. Send /download again.")
            return
        task_id, err = insert_task(
            chat_id=message.chat.id,
            user_id=uid,
            url=url,
            chapter_range=text,
            username=message.from_user.username,
            first_name=message.from_user.first_name,
            last_name=message.from_user.last_name,
        )
        if err:
            reply_notice(message, f"❌ {err}")
            return
        reply_notice(
            message,
            f"✅ Queued task #{task_id}.\n"
            "The local worker will process it and send the EPUB here."
            + queue_wait_note(task_id),
        )



# ---------------------------------------------------------------------------
# Telegram Stars: invoice, checkout, confirmation, support, refunds, admin info
# ---------------------------------------------------------------------------

@bot.callback_query_handler(func=lambda c: c.data == "pay_stars")
def on_pay_stars(call):
    uid = call.from_user.id
    try:
        ban = None if is_admin(uid) else get_ban(uid)
    except Exception:
        ban = None
    if ban:
        bot.answer_callback_query(call.id, "You are banned.", show_alert=True)
        return
    order_id, err = create_star_order(uid)
    if err or not order_id:
        bot.answer_callback_query(call.id, (err or "Error")[:190], show_alert=True)
        return
    try:
        prices = [
            telebot.types.LabeledPrice(
                label=f"WTR-Lab Premium - {PREMIUM_STARS_WEEKS} week(s)",
                amount=PREMIUM_STARS_PRICE,
            )
        ]
        msg = bot.send_invoice(
            chat_id=call.message.chat.id,
            title="WTR-Lab Premium",
            description=(
                f"{PREMIUM_STARS_WEEKS * 7} days of Premium: "
                f"{PREMIUM_CHAPTERS_PER_DAY} chapters/day, "
                f"{PREMIUM_TASKS_PER_DAY} tasks/day, priority queue, Auto-Continue."
            ),
            invoice_payload=order_id,
            provider_token="",
            currency="XTR",
            prices=prices,
        )
        try:
            conn = shared_db()
            try:
                with conn.cursor() as cur:
                    cur.execute(
                        "UPDATE wtrlab_star_orders SET invoice_message_id = %s "
                        "WHERE order_id = %s",
                        (getattr(msg, "message_id", None), order_id),
                    )
                conn.commit()
            finally:
                conn.close()
        except Exception as e:
            print(f"[STARS] save invoice msg id: {e}")
        bot.answer_callback_query(call.id)
    except Exception as e:
        print(f"[STARS] send_invoice: {type(e).__name__}: {e}")
        bot.answer_callback_query(call.id, "Could not create the invoice.", show_alert=True)


@bot.pre_checkout_query_handler(func=lambda q: True)
def on_pre_checkout(query):
    try:
        ban = None if is_admin(query.from_user.id) else get_ban(query.from_user.id)
    except Exception:
        ban = None
    if ban:
        ok, err = False, "You are banned from using this bot."
    else:
        ok, err = validate_star_checkout(
            query.invoice_payload, query.from_user.id,
            query.total_amount, query.currency,
        )
    try:
        if ok:
            bot.answer_pre_checkout_query(query.id, ok=True)
        else:
            bot.answer_pre_checkout_query(query.id, ok=False, error_message=err[:200])
    except Exception as e:
        print(f"[STARS] answer_pre_checkout_query: {e}")


@bot.message_handler(content_types=["successful_payment"])
def on_successful_payment(message):
    sp = message.successful_payment
    uid = message.from_user.id
    uname = message.from_user.username
    chat_id = message.chat.id
    result = None
    last_error = None
    for attempt in range(3):
        try:
            result = process_star_payment(uid, uname, sp)
            break
        except Exception as e:
            last_error = e
            print(f"[STARS] process_star_payment attempt {attempt + 1}: {e}")
            time.sleep(2)

    if result is None:
        # Stars were charged but the DB write failed: never lose the charge id.
        notify_admin(
            "🚨 <b>Stars payment NOT recorded</b>\n"
            f"user_id: <code>{uid}</code> @{html.escape(uname or '—')}\n"
            f"amount: {sp.total_amount} {html.escape(str(sp.currency))}\n"
            f"charge_id: <code>{html.escape(str(sp.telegram_payment_charge_id))}</code>\n"
            f"payload: <code>{html.escape(str(sp.invoice_payload))}</code>\n"
            f"error: {html.escape(str(last_error))[:200]}\n"
            f"Grant manually: /grantpremium {uid} {PREMIUM_STARS_WEEKS}"
        )
        bot.send_message(
            chat_id,
            "⚠️ Your payment went through, but I could not activate Premium "
            "automatically. An admin has been notified with your payment "
            "details. You can also use /paysupport.",
        )
        return

    if result["result"] == "duplicate":
        print(f"[STARS] duplicate payment update ignored: {sp.telegram_payment_charge_id}")
        return

    if result["result"] == "review":
        notify_admin(
            "⚠️ <b>Stars payment needs review</b> (Premium NOT activated)\n"
            f"user_id: <code>{uid}</code> @{html.escape(uname or '—')}\n"
            f"amount: {result['amount']} ⭐\n"
            f"reason: {html.escape(result['problem'])}\n"
            f"charge_id: <code>{html.escape(result['charge_id'])}</code>\n"
            f"payment id: {result.get('payment_id')}\n"
            f"Refund: /refundstars {result.get('payment_id')} [reason]\n"
            f"Manual activation: /activatestars {result.get('payment_id')} [weeks]"
        )
        bot.send_message(
            chat_id,
            "⚠️ Your payment was received but did not match a valid Premium "
            "order, so Premium was not activated. An admin will review it "
            "and refund or activate it. Use /paysupport if you need help.",
        )
        return

    ends = result["ends_at"]
    days = result["weeks"] * 7
    bot.send_message(
        chat_id,
        (
            "✅ <b>Payment successful!</b>\n\n"
            "Your WTR-Lab Premium is active.\n\n"
            f"• Paid: <b>{result['amount']} ⭐</b>\n"
            f"• Added: <b>{days} days</b>\n"
            f"• Active until: <code>{html.escape(fmt_ts(ends))}</code> "
            f"({html.escape(format_time_left(ends))} left)\n"
            f"• {PREMIUM_CHAPTERS_PER_DAY} chapters/day · "
            f"{PREMIUM_TASKS_PER_DAY} tasks/day · priority queue\n\n"
            "Any Premium time you already had was extended, not replaced. "
            "Keep this chat as your receipt — /paysupport if anything is wrong.\n\n"
            f"{REFUND_POLICY_SHORT} See /terms."
        ),
        parse_mode="HTML",
    )
    notify_admin(
        "⭐ <b>Stars payment</b>\n"
        f"user_id: <code>{uid}</code> @{html.escape(uname or '—')}\n"
        f"amount: {result['amount']} ⭐ · +{days} days\n"
        f"expires: {html.escape(fmt_ts(ends))}\n"
        f"payment id: {result['payment_id']}"
    )


@bot.message_handler(commands=["paysupport"])
def cmd_paysupport(message):
    uid = message.from_user.id
    lines = [
        "💬 <b>Payment support</b>",
        "",
        "If you paid with Telegram Stars and Premium did not activate, or you "
        "want a refund, message the admin "
        "(<a href=\"https://t.me/patiorjunrick004\">@patiorjunrick004</a>) and "
        "include your payment id or Telegram receipt.",
    ]
    if USE_SHARED_TASKS:
        try:
            conn = shared_db()
            try:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        SELECT id, amount, status, paid_at
                        FROM wtrlab_star_payments
                        WHERE user_id = %s ORDER BY id DESC LIMIT 5
                        """,
                        (uid,),
                    )
                    rows = cur.fetchall()
            finally:
                conn.close()
            if rows:
                lines.append("")
                lines.append("<b>Your recent Stars payments:</b>")
                for pid, amt, st, paid_at in rows:
                    lines.append(f"• #{pid} · {amt} ⭐ · {html.escape(str(st))} · {html.escape(fmt_ts(paid_at))}")
        except Exception as e:
            print(f"[PAYSUPPORT] {e}")
    bot.send_message(
        message.chat.id, "\n".join(lines), parse_mode="HTML",
        link_preview_options=LINK_PREVIEW_OFF,
    )


@bot.message_handler(commands=["terms"])
def cmd_terms(message):
    bot.send_message(
        message.chat.id,
        (
            "📄 <b>WTR-Lab Premium — terms</b>\n\n"
            f"• Price: <b>{PREMIUM_STARS_PRICE} ⭐ Telegram Stars</b> per "
            f"{PREMIUM_STARS_WEEKS * 7} days (or $1/week via PayPal).\n"
            f"• Includes up to {PREMIUM_CHAPTERS_PER_DAY} fresh chapters/day, "
            f"{PREMIUM_TASKS_PER_DAY} tasks/day, priority queue and Auto-Continue.\n"
            "• Delivery: Premium activates automatically right after Telegram "
            "confirms the payment. Buying again extends your existing time.\n"
            "• <b>Refunds:</b> only for an accidental purchase (for example you "
            "were just testing), and only if you have <b>not used Premium</b>.\n"
            "  Premium counts as used as soon as you queue any task while it is "
            "active, because every Premium task goes through the priority queue "
            "and can start ahead of other users' tasks, even if you keep it "
            "within the normal free limits. Downloading beyond the normal daily "
            "chapter limit or using Auto-Continue also counts as use.\n"
            "• If Premium did not activate, or it failed to work as described, "
            "contact /paysupport and you will be refunded.\n"
            "• Refunds are issued in Stars by the admin, for the full payment "
            "only (no partial or pro-rated refunds), and the time that payment "
            "bought is removed.\n"
            "• Premium is for personal use and is subject to the bot's normal "
            "rules; abuse may lead to a ban."
        ),
        parse_mode="HTML",
        link_preview_options=LINK_PREVIEW_OFF,
    )


@bot.message_handler(commands=["activatestars"])
def cmd_activatestars(message):
    if deny_if_needed(message):
        return
    if not is_admin(message.from_user.id):
        reply_notice(message, "❌ Admin only.")
        return
    parts = (message.text or "").split()
    if len(parts) < 2 or not parts[1].isdigit():
        reply_notice(message, "Usage: /activatestars <payment_id> [weeks]")
        return
    weeks = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 1
    ok, result, buyer = activate_review_star_payment(
        int(parts[1]), message.from_user.id, weeks=weeks
    )
    reply_notice(message, ("✅ " if ok else "⚠️ ") + result)
    if ok and buyer:
        try:
            bot.send_message(
                buyer,
                f"✅ An admin verified your Stars payment and activated {weeks * 7} days of WTR-Lab Premium.",
            )
        except Exception as error:
            print(f"[STARS REVIEW NOTIFY] {error}")


@bot.message_handler(commands=["refundstars"])
def cmd_refundstars(message):
    if deny_if_needed(message):
        return
    if not is_admin(message.from_user.id):
        reply_notice(message, "❌ Admin only.")
        return
    parts = (message.text or "").split(None, 2)
    if len(parts) < 2:
        reply_notice(message, "Usage: /refundstars <payment_id> [reason]")
        return
    ref = parts[1].strip()
    reason = parts[2].strip() if len(parts) > 2 else ""
    try:
        conn = shared_db()
        try:
            with conn.cursor() as cur:
                if ref.isdigit():
                    cur.execute(
                        """
                        SELECT id, user_id, telegram_username, amount, status, paid_at,
                               telegram_payment_charge_id
                        FROM wtrlab_star_payments WHERE id = %s
                        """,
                        (int(ref),),
                    )
                else:
                    cur.execute(
                        """
                        SELECT id, user_id, telegram_username, amount, status, paid_at,
                               telegram_payment_charge_id
                        FROM wtrlab_star_payments
                        WHERE telegram_payment_charge_id = %s
                        """,
                        (ref,),
                    )
                row = cur.fetchone()
        finally:
            conn.close()
    except Exception as e:
        reply_notice(message, f"⚠️ Lookup failed: {e}")
        return
    if not row:
        reply_notice(message, "⚠️ Payment not found.")
        return
    pid, puid, puname, amt, status, paid_at, charge = row
    if status not in ("paid", "review"):
        reply_notice(message, f"⚠️ Payment #{pid} is '{status}' — nothing to refund.")
        return
    # Did the buyer use Premium (priority queue) after paying?
    usage_line = ""
    try:
        _, urows = _pg_exec(
            """
            SELECT COUNT(*), MIN(created_at) FROM wtrlab_tasks
            WHERE user_id = %s AND queue = 'premium' AND created_at >= %s
            """,
            (int(puid), paid_at), fetch=True,
        )
        used_n, first_used = urows[0] if urows else (0, None)
        if used_n:
            usage_line = (
                f"\n⚠️ PREMIUM WAS USED: {used_n} task(s) went through the "
                f"priority queue since payment (first: {fmt_ts(first_used)}). "
                "Under the terms this purchase is NOT eligible for a refund "
                "unless Premium failed to work.\n"
            )
        else:
            usage_line = (
                "\n✅ No Premium tasks queued since payment — eligible if the "
                "purchase was accidental.\n"
            )
    except Exception as error:
        log.warning("[REFUND] usage check failed: %s", error)
        usage_line = "\n(Could not check Premium usage.)\n"
    markup = telebot.types.InlineKeyboardMarkup()
    markup.row(
        telebot.types.InlineKeyboardButton("✅ Confirm refund", callback_data=f"rfc:{pid}"),
        telebot.types.InlineKeyboardButton("Cancel", callback_data="rfx"),
    )
    if reason:
        _REFUND_REASONS[(message.from_user.id, pid)] = reason
    bot.send_message(
        message.chat.id,
        (
            f"Refund payment #{pid}?\n\n"
            f"Buyer: {puid} (@{puname or '—'})\n"
            f"Amount: {amt} ⭐\n"
            f"Paid: {fmt_ts(paid_at)}\n"
            f"Charge ID: {charge}\n"
            + (f"Reason: {reason}\n" if reason else "")
            + usage_line
            + "\nThis refunds the Stars. Any Premium time granted for this "
            "payment will also be removed."
        ),
        reply_markup=markup,
    )


_REFUND_REASONS: dict = {}


@bot.callback_query_handler(func=lambda c: (c.data or "").startswith("rfc:") or c.data == "rfx")
def on_refund_confirm(call):
    if not is_admin(call.from_user.id):
        bot.answer_callback_query(call.id, "Admin only.", show_alert=True)
        return
    if call.data == "rfx":
        try:
            bot.edit_message_text("Refund cancelled.", call.message.chat.id, call.message.message_id)
        except Exception:
            pass
        bot.answer_callback_query(call.id)
        return
    pid = int(call.data.split(":", 1)[1])
    reason = _REFUND_REASONS.pop((call.from_user.id, pid), "")
    ok, msg, buyer = refund_star_payment_record(pid, call.from_user.id, reason)
    try:
        bot.edit_message_text(("✅ " if ok else "⚠️ ") + msg, call.message.chat.id, call.message.message_id)
    except Exception:
        pass
    bot.answer_callback_query(call.id)
    if ok and buyer:
        try:
            bot.send_message(
                buyer,
                "↩️ Your Telegram Stars payment was refunded. Any Premium time "
                "granted for that payment has been removed. Use /paysupport if you have questions.",
            )
        except Exception as e:
            print(f"[REFUND NOTIFY] {e}")


@bot.message_handler(commands=["premiumusers", "premiuminfo"])
def cmd_premiumusers(message):
    try:
        _premiumusers_impl(message)
    except Exception as error:
        log.exception("/premiumusers failed")
        reply_notice(
            message, f"⚠️ /premiumusers failed: {type(error).__name__}: {error}"
        )


def _premiumusers_impl(message):
    if deny_if_needed(message):
        return
    if not is_admin(message.from_user.id):
        reply_notice(message, "❌ Admin only.")
        return
    if not USE_SHARED_TASKS:
        reply_notice(message, "⚠️ Shared database not configured.")
        return
    arg = ((message.text or "").split(None, 1) + [""])[1].strip().lower()
    include_expired = arg == "all"
    single = int(arg) if arg.lstrip("-").isdigit() else None
    try:
        conn = shared_db()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT
                      COALESCE(SUM(amount) FILTER (WHERE status = 'paid'), 0),
                      COALESCE(SUM(amount) FILTER (WHERE status = 'refunded'), 0),
                      COUNT(*) FILTER (WHERE status = 'paid'),
                      COUNT(*) FILTER (WHERE status = 'refunded'),
                      COUNT(*) FILTER (WHERE status IN ('review', 'refunding'))
                    FROM wtrlab_star_payments
                    """
                )
                paid_stars, refunded_stars, n_paid, n_ref, n_review = cur.fetchone()
                where = []
                params: list = []
                if single is not None:
                    where.append("p.user_id = %s")
                    params.append(single)
                elif not include_expired:
                    where.append("p.expires_at > NOW()")
                cur.execute(
                    f"""
                    SELECT p.user_id, p.expires_at, p.last_method, p.granted_by,
                           COALESCE(s.stars, 0), COALESCE(s.n, 0), s.last_paid,
                           COALESCE(s.uname,
                               (SELECT t.username FROM wtrlab_tasks t
                                WHERE t.user_id = p.user_id AND t.username IS NOT NULL
                                ORDER BY t.id DESC LIMIT 1)),
                           (SELECT COUNT(*) FROM wtrlab_tasks t
                            WHERE t.user_id = p.user_id
                              AND t.created_at >= NOW() - INTERVAL '24 hours'
                              AND t.status IN ('pending','running','done','failed','upload_failed'))
                    FROM wtrlab_premium p
                    LEFT JOIN (
                        SELECT user_id,
                               SUM(amount) FILTER (WHERE status = 'paid') AS stars,
                               COUNT(*) FILTER (WHERE status = 'paid') AS n,
                               MAX(paid_at) FILTER (WHERE status = 'paid') AS last_paid,
                               (ARRAY_AGG(telegram_username ORDER BY paid_at DESC))[1] AS uname
                        FROM wtrlab_star_payments GROUP BY user_id
                    ) s ON s.user_id = p.user_id
                    {('WHERE ' + ' AND '.join(where)) if where else ''}
                    ORDER BY p.expires_at DESC
                    LIMIT 100
                    """,
                    params,
                )
                users = cur.fetchall()
                history = []
                if single is not None:
                    cur.execute(
                        """
                        SELECT id, amount, status, paid_at, premium_expires_at,
                               telegram_payment_charge_id, note
                        FROM wtrlab_star_payments
                        WHERE user_id = %s ORDER BY id DESC LIMIT 15
                        """,
                        (single,),
                    )
                    history = cur.fetchall()
        finally:
            conn.close()
    except Exception as e:
        reply_notice(message, f"⚠️ Query failed: {e}")
        return

    def method_label(m, granted_by):
        if m == "stars":
            return "Telegram Stars"
        if m == "admin" or granted_by:
            return "Admin grant (PayPal/manual)"
        return "Unknown"

    head = [
        "⭐ Premium overview",
        f"Price: {PREMIUM_STARS_PRICE} ⭐ / {PREMIUM_STARS_WEEKS * 7} days · "
        f"{PREMIUM_CHAPTERS_PER_DAY} chapters/day · {PREMIUM_TASKS_PER_DAY} tasks/day",
        f"Stars collected: {int(paid_stars) + int(refunded_stars)} ⭐ gross · "
        f"{int(refunded_stars)} refunded · {int(paid_stars)} net",
        f"Payments: {int(n_paid)} paid · {int(n_ref)} refunded"
        + (f" · {int(n_review)} need review" if n_review else ""),
        f"Showing: {'user ' + str(single) if single is not None else ('all premium users' if include_expired else 'active premium users')}"
        f" ({len(users)})",
        "",
    ]
    body = []
    for i, (uid, exp, method, granted_by, stars, npay, last_paid, uname, tasks24) in enumerate(users, 1):
        left = format_time_left(exp)
        body.append(
            f"{i}. {('@' + uname) if uname else 'no username'} · id {uid}\n"
            f"   Method: {method_label(method, granted_by)}\n"
            f"   Stars paid: {int(stars)} ⭐ ({int(npay)} payment(s))"
            + (f" · last {last_paid.strftime('%Y-%m-%d')}" if last_paid else "")
            + "\n"
            f"   Remaining: {left} (until {fmt_ts(exp)})\n"
            f"   Tasks (24h): {int(tasks24)}/{PREMIUM_TASKS_PER_DAY}"
            + (f"\n   Granted by admin id: {granted_by}" if granted_by else "")
        )
    if not users:
        body.append("No premium users found.")
    tail = []
    if history:
        tail.append("")
        tail.append("Payment history:")
        for pid, amt, st, paid_at, pexp, charge, note in history:
            tail.append(
                f"• #{pid} · {amt} ⭐ · {st} · {fmt_ts(paid_at)}\n"
                f"  charge: {charge}"
                + (f"\n  note: {note}" if note else "")
            )
    _send_long_text(
        message.chat.id,
        "\n".join(head) + "\n\n".join(body) + "\n".join(tail),
        user_id=message.from_user.id,
    )


@bot.callback_query_handler(func=lambda c: (c.data or "").startswith("pay_") and c.data != "pay_stars")
def on_pay_choice(call):
    uid = call.from_user.id
    state = pending_download.get(uid)
    if not state or state.get("step") != "premium_pay" or session_expired(state):
        bot.answer_callback_query(call.id, "Session expired — /premium again")
        pending_download.pop(uid, None)
        return
    if call.data == "pay_paypal":
        markup = telebot.types.InlineKeyboardMarkup()
        markup.row(
            telebot.types.InlineKeyboardButton(
                "Open PayPal", url="https://www.paypal.com/ncp/payment/3Y9BSSWAS2FKY"
            )
        )
        markup.row(
            telebot.types.InlineKeyboardButton("I've paid", callback_data="pay_done"),
            telebot.types.InlineKeyboardButton("Cancel", callback_data="pay_cancel"),
        )
        body = (
            "💳 <b>PayPal</b> — $1 / week\n\n"
            "Link (copy if needed):\n"
            "https://www.paypal.com/ncp/payment/3Y9BSSWAS2FKY\n\n"
            "After paying, tap <b>I've paid</b> to notify admin."
        )
        try:
            bot.edit_message_text(
                body.replace("\\n", "\n") if False else (
                    "💳 <b>PayPal</b> — $1 / week\n\n"
                    "Link (copy if needed):\n"
                    "https://www.paypal.com/ncp/payment/3Y9BSSWAS2FKY\n\n"
                    "After paying, tap <b>I've paid</b> to notify admin."
                ),
                call.message.chat.id,
                call.message.message_id,
                parse_mode="HTML",
                reply_markup=markup,
                link_preview_options=LINK_PREVIEW_OFF,
            )
        except Exception:
            pass
        session_start(uid, step="premium_pay", premium_msg_id=call.message.message_id)
        bot.answer_callback_query(call.id)
        return
    if call.data == "pay_cancel":
        pending_download.pop(uid, None)
        try:
            bot.edit_message_text("Cancelled.", call.message.chat.id, call.message.message_id)
        except Exception:
            pass
        bot.answer_callback_query(call.id)
        return
    if call.data == "pay_done":
        pending_download.pop(uid, None)
        u = call.from_user
        uname = u.username or "—"
        fname = ((u.first_name or "") + " " + (u.last_name or "")).strip()
        notify_admin(
            "💰 <b>Premium payment claim</b>\n"
            f"user_id: <code>{uid}</code>\n"
            f"username: @{html.escape(uname)}\n"
            f"name: {html.escape(fname)}\n"
            f"Grant: /grantpremium {uid} 1"
        )
        try:
            bot.edit_message_text(
                "✅ Admin notified. Premium activates after they confirm.",
                call.message.chat.id,
                call.message.message_id,
            )
        except Exception:
            pass
        bot.answer_callback_query(call.id, "Admin notified")


@bot.callback_query_handler(func=lambda c: (c.data or "").startswith("login_srv:"))
def on_login_srv(call):
    uid = call.from_user.id
    sid = (call.data or "").split(":", 1)[-1]
    try:
        fleet = fleet_overview() if USE_SHARED_TASKS else []
    except Exception as error:
        bot.answer_callback_query(call.id, "Status lookup failed")
        log.exception("login_srv callback: %s", error)
        return
    if not fleet:
        session_start(uid, step="login_email", login_server=sid)
        ok, text = True, (
            f"📧 Send magic-link email for <code>{html.escape(sid)}</code> "
            f"(expires in {SESSION_SECONDS // 60} min)."
        )
    else:
        ok, text = _begin_login(uid, sid, fleet)
    bot.answer_callback_query(call.id, f"Login {sid}" if ok else "Not possible")
    bot.send_message(call.message.chat.id, text, parse_mode="HTML")


@bot.callback_query_handler(func=lambda c: (c.data or "").startswith("stopac:"))
def on_stopac(call):
    uid = call.from_user.id
    if session_expired(pending_download.get(uid)):
        pending_download.pop(uid, None)
        bot.answer_callback_query(call.id, "Expired")
        return
    arg = (call.data or "").split(":", 1)[-1]
    if not USE_SHARED_TASKS:
        bot.answer_callback_query(call.id, "No shared DB")
        return
    try:
        conn = shared_db()
        try:
            with conn.cursor() as cur:
                if arg == "all":
                    cur.execute(
                        "UPDATE wtrlab_auto_continue SET enabled = FALSE WHERE user_id = %s",
                        (uid,),
                    )
                else:
                    cur.execute(
                        "UPDATE wtrlab_auto_continue SET enabled = FALSE WHERE id = %s AND user_id = %s",
                        (int(arg), uid),
                    )
                n = cur.rowcount
            conn.commit()
        finally:
            conn.close()
        pending_download.pop(uid, None)
        bot.answer_callback_query(call.id, "Stopped")
        try:
            bot.edit_message_text(
                f"✅ Auto-continue stopped ({n}).",
                call.message.chat.id,
                call.message.message_id,
            )
        except Exception:
            pass
    except Exception as e:
        bot.answer_callback_query(call.id, str(e)[:80])


@bot.callback_query_handler(func=lambda c: c.data in ("ac_on", "ac_off"))
def on_auto_continue_choice(call):
    uid = call.from_user.id
    state = pending_download.get(uid)
    if not state or state.get("step") != "auto_continue" or session_expired(state):
        pending_download.pop(uid, None)
        bot.answer_callback_query(call.id, "Session expired")
        return
    url = state.get("url")
    cr = state.get("chapter_range") or "all"
    want_ac = call.data == "ac_on"
    pending_download.pop(uid, None)
    task_id, err = insert_task(
        chat_id=call.message.chat.id,
        user_id=uid,
        url=url,
        chapter_range=cr,
        username=call.from_user.username,
        first_name=call.from_user.first_name,
        last_name=call.from_user.last_name,
        auto_continue=want_ac,
    )
    if err:
        bot.answer_callback_query(call.id, "Failed")
        send_notice(call.message.chat.id, f"❌ {err}", user_id=uid)
        return
    if want_ac and task_id:
        register_auto_continue(
            uid, call.message.chat.id, url, cr,
            username=call.from_user.username,
            first_name=call.from_user.first_name,
            last_name=call.from_user.last_name,
            last_task_id=task_id,
        )
    note = " · Auto-Continue ON" if want_ac else " · Auto-Continue OFF"
    note += queue_wait_note(task_id)
    try:
        bot.edit_message_text(
            f"✅ Queued task #{task_id}{note}",
            call.message.chat.id,
            call.message.message_id,
        )
    except Exception:
        send_notice(call.message.chat.id, f"✅ Queued #{task_id}{note}", user_id=uid)
    bot.answer_callback_query(call.id)


@bot.callback_query_handler(func=lambda c: c.data in ("range_all", "range_custom"))
def on_range_choice(call):
    uid = call.from_user.id
    if not user_allowed(uid):
        bot.answer_callback_query(call.id, "Not allowed")
        return
    state = pending_download.get(uid)
    if not state or not state.get("url"):
        bot.answer_callback_query(call.id, "Session expired")
        return
    try:
        bot.edit_message_reply_markup(
            call.message.chat.id, call.message.message_id, reply_markup=None
        )
    except Exception:
        pass

    if call.data == "range_custom":
        state["step"] = "custom_range"
        msg = telegram_call(
            bot.send_message,
            call.message.chat.id,
            "↔️ Send the range, e.g. <code>40-60</code> or <code>40 60</code>",
            parse_mode="HTML",
        )
        track_and_autodelete(msg, uid, DELETE_AFTER_NOTICE)
        bot.answer_callback_query(call.id)
        return

    url = state["url"]
    # Premium: ask auto-continue before queueing
    if get_premium(uid):
        state["step"] = "auto_continue"
        state["chapter_range"] = "all"
        state["url"] = url
        markup = telebot.types.InlineKeyboardMarkup()
        markup.row(
            telebot.types.InlineKeyboardButton("Auto-Continue ON", callback_data="ac_on"),
            telebot.types.InlineKeyboardButton("OFF", callback_data="ac_off"),
        )
        try:
            bot.edit_message_text(
                "⭐ Premium: Auto-Continue for this novel? "
                "Up to 2 more runs when limit resets. /stopautocontinue to stop. "
                f"(Expires in {SESSION_SECONDS // 60} min.)",
                call.message.chat.id,
                call.message.message_id,
                parse_mode="HTML",
                reply_markup=markup,
            )
        except Exception:
            bot.send_message(
                call.message.chat.id,
                "⭐ Auto-Continue ON or OFF?",
                reply_markup=markup,
            )
        bot.answer_callback_query(call.id)
        return
    pending_download.pop(uid, None)
    task_id, err = insert_task(
        chat_id=call.message.chat.id,
        user_id=uid,
        url=url,
        chapter_range="all",
        username=call.from_user.username,
        first_name=call.from_user.first_name,
        last_name=call.from_user.last_name,
    )
    if err:
        send_notice(call.message.chat.id, f"❌ {err}", user_id=uid)
    else:
        send_notice(
            call.message.chat.id,
            f"✅ Queued task #{task_id}.\n"
            "The local worker will process it and send the EPUB here."
            + queue_wait_note(task_id),
            user_id=uid,
        )
    bot.answer_callback_query(call.id)


# ---------------------------------------------------------------------------
# Worker loop + bot polling
# ---------------------------------------------------------------------------

MAX_CRASH_RESUMES = 3
_crash_counts: dict[int, int] = {}


def resume_after_crash(task: dict, error: BaseException) -> None:
    """Requeue a task whose Chrome died, at most MAX_CRASH_RESUMES times."""
    task_id = int(task["id"])
    count = _crash_counts.get(task_id, 0) + 1
    _crash_counts[task_id] = count
    if count <= MAX_CRASH_RESUMES:
        print(f"[BROWSER] Resuming task #{task_id} after crash ({count}/{MAX_CRASH_RESUMES})")
        requeue_task(task_id)
        return
    print(f"[BROWSER] Task #{task_id} crashed {count} times — leaving it failed.")
    try:
        send_notice(
            task["chat_id"],
            (
                "⚠️ Chrome keeps crashing on this download, so I stopped "
                "retrying automatically.\nCached chapters are safe. Send "
                "/continue to try again."
            ),
            user_id=task.get("user_id"),
        )
    except Exception:
        pass


def drain_login_requests(browser: "WtrBrowser") -> None:
    """Run queued /login requests on the worker thread (the only Chrome driver)."""
    while True:
        try:
            request = login_requests.get_nowait()
        except queue.Empty:
            return
        do_magic_login(
            request["chat_id"],
            request["user_id"],
            request["email"],
            browser=browser,
        )


def worker_loop():
    recover_interrupted_tasks()
    purge_old_finished_tasks(days=7)
    last_purge_at = time.time()
    print("=" * 78)
    print("WTR-Lab LOCAL worker (SQLite queue) + self-service magic-link login")
    print(f"SQLite: {SQLITE_PATH}")
    print(f"Chrome: {'headless2 low-RAM (no window)' if HEADLESS else 'headed / visible (manual Turnstile)'}")
    print(f"Chrome profile: {CHROME_PROFILE_DIR}")
    print(f"Throttle: {CHAPTER_THROTTLE_MIN:.1f}–{CHAPTER_THROTTLE_MAX:.1f}s")
    _cap = get_chapter_cap()
    print(
        f"Daily site chapters (CHAPTER_CAP): "
        f"{'unlimited' if _cap <= 0 else _cap}"
    )
    print("Task log purge: finished tasks older than 7 days, every 24h")
    if HEADLESS:
        print("HEADLESS=1 — no Chrome window. Captcha cannot be solved; set HEADLESS=0 for VNC.")
    else:
        print("Manual Turnstile: solve in Chrome over VNC, then send /solved (or wait 10 min).")
    print("=" * 78)

    browser = WtrBrowser()
    resume_first = None
    last_login_check = 0.0
    try:
        browser.open("https://wtr-lab.com/")
        try:  # fill the login status right away for /servers and /login
            browser.is_logged_in()
            last_login_check = time.time()
        except Exception as error:
            print(f"[LOGIN CHECK] startup check failed: {error}")
        resume_first = resume_own_stale_task()
        while not stop_event.is_set():
            if time.time() - last_purge_at >= 24 * 60 * 60:
                purge_old_finished_tasks(days=7)
                last_purge_at = time.time()

            if not browser.is_alive():
                browser.recreate("session was dead while idle")

            _HB_STATE["task_id"] = None
            if resume_first:
                task, resume_first = resume_first, None
            else:
                task = claim_task()
            if task:
                _HB_STATE["task_id"] = task.get("id")
            if not task:
                recover_paused_tasks()
                drain_login_requests(browser)
                if (
                    LOGIN_IDLE_CHECK_SECONDS > 0
                    and time.time() - last_login_check >= LOGIN_IDLE_CHECK_SECONDS
                ):
                    last_login_check = time.time()
                    try:
                        browser.is_logged_in()
                    except Exception as error:
                        print(f"[LOGIN CHECK] idle check failed: {error}")
                if IS_COORDINATOR:
                    try:
                        process_auto_continues()
                    except Exception as _ac_err:
                        print(f"[AUTO-CONT] {_ac_err}")
                heartbeat_once(None)
                time.sleep(3)
                continue

            log.info(
                "[PICK] task #%s user=%s queue=%s prio=%s server=%s range=%s url=%s",
                task["id"], task.get("user_id"), task.get("queue", "local"),
                task.get("priority", 0), SERVER_ID, task["chapter_range"], task["url"],
            )
            try:
                process_task(task, browser)
                _crash_counts.pop(int(task["id"]), None)
            except DeadBrowser as error:
                print(f"[BROWSER] Dead during task #{task['id']}: {error}")
                resume_after_crash(task, error)
                browser.recreate(str(error))
            except (
                InvalidSessionIdException,
                NoSuchWindowException,
                WebDriverException,
            ) as error:
                if is_dead_session(error):
                    print(f"[BROWSER] Session error #{task['id']}: {error}")
                    resume_after_crash(task, error)
                    browser.recreate(str(error))
                else:
                    mark_failed(task["id"], str(error))
                    log.exception("[TASK ERROR] %s", error)
    except Exception as error:
        log.exception("[WORKER FATAL] %s", error)
    finally:
        browser.close()


def log_db_versions() -> None:
    """Log which Postgres servers we talk to (shared may be v18, chapters v13)."""
    log.info("[CONFIG] SERVER_ID=%s QUEUE_POOL=%s coordinator=%s worker_only=%s",
             SERVER_ID, QUEUE_POOL, IS_COORDINATOR, WORKER_ONLY)
    log.info("[CONFIG] admins=%s allowed_users=%s output_groups=%s",
             sorted(ADMIN_USER_IDS), sorted(ALLOWED_USER_IDS) or "anyone",
             OUTPUT_GROUPS or "none")
    log.info("[CONFIG] chapter_cap=%s daily_task_limit=%s throttle=%.0f-%.0fs headless=%s",
             DEFAULT_CHAPTER_CAP, DAILY_TASK_LIMIT, CHAPTER_THROTTLE_MIN,
             CHAPTER_THROTTLE_MAX, HEADLESS)
    log.info("[CONFIG] library=%s sqlite=%s", LIBRARY_DIR, SQLITE_PATH)
    checks = []
    if USE_SHARED_TASKS:
        checks.append(("shared task DB", shared_db))
    else:
        log.info("[DB] shared task DB disabled (DB_PASSWORD not set) - local SQLite queue")
    if CHAPTER_DATABASE_URL:
        checks.append(("chapter DB", chapter_db))
    else:
        log.info("[DB] chapter DB disabled (CHAPTER_DATABASE_URL not set) - chapters index in SQLite")
    for label, opener in checks:
        try:
            conn = opener()
            try:
                with conn.cursor() as cur:
                    cur.execute("SHOW server_version")
                    version = cur.fetchone()[0]
                log.info("[DB] %s connected: PostgreSQL %s", label, version)
            finally:
                conn.close()
        except Exception as error:
            log.error("[DB] %s connection FAILED: %s: %s", label, type(error).__name__, error)


def main():
    """Coordinator polls Telegram + crawls. Workers only claim shared tasks."""
    setup_local_db()
    try:
        log_db_versions()
    except Exception:
        log.exception("[DB] version check failed")
    if USE_SHARED_TASKS:
        setup_shared_tasks_db()
        heartbeat_once(None)
        start_heartbeat_thread()
        start_command_thread()
    else:
        print("[MODE] local SQLite only (set DB_PASSWORD for multi-worker)")
    try:
        setup_chapter_db()
    except Exception as e:
        print(f"[CHAPTER DB] setup failed: {e}")
    try:
        load_shared_config()
    except Exception as e:
        print(f"[CONFIG] {e}")

    print(
        f"[MODE] SERVER_ID={SERVER_ID} QUEUE_POOL={QUEUE_POOL} "
        f"shared={USE_SHARED_TASKS} worker_only={WORKER_ONLY}"
    )

    worker = threading.Thread(target=worker_loop, name="wtr-worker", daemon=True)
    worker.start()

    if WORKER_ONLY and not IS_COORDINATOR:
        print("[BOT] Worker-only — no Telegram polling.")
        try:
            while not stop_event.is_set():
                heartbeat_once(None)
                try:
                    process_auto_continues()
                except Exception as e:
                    print(f"[AUTO-CONT LOOP] {e}")
                time.sleep(20)
        except KeyboardInterrupt:
            print("\nStopping…")
        finally:
            stop_event.set()
            worker.join(timeout=15)
        return

    print("[BOT] Starting Telegram polling…")
    try:
        bot.infinity_polling(timeout=60, long_polling_timeout=40)
    except KeyboardInterrupt:
        print("\nStopping…")
    finally:
        stop_event.set()
        worker.join(timeout=15)



def worker_main():
    """
    Worker-only mode: process pending/running tasks already in SQLite.
    Does NOT start Telegram polling — users cannot queue new tasks via chat.
    Still uses BOT_TOKEN to *send* progress / EPUB for tasks already in the DB.
    """
    setup_local_db()
    print(
        "[MODE] worker-only — no Telegram polling "
        "(chat cannot queue new tasks)"
    )
    print("       Drains existing pending tasks in SQLite; Ctrl+C to stop.")
    try:
        worker_loop()
    except KeyboardInterrupt:
        print("\nStopping worker…")
    finally:
        stop_event.set()


if __name__ == "__main__":
    main()
