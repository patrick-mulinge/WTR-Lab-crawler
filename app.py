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
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
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

telebot.logger.setLevel(logging.INFO)

# ---------------------------------------------------------------------------
# Configuration — fully local (SQLite on this PC, no shared Postgres)
# ---------------------------------------------------------------------------

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

BOT_TOKEN = os.environ.get("BOT_TOKEN", "").strip()
if not BOT_TOKEN:
    raise SystemExit(
        "BOT_TOKEN is missing. Copy .env.example to .env and set BOT_TOKEN "
        "from @BotFather."
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
LIBRARY_DIR = DATA_DIR / "library"
SQLITE_PATH = DATA_DIR / "worker.sqlite3"

PROGRESS_UPDATE_SECONDS = int(os.environ.get("PROGRESS_UPDATE_SECONDS", "25"))

_legacy = os.environ.get("CHAPTER_THROTTLE_SECONDS")
if _legacy is not None:
    _mid = max(0.0, float(_legacy))
    CHAPTER_THROTTLE_MIN = max(0.0, _mid * 0.75)
    CHAPTER_THROTTLE_MAX = max(CHAPTER_THROTTLE_MIN, _mid * 1.25)
else:
    CHAPTER_THROTTLE_MIN = max(
        0.0, float(os.environ.get("CHAPTER_THROTTLE_MIN", "10"))
    )
    CHAPTER_THROTTLE_MAX = max(
        CHAPTER_THROTTLE_MIN,
        float(os.environ.get("CHAPTER_THROTTLE_MAX", "18")),
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
# UC Turnstile mouse-click only works in headed mode.
_raw_headless = os.environ.get("HEADLESS", "1").strip().lower()
HEADLESS = _raw_headless not in ("0", "false", "no", "off")

# Extra Chrome flags that cut RAM (GPU, extra renderers, images, crashpad).
LOW_RAM_CHROME_ARGS = ",".join(
    [
        "--disable-gpu",
        "--no-sandbox"
        "--disable-dev-shm-usage"
        "--disable-gpu-compositing",
        "--disable-software-rasterizer",
        "--in-process-gpu",
        "--disable-3d-apis",
        "--disable-webgl",
        "--disable-webgl2",
        "--disable-accelerated-2d-canvas",
        "--disable-accelerated-video-decode",
        "--disable-crash-reporter",
        "--disable-breakpad",
        "--disable-extensions",
        "--disable-component-extensions-with-background-pages",
        "--disable-component-update",
        "--disable-background-networking",
        "--disable-sync",
        "--disable-translate",
        "--disable-default-apps",
        "--disable-hang-monitor",
        "--disable-domain-reliability",
        "--disable-client-side-phishing-detection",
        "--disable-notifications",
        "--disable-speech-api",
        "--disable-file-system",
        "--disable-features=Translate,TranslateUI,AudioServiceOutOfProcess>",
        "--disable-site-isolation-trials",
        "--renderer-process-limit=2",
        "--mute-audio",
        "--no-first-run",
        "--no-default-browser-check",
        "--no-pings",
        "--metrics-recording-only",
        "--blink-settings=imagesEnabled=false",

    ]
)

WTR_HOSTS = {"wtr-lab.com", "www.wtr-lab.com"}

bot = telebot.TeleBot(BOT_TOKEN, threaded=True)

DATA_DIR.mkdir(parents=True, exist_ok=True)
LIBRARY_DIR.mkdir(parents=True, exist_ok=True)
CHROME_PROFILE_DIR.mkdir(parents=True, exist_ok=True)

stop_event = threading.Event()
pending_download: dict[int, dict[str, Any]] = {}
login_lock = threading.Lock()
current_login_user: Optional[int] = None


def is_wtr_url(url: str) -> bool:
    try:
        host = (urllib.parse.urlparse(url).hostname or "").lower()
    except Exception:
        return False
    return host in WTR_HOSTS


def user_allowed(user_id: int) -> bool:
    if not ALLOWED_USER_IDS:
        return True
    return user_id in ALLOWED_USER_IDS


def is_admin(user_id: int) -> bool:
    """Admins skip chapter cap and daily task limits."""
    return user_id in ADMIN_USER_IDS


def notify_admin(text: str):
    """Send a message to ADMIN_CHAT_ID if configured."""
    if not ADMIN_CHAT_ID:
        return
    try:
        bot.send_message(ADMIN_CHAT_ID, text, parse_mode="HTML", disable_web_page_preview=True)
    except Exception as e:
        print(f"[ADMIN NOTIFY ERROR] {e}")

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
        return "Cloudflare Turnstile auto-solve failed. Awaiting human intervention in Chrome."
    if isinstance(error, LoginRequired):
        return "WTR-Lab requires login. Use /login or reply with your email when prompted."
    if is_dead_session(error) or isinstance(error, DeadBrowser):
        return "Chrome closed or the browser session died. The worker will reopen Chrome and retry."
    if isinstance(error, TimeoutException) or "script timeout" in str(error).lower():
        return "WTR-Lab took too long to respond (timeout)."
    if isinstance(error, WtrError):
        return str(error)[:400]
    return "The download failed. Cached chapters are kept for a later retry."


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


def get_chapter_cap() -> int:
    """
    Max first-time network chapter fetches per user per novel per 24h
    (CHAPTER_CAP in .env). 0 = unlimited. Cache hits do not count.

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


def count_chapter_pulls(
    user_id: int, novel_id: str, hours: int = 24
) -> int:
    """How many first-time network chapter fetches this user did for novel."""
    conn = local_db()
    try:
        row = conn.execute(
            """
            SELECT COUNT(*) AS c FROM chapter_pulls
            WHERE user_id = ?
              AND novel_id = ?
              AND pulled_at >= datetime('now', ?)
            """,
            (int(user_id), str(novel_id), f"-{int(hours)} hours"),
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


def pulls_remaining(user_id: int, novel_id: str, unlimited: bool = False) -> int:
    """
    Remaining first-time network fetches for this user/novel in 24h.
    Large number means unlimited (admin or CHAPTER_CAP=0).
    """
    if unlimited or is_admin(user_id):
        return 10**9
    cap = get_chapter_cap()
    if cap <= 0:
        return 10**9
    used = count_chapter_pulls(user_id, novel_id, hours=24)
    return max(0, cap - used)


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


def insert_task(
    chat_id: int,
    user_id: int,
    url: str,
    chapter_range: str,
    username: Optional[str] = None,
    first_name: Optional[str] = None,
    last_name: Optional[str] = None,
    unlimited_cap: bool = False,
) -> tuple[Optional[int], Optional[str]]:
    """Returns (task_id, error_message). error_message is None on success."""
    if not is_wtr_url(url):
        return None, "Only wtr-lab.com links are supported."

    # Admins ignore chapter cap and daily task limits.
    if is_admin(user_id):
        unlimited_cap = True

    retry = is_same_link_retry(user_id, url)

    if DAILY_TASK_LIMIT > 0 and not unlimited_cap and not retry:
        used = count_recent_tasks(user_id)
        if used >= DAILY_TASK_LIMIT:
            return None, (
                f"Daily task limit reached ({DAILY_TASK_LIMIT} / 24h).\n"
                "A different novel is blocked while that slot is used.\n"
                "• Resend the same failed/partial link to resume from cache, or\n"
                "• Use /continue to re-queue your latest failed task.\n"
                "Contact admin if you need help or another task."
            )

    chapter_range = apply_range_cap(chapter_range, unlimited=unlimited_cap)

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
                chat_id,
                user_id,
                username,
                first_name,
                last_name,
                url,
                chapter_range,
            ),
        )
        conn.commit()
        return int(cur.lastrowid), None
    finally:
        conn.close()


def claim_task() -> Optional[dict]:
    conn = local_db()
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            """
            SELECT * FROM tasks
            WHERE status = 'pending'
            ORDER BY id ASC
            LIMIT 1
            """
        ).fetchone()
        if not row:
            conn.commit()
            return None
        conn.execute(
            """
            UPDATE tasks
            SET status = 'running', last_progress_at = datetime('now')
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


def update_task_metadata(task_id: int, title: str, total: int):
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


def is_cancelled(task_id: int) -> bool:
    conn = local_db()
    try:
        row = conn.execute(
            "SELECT status FROM tasks WHERE id = ?", (task_id,)
        ).fetchone()
        return bool(row and row["status"] == "cancelled")
    finally:
        conn.close()


def mark_done(task_id: int):
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
    conn = local_db()
    try:
        conn.execute(
            """
            UPDATE tasks
            SET status = 'pending'
            WHERE id = ? AND status NOT IN ('cancelled', 'done')
            """,
            (task_id,),
        )
        conn.commit()
    finally:
        conn.close()


def mark_upload_failed(task_id: int):
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
    conn = local_db()
    try:
        cur = conn.execute(
            """
            UPDATE tasks
            SET status = 'cancelled', completed_at = datetime('now')
            WHERE user_id = ? AND status IN ('pending', 'running')
            """,
            (user_id,),
        )
        conn.commit()
        return cur.rowcount
    finally:
        conn.close()


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

def cache_has_chapter(novel_id: str, chapter_no: int) -> bool:
    conn = local_db()
    try:
        row = conn.execute(
            """
            SELECT xhtml_path FROM chapters
            WHERE novel_id=? AND chapter_no=?
            """,
            (novel_id, chapter_no),
        ).fetchone()

        if not (row and Path(row["xhtml_path"]).is_file()):
            return False

        path = Path(row["xhtml_path"])
        try:
            if chapter_cache_is_bad(path):
                print(
                    f"[CACHE] Re-download chapter {chapter_no}: "
                    "empty or unusable file"
                )
                return False
            text = path.read_text(encoding="utf-8", errors="replace")
        except Exception:
            return False

        # Older builds left WTR-Lab glossary slots (※12⛬) in the XHTML.
        # Treat those as missing so the chapter is fetched and filled in.
        if xhtml_has_unresolved_placeholders(text):
            print(
                f"[CACHE] Re-download chapter {chapter_no}: "
                "unresolved glossary placeholders in cached XHTML"
            )
            return False

        return True
    finally:
        conn.close()


def cache_chapter(
    novel_id: str,
    chapter_no: int,
    chapter_id: str,
    title: str,
    file_path: Path,
):
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
            (novel_id, chapter_no, chapter_id, title, str(file_path)),
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

class WtrBrowser:
    """
    Chrome via SeleniumBase UC Mode, tuned for low RAM.
    HEADLESS=1 (default): no window, no Turnstile GUI, no extra UC tabs.
    HEADLESS=0: visible window — only then is Turnstile auto-click used.
    """

    def __init__(self):
        self.driver = None
        self.on_status = None
        self._spawn()

    def _spawn(self):
        kwargs = {
            "uc": True,
            "user_data_dir": str(CHROME_PROFILE_DIR),
            "block_images": True,
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
        self.driver.uc_open_with_reconnect(url, 4)
        self._try_solve_turnstile()
        self._keep_one_tab()

    def html(self) -> str:
        return self.driver.get_page_source()

    def is_login_page(self) -> bool:
        try:
            src = (self.html() or "").lower()
            title = (self.driver.get_title() or "").lower()
            return (
                "continue with email" in src
                or "welcome to wtr-lab" in src
                or "sign in to continue" in src
                or "login" in title and "wtr" in title
            )
        except Exception:
            return False

    def is_logged_in(self) -> bool:
        """Quick check: open profile page and see if we land on a login form."""
        try:
            self.open("https://wtr-lab.com/en/profile")
            time.sleep(2.5)
            return not self.is_login_page()
        except Exception:
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

    def _try_solve_turnstile(self) -> bool:
        if HEADLESS:
            # Still detect + notify so admin knows, even if we cannot click.
            try:
                title = (self.driver.get_title() or "").lower()
                source = (self.driver.get_page_source() or "").lower()
                looks_blocked = (
                    "cloudflare" in title
                    or "just a moment" in title
                    or "turnstile" in source
                    or "cf-challenge" in source
                    or "cf-turnstile" in source
                    or "verify you are human" in source
                )
                if looks_blocked:
                    print("[TURNSTILE] Challenge detected (headless — cannot auto-click).")
                    self._notify(
                        "turnstile",
                        "Turnstile detected in headless mode. Set HEADLESS=0 for auto-click.",
                    )
                    notify_admin(
                        "🛡️ <b>Turnstile challenge</b> detected on the worker (headless).\n"
                        "Auto-click is disabled. Set <code>HEADLESS=0</code> and use a "
                        "visible window / VNC to solve it, or use /login after solving."
                    )
            except Exception:
                pass
            return True
        try:
            title = (self.driver.get_title() or "").lower()
            source = (self.driver.get_page_source() or "").lower()
            looks_blocked = (
                "cloudflare" in title
                or "just a moment" in title
                or "turnstile" in source
                or "cf-challenge" in source
                or "cf-turnstile" in source
                or "verify you are human" in source
            )
            if not looks_blocked:
                return True
            print("[TURNSTILE] Challenge detected. Trying UC auto-click...")
            self._notify("turnstile", "Opening the chapter page and running UC auto-solve.")
            notify_admin(
                "🛡️ <b>Turnstile challenge</b> detected on the worker.\n"
                "Attempting UC auto-click. If it fails, set <code>HEADLESS=0</code> "
                "and solve it in the visible Chrome window."
            )
            self.driver.uc_gui_click_captcha()
            time.sleep(3)
            self._keep_one_tab()
            return True
        except Exception as error:
            if is_dead_session(error):
                raise DeadBrowser(str(error)) from error
            print(f"[TURNSTILE] Auto-click failed: {error}")
            notify_admin(
                f"🛡️ <b>Turnstile auto-click failed</b>: {html.escape(str(error)[:200])}\n"
                "Manual intervention may be required."
            )
            return False

    def wait_for_manual_access(self, novel_url: str, reason: str):
        if HEADLESS:
            self._notify(
                "turnstile_failed",
                "Cloudflare blocked headless Chrome. Turnstile auto-solve is off to save RAM. "
                "Set HEADLESS=0 in .env to use a visible window.",
            )
            raise ManualChallengeRequired(
                "Cloudflare blocked the headless browser. "
                "Turnstile solving is disabled in this mode. "
                "Set HEADLESS=0 in .env, restart, and solve it once in a visible window."
            )
        if self._try_solve_turnstile():
            title = (self.driver.get_title() or "").lower()
            if "cloudflare" not in title and "just a moment" not in title:
                return
        self._notify(
            "turnstile_failed",
            "Auto-solve failed. Complete the checkbox in Chrome, then press Enter in the worker terminal.",
        )
        print("\n" + "=" * 78)
        print("[WTR-LAB MANUAL ACTION REQUIRED]")
        print(reason)
        print("Chrome is open. Log in or click the Turnstile checkbox yourself.")
        print("=" * 78 + "\n")
        self.open(novel_url)
        try:
            self.driver.switch_to.window(self.driver.current_window_handle)
        except Exception:
            pass
        input("Press Enter only after WTR-Lab works normally in Chrome: ")
        self._keep_one_tab()



# ---------------------------------------------------------------------------
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
    global current_login_user
    owned = browser is None
    try:
        with login_lock:
            current_login_user = user_id

        if owned:
            browser = WtrBrowser()

        browser.open("https://wtr-lab.com/en/profile")
        time.sleep(2)

        if not browser.is_login_page():
            send_notice(
                chat_id,
                "✅ Already logged in on this Chrome profile.",
                user_id=user_id,
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
                f"📬 Magic link requested for <code>{html.escape(email)}</code>.\n"
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
            send_notice(
                chat_id,
                "⚠️ Login may have failed — still seeing the login form.",
                user_id=user_id,
            )
            return False

        send_notice(
            chat_id,
            (
                "✅ <b>Login successful!</b>\n"
                "The shared Chrome profile is now authenticated.\n"
                "You and other users can download novels normally."
            ),
            user_id=user_id,
            parse_mode="HTML",
        )
        notify_admin(
            f"✅ User <code>{user_id}</code> successfully logged in via magic link."
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


def is_chapter_locked(status: int, body: Any) -> bool:
    if isinstance(body, dict):
        code = str(body.get("code") or "")
        if code == "CHAPTER_LOCKED":
            return True
        msg = str(body.get("message") or body.get("error") or "").lower()
        if "chapter_locked" in msg or "isn't ai translated" in msg:
            return True
        if "chapter locked" in msg or "ai-lock" in msg:
            return True
    return False


def is_challenge(status: int, body: Any) -> bool:
    if is_chapter_locked(status, body):
        return False

    if status in (401, 403, 429, 503):
        return True

    if isinstance(body, dict):
        if body.get("requireTurnstile") or body.get("timeout"):
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
            raise WtrError("WTR-Lab page did not contain __NEXT_DATA__")

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

    def clear_turnstile(self, novel: NovelInfo, chapter: ChapterInfo, reason: str) -> None:
        if HEADLESS:
            raise ManualChallengeRequired(
                f"Chapter {chapter.order} hit Cloudflare. "
                "Headless mode does not run Turnstile (saves RAM). "
                "Set HEADLESS=0 in .env to solve it in a visible window."
            )
        url = self.chapter_open_url(novel, chapter)
        print(f"[TURNSTILE] {reason}")
        print(f"[TURNSTILE] Opening {url}")
        self.browser._notify("turnstile", f"Chapter {chapter.order}: {reason}")

        self.browser.open(url)
        time.sleep(2)

        try:
            print("[TURNSTILE] Trying UC auto-click...")
            self.browser.driver.uc_gui_click_captcha()
            time.sleep(4)
        except Exception as error:
            if is_dead_session(error):
                raise DeadBrowser(str(error)) from error
            print(f"[TURNSTILE] Auto-click error: {error}")

        self.browser._keep_one_tab()

        title = (self.browser.driver.get_title() or "").lower()
        source = (self.browser.driver.get_page_source() or "").lower()
        still_blocked = (
            "just a moment" in title
            or "cloudflare" in title
            or "turnstile" in source
            or "verify you are human" in source
        )
        if still_blocked:
            self.browser.wait_for_manual_access(
                url,
                (
                    f"Auto Turnstile failed on chapter {chapter.order}.\n"
                    "Solve the checkbox in Chrome, wait until the chapter page "
                    "works, then press Enter so the worker retries."
                ),
            )
    def download_chapter(
        self,
        novel: NovelInfo,
        chapter: ChapterInfo,
        novel_dir: Path,
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
        max_attempts = 6

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
                )
                continue

            # Non-turnstile failure — stop retrying
            break

        if not (
            status == 200
            and isinstance(response, dict)
            and response.get("success")
        ):
            message = (
                response.get("message", "Unknown WTR-Lab response")
                if isinstance(response, dict)
                else str(response)
            )
            raise WtrError(f"Chapter {chapter.order} failed: {message}")

        data = nested(response, "data", "data", default={}) or {}
        body_lines = decrypt_body(data.get("body"))
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
EPUB_BOT_URL = "https://t.me/lightnovel_crawer_bot"
EPUB_BOT_LABEL = "BOT: Novel Downloader"
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
    """Return chapter numbers in [start, end] that are missing or empty on disk."""
    bad: list[int] = []
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
        present = {}
        for row in rows:
            present[int(row["chapter_no"])] = Path(row["xhtml_path"])
    finally:
        conn.close()

    for n in range(start, end + 1):
        path = present.get(n) or (
            LIBRARY_DIR / str(novel_id) / "chapters" / f"{n:05}.xhtml"
        )
        if chapter_cache_is_bad(path):
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
    Find empty/bad cached chapters in range, delete them, re-download immediately.
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

        # Light pacing between repair downloads
        wait = next_chapter_throttle()
        if wait > 0 and i < len(bad_nos):
            time.sleep(min(wait, 5.0))

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
                # Zero-byte file: still include via placeholder so TOC stays continuous.
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
        print(f"[TELEGRAM ERROR] {type(error).__name__}: {error}")
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
        disable_web_page_preview=True,
    )
    return track_and_autodelete(msg, user_id, delay)


def reply_notice(
    message,
    text: str,
    *,
    parse_mode: str | None = None,
    delay: int = DELETE_AFTER_NOTICE,
):
    """reply_to variant for short/task notices (24h)."""
    try:
        msg = bot.reply_to(message, text, parse_mode=parse_mode)
    except Exception as error:
        print(f"[TELEGRAM ERROR] {type(error).__name__}: {error}")
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
        disable_web_page_preview=True,
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
            disable_web_page_preview=True,
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


def send_completed_files(
    task: dict,
    novel: NovelInfo,
    novel_dir: Path,
    epub_path: Path,
    display_title: str,
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
            disable_web_page_preview=True,
        )
        if info_message:
            posted_ids.append(info_message.message_id)

    synopsis = novel.synopsis.strip() or "No synopsis available."
    synopsis_message = telegram_call(
        bot.send_message,
        chat_id,
        f"<b>Synopsis:</b> {html.escape(synopsis[:3900])}",
        parse_mode="HTML",
        disable_web_page_preview=True,
    )
    if synopsis_message:
        posted_ids.append(synopsis_message.message_id)

    document_message = None
    range_match = re.search(r"c(\d+)-(\d+)\s*$", display_title, re.I)
    if range_match:
        file_name = (
            f"{epub_stem(novel.title, int(range_match.group(1)), int(range_match.group(2)))}.epub"
        )
    else:
        file_name = epub_path.name
    for attempt in range(2):
        try:
            with epub_path.open("rb") as document:
                document_message = bot.send_document(
                    chat_id,
                    document,
                    visible_file_name=file_name,
                    caption=display_title[:1024],
                    timeout=180,
                )
            break
        except Exception as error:
            print(f"[EPUB SEND {attempt + 1}/2 ERROR] {error}")
            time.sleep(5)

    if not document_message:
        mark_upload_failed(task["id"])
        send_notice(
            chat_id,
            "⚠️ The EPUB was created and remains safely stored on the local WTR worker, "
            "but Telegram could not accept the upload right now.",
            user_id=task.get("user_id"),
        )
        return False

    posted_ids.append(document_message.message_id)

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


def process_task(task: dict, browser: WtrBrowser):
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
        if kind == "turnstile":
            edit_progress(
                progress,
                (
                    f"📖 {title}\n\n"
                    "🛡️ Attempting to auto-solve Cloudflare Turnstile...\n\n"
                    f"{detail or 'Please leave the Chrome window visible.'}"
                ),
            )
        elif kind == "turnstile_failed":
            edit_progress(
                progress,
                (
                    f"📖 {title}\n\n"
                    "🛡️ Auto-solve failed. Awaiting human intervention.\n\n"
                    "Complete the checkbox in the Chrome window on the worker PC.\n"
                    "Already-downloaded chapters stay cached. After this task ends, "
                    "retry the same link to receive an EPUB of what was saved."
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
            return send_completed_files(task, novel, novel_dir, epub_path, display_title)
        except Exception as pack_error:
            print(f"[PARTIAL EPUB ERROR] {pack_error}")
            return False

    try:
        # Shared Chrome profile must be logged in for chapter API access.
        if not browser.is_logged_in():
            send_notice(
                chat_id,
                (
                    "🔐 <b>Login required</b>\n\n"
                    "The shared Chrome profile is logged out.\n"
                    "Please send your email address now to start magic-link login.\n"
                    "(WTR-Lab accounts are free)"
                ),
                user_id=task.get("user_id"),
                parse_mode="HTML",
            )
            pending_download[task["user_id"]] = {
                "step": "login_email",
                "from_task": task_id,
            }
            requeue_task(task_id)
            return

        client = WtrLabClient(browser)
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
                f"[UNLOCK] AI-Unlock {unlocked_cap}/{total_listed} "
                f"status={novel.status_label}"
            )

        start, end = parse_chapter_range(task.get("chapter_range"), total_listed)
        if end > unlocked_cap:
            end = unlocked_cap

        requested = [
            chapter
            for chapter in novel.chapters
            if start <= chapter.order <= end
        ]

        if not requested:
            raise WtrError(
                f"No unlocked chapters in the requested range. "
                f"AI-Unlock progress is {unlocked_cap}/{total_listed}."
            )

        user_id = int(task["user_id"])
        unlimited_pulls = is_admin(user_id)
        daily_cap = get_chapter_cap()

        # Only fetch chapters that are missing/empty (gap-only).
        to_fetch = [
            ch
            for ch in requested
            if not cache_has_chapter(novel.novel_id, ch.order)
        ]
        cached_count = len(requested) - len(to_fetch)
        completed = cached_count
        last_progress = 0.0
        last_chapter_request = 0.0
        stopped_by_pull_cap = False
        network_fetches_this_run = 0

        unlock_line = (
            f"🔓 AI-Unlock: {unlocked_cap}/{total_listed} · {novel.status_label}"
        )

        if to_fetch:
            sample = [ch.order for ch in to_fetch[:12]]
            more = "…" if len(to_fetch) > 12 else ""
            print(
                f"[RESUME] novel={novel.novel_id} "
                f"cached={cached_count}/{len(requested)} "
                f"missing={len(to_fetch)} {sample}{more} "
                f"pulls_left={pulls_remaining(user_id, novel.novel_id, unlimited_pulls)}"
            )
            edit_progress(
                progress,
                (
                    f"📖 {novel.title}\n\n"
                    f"📥 Resuming local WTR-Lab worker...\n\n"
                    f"📌 Already on server: {cached_count}/{len(requested)}\n"
                    f"▶️ Need from site: {len(to_fetch)} chapter(s)\n"
                    f"🔍 Range: from {start} to {end}\n"
                    f"{unlock_line}"
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
                    f"{unlock_line}"
                ),
            )

        for chapter in to_fetch:
            if is_cancelled(task_id):
                raise TaskCancelled()

            # CHAPTER_CAP = daily first-time network fetches per user/novel.
            if pulls_remaining(user_id, novel.novel_id, unlimited_pulls) <= 0:
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
            try:
                title, xhtml = client.download_chapter(novel, chapter, novel_dir)
            except ChapterLocked:
                last_ok = chapter.order - 1
                if completed > 0 and last_ok >= start:
                    send_partial_if_possible(
                        (
                            f"🔓 Stopped at chapter {chapter.order}: it is still "
                            f"AI-locked on WTR-Lab.\n"
                            f"Sending unlocked chapters {start}–{last_ok} "
                            f"({unlocked_cap}/{total_listed} unlocked).\n\n"
                            "📌 Resend the same link (or /continue) to resume "
                            "from cache. Contact admin if you need another task."
                        )
                    )
                    mark_failed(
                        task_id,
                        f"AI-locked at chapter {chapter.order}",
                    )
                    return
                raise

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
            if (
                now - last_progress >= PROGRESS_UPDATE_SECONDS
                or completed == len(requested)
            ):
                last_progress = now
                touch_progress(task_id)

                percent = completed / len(requested) * 100
                edit_progress(
                    progress,
                    (
                        f"📖 {novel.title}\n\n"
                        f"📥 Downloading with local WTR-Lab worker...\n\n"
                        f"📊 Progress: {percent:.1f}%\n"
                        f"📖 Currently At: Chapter {chapter.order}\n"
                        f"🔍 Range: from {start} to {end}\n"
                        f"{unlock_line}\n\n"
                        f"📌 Status: {completed}/{len(requested)} chapters available"
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
            if have_nos:
                pack_end = max(have_nos)

        edit_progress(
            progress,
            f"📖 {novel.title}\n\n📚 Checking cache / creating EPUB...",
        )

        # Repair empty/corrupt files; does not consume CHAPTER_CAP quota.
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
                    f"⚠️ Daily chapter limit reached for this novel "
                    f"({daily_cap if daily_cap > 0 else 'cap'} from the site / 24h).\n"
                    f"Chapters already on the server are included free.\n"
                    f"Sending what is available: c{pack_start}-{pack_end}.\n\n"
                    "📌 Resend the same link or /continue after the limit resets. "
                    "Contact admin if you need help or another task."
                ),
                user_id=user_id,
            )
            if send_completed_files(
                task, novel, novel_dir, epub_path, display_title
            ):
                mark_failed(
                    task_id,
                    f"Daily chapter limit ({daily_cap}); packed c{pack_start}-{pack_end}",
                )
            else:
                mark_failed(task_id, f"Daily chapter limit ({daily_cap}); upload failed")
            return

        if send_completed_files(
            task,
            novel,
            novel_dir,
            epub_path,
            display_title,
        ):
            mark_done(task_id)

    except LoginRequired:
        send_notice(
            chat_id,
            (
                "🔐 Login required. Please reply with your email address "
                "to start magic-link login."
            ),
            user_id=task.get("user_id"),
        )
        pending_download[task["user_id"]] = {
            "step": "login_email",
            "from_task": task_id,
        }
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
            "📌 Resend the same link (or /continue) to resume. "
            "The worker will reopen Chrome and continue from cache."
        )
        mark_failed(task_id, "Chrome session died")
        raise

    except Exception as error:
        print(f"[WTR TASK ERROR] task={task_id}: {type(error).__name__}: {error}")
        reason = user_facing_error(error)

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

        if not partial_sent:
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
    if user_allowed(uid):
        return False
    reply_notice(
        message,
        "❌ This personal WTR-Lab bot is locked to its owner. "
        "Set ALLOWED_USER_IDS in .env.",
    )
    return True


@bot.message_handler(commands=["start", "help"])
def cmd_start(message):
    if deny_if_needed(message):
        return
    cap = get_chapter_cap()
    limit_line = (
        f"• Daily task limit: {DAILY_TASK_LIMIT}\n"
        if DAILY_TASK_LIMIT > 0
        else "• Daily task limit: none\n"
    )
    cap_line = (
        f"• Up to {cap} chapters/day from the site per novel "
        f"(already on the server are free)\n"
        if cap > 0
        else "• No daily chapter limit from the site\n"
    )
    admin_line = ""
    if is_admin(message.from_user.id):
        admin_line = (
            "\n<b>Admin</b>\n"
            "/logs — task logs with user details\n"
            "/trial — all tasks in table\n"
        )
    try:
        msg = bot.reply_to(
            message,
            "📚 <b>Personal WTR-Lab downloader</b>\n\n"
            "Runs on this server. Tasks are stored in a local SQLite database.\n\n"
            "<b>Commands</b>\n"
            "/download — queue a wtr-lab.com novel\n"
            "/login — magic-link login (if the shared profile is logged out)\n"
            "/queue — your pending/running tasks\n"
            "/mytasks — your recent tasks (any status)\n"
            "/continue — re-queue your latest failed task\n"
            "/cancel — cancel your pending/running tasks\n"
            "/status — worker status\n"
            "/cap — show daily chapter limit\n"
            f"{admin_line}\n"
            f"{limit_line}"
            f"{cap_line}"
            "Each finished task uses your daily task slot. "
            "Chapters already downloaded on the server are reused for free. "
            "Failed/partial: resend the <b>same</b> link or /continue. "
            "Contact admin if you need help or another task.\n\n"
            "Only links from <code>wtr-lab.com</code> are accepted.",
            parse_mode="HTML",
        )
        track_and_autodelete(msg, message.from_user.id, DELETE_AFTER_LIST)
    except Exception as error:
        print(f"[TELEGRAM ERROR] {error}")


@bot.message_handler(commands=["login"])
def cmd_login(message):
    if deny_if_needed(message):
        return
    pending_download[message.from_user.id] = {"step": "login_email"}
    reply_notice(
        message,
        "📧 Send the email address for WTR-Lab magic-link login.\n"
        "(Accounts are free — any email works)",
    )


@bot.message_handler(commands=["status"])
def cmd_status(message):
    if deny_if_needed(message):
        return
    cap = get_chapter_cap()
    try:
        msg = bot.reply_to(
            message,
            (
                "🖥 <b>WTR local worker</b>\n"
                f"SQLite: <code>{html.escape(str(SQLITE_PATH))}</code>\n"
                f"Chapters/day from site: "
                f"{'unlimited' if cap <= 0 else cap}\n"
                f"Daily task limit: "
                f"{'none' if DAILY_TASK_LIMIT <= 0 else DAILY_TASK_LIMIT}\n"
                f"Throttle: {CHAPTER_THROTTLE_MIN:.0f}–{CHAPTER_THROTTLE_MAX:.0f}s"
            ),
            parse_mode="HTML",
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


@bot.message_handler(commands=["mytasks", "tasks"])
def cmd_mytasks(message):
    """Same layout as reference /trial, limited to the calling user."""
    if deny_if_needed(message):
        return
    conn = local_db()
    try:
        rows = conn.execute(
            """
            SELECT id, user_id, username, status, url, chapter_range,
                   novel_title, created_at, completed_at
            FROM tasks
            WHERE user_id = ?
            ORDER BY id ASC
            """,
            (message.from_user.id,),
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


@bot.message_handler(commands=["continue"])
def cmd_continue(message):
    """Re-queue latest failed / upload_failed task for this user."""
    if deny_if_needed(message):
        return
    uid = message.from_user.id
    conn = local_db()
    try:
        active = conn.execute(
            """
            SELECT id FROM tasks
            WHERE user_id = ? AND status IN ('pending', 'running')
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
            SELECT id, url, novel_title, chapter_range, status
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


@bot.message_handler(commands=["logs"])
def cmd_logs(message):
    """Admin: same layout as reference /logs (with user details)."""
    if deny_if_needed(message):
        return
    if not is_admin(message.from_user.id):
        reply_notice(message, "❌ Access Denied.")
        return
    conn = local_db()
    try:
        rows = conn.execute(
            """
            SELECT id, user_id, username, first_name, last_name,
                   status, url, chapter_range,
                   novel_title, created_at, completed_at
            FROM tasks
            ORDER BY id ASC
            """
        ).fetchall()
    finally:
        conn.close()

    if not rows:
        reply_notice(message, "ℹ️ No task logs found.")
        return

    cap = get_chapter_cap()
    text = "📜 Task Logs (with user details):\n\n"
    for t in rows:
        username = f"@{t['username']}" if t["username"] else "N/A"
        first = t["first_name"] or "N/A"
        last = t["last_name"] or "N/A"
        nid_match = re.search(r"/novel/(\d+)", t["url"] or "")
        novel_id = nid_match.group(1) if nid_match else None
        if novel_id:
            used = count_chapter_pulls(int(t["user_id"]), novel_id, hours=24)
            if cap > 0:
                pull_line = f"📥 Pulls 24h: {used}/{cap}\n"
            else:
                pull_line = f"📥 Pulls 24h: {used} (unlimited)\n"
        else:
            pull_line = "📥 Pulls 24h: N/A\n"
        text += (
            f"🆔 Task ID: {t['id']}\n"
            f"👤 User ID: {t['user_id']}\n"
            f"🔗 Username: {username}\n"
            f"📛 Name: {first} {last}\n"
            f"🖥 Server: local\n"
            f"⚙️ Crawler: wtr_local\n"
            f"📌 Status: {t['status']}\n"
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


@bot.message_handler(commands=["cap"])
def cmd_cap(message):
    if deny_if_needed(message):
        return
    cap = get_chapter_cap()
    if cap <= 0:
        text = (
            "Daily chapters from the site: <b>unlimited</b>.\n"
            "Chapters already on the server are always free.\n"
            "Set <code>CHAPTER_CAP=1000</code> in .env to limit, then restart."
        )
    else:
        text = (
            f"Daily chapters from the site: <b>{cap}</b> per novel / 24h.\n"
            "Chapters already on the server are free and do not use that limit.\n"
            "Each finished task still uses your daily task slot.\n"
            "Contact admin if you need help or another task."
        )
    reply_notice(message, text, parse_mode="HTML")


@bot.message_handler(commands=["queue"])
def cmd_queue(message):
    """Same layout as reference /queue — pending tasks."""
    if deny_if_needed(message):
        return
    conn = local_db()
    try:
        # Match reference: global pending queue (this PC is the only worker).
        rows = conn.execute(
            """
            SELECT id, user_id, username, status, url, chapter_range,
                   novel_title, created_at
            FROM tasks
            WHERE status = 'pending'
            ORDER BY id ASC
            """
        ).fetchall()
    finally:
        conn.close()

    if not rows:
        reply_notice(message, "ℹ️ Queue is empty.")
        return

    text = "📋 Pending trial Queue:\n\n"
    for t in rows:
        text += (
            f"🆔 Task ID: {t['id']}\n"
            f"👤 User ID: {t['user_id']}\n"
            f"🖥 Server: local\n"
            f"⚙️ Crawler: wtr_local\n"
            f"🌐 URL: {t['url']}\n"
            f"📖 Range: {t['chapter_range']}\n"
            f"📌 Status: {t['status']}\n"
            f"🕒 Created: {t['created_at']}\n"
            f"-----------------------------\n"
        )
    _send_long_text(message.chat.id, text, user_id=message.from_user.id)


@bot.message_handler(commands=["cancel"])
def cmd_cancel(message):
    if deny_if_needed(message):
        return
    n = cancel_user_tasks(message.from_user.id)
    pending_download.pop(message.from_user.id, None)
    if n:
        reply_notice(message, f"✅ Cancelled {n} task(s).")
    else:
        reply_notice(message, "ℹ️ Nothing to cancel.")


@bot.message_handler(commands=["download"])
def cmd_download(message):
    if deny_if_needed(message):
        return
    pending_download[message.from_user.id] = {"step": "url"}
    reply_notice(
        message,
        "📌 Send a WTR-Lab novel URL.\n"
        "Example:\n"
        "https://wtr-lab.com/en/novel/12345/some-slug",
    )


@bot.message_handler(func=lambda m: True, content_types=["text"])
def on_text(message):
    if deny_if_needed(message):
        return
    if not message.text or message.text.startswith("/"):
        return

    uid = message.from_user.id
    state = pending_download.get(uid)
    if not state:
        return

    text = message.text.strip()

    if state.get("step") == "login_email":
        email = text.strip()
        if "@" not in email or "." not in email.split("@")[-1]:
            reply_notice(message, "Please send a valid email address.")
            return
        state["email"] = email
        state["step"] = "login_waiting_link"
        reply_notice(
            message,
            f"⏳ Starting magic-link login for <code>{html.escape(email)}</code>…",
            parse_mode="HTML",
        )
        threading.Thread(
            target=do_magic_login,
            args=(message.chat.id, uid, email),
            daemon=True,
        ).start()
        return

    if state.get("step") == "login_waiting_link":
        if not text.startswith("http"):
            reply_notice(
                message,
                "Please send the full magic link (it should start with https://).",
            )
            return
        state["magic_url"] = text
        reply_notice(
            message,
            "✅ Magic link received. Completing login on the worker…",
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
            "The local worker will process it and send the EPUB here.",
        )


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
            "The local worker will process it and send the EPUB here.",
            user_id=uid,
        )
    bot.answer_callback_query(call.id)


# ---------------------------------------------------------------------------
# Worker loop + bot polling
# ---------------------------------------------------------------------------

def worker_loop():
    recover_interrupted_tasks()
    print("=" * 78)
    print("WTR-Lab LOCAL worker (SQLite queue) + self-service magic-link login")
    print(f"SQLite: {SQLITE_PATH}")
    print(f"Chrome: {'headless2 low-RAM (no window, no Turnstile)' if HEADLESS else 'headed (visible)'}")
    print(f"Chrome profile: {CHROME_PROFILE_DIR}")
    print(f"Throttle: {CHAPTER_THROTTLE_MIN:.1f}–{CHAPTER_THROTTLE_MAX:.1f}s")
    _cap = get_chapter_cap()
    print(
        f"Daily site chapters (CHAPTER_CAP): "
        f"{'unlimited' if _cap <= 0 else _cap}"
    )
    if HEADLESS:
        print("HEADLESS=1 — no Chrome window. Set HEADLESS=0 in .env for a visible window.")
    else:
        print("Log into WTR-Lab in the Chrome window if needed.")
    print("=" * 78)

    browser = WtrBrowser()
    try:
        browser.open("https://wtr-lab.com/")
        while not stop_event.is_set():
            if not browser.is_alive():
                browser.recreate("session was dead while idle")

            task = claim_task()
            if not task:
                time.sleep(3)
                continue

            print(f"[TASK] #{task['id']}: {task['url']} range={task['chapter_range']}")
            try:
                process_task(task, browser)
            except DeadBrowser as error:
                print(f"[BROWSER] Dead during task #{task['id']}: {error}")
                requeue_task(task["id"])
                browser.recreate(str(error))
            except (
                InvalidSessionIdException,
                NoSuchWindowException,
                WebDriverException,
            ) as error:
                if is_dead_session(error):
                    print(f"[BROWSER] Session error #{task['id']}: {error}")
                    requeue_task(task["id"])
                    browser.recreate(str(error))
                else:
                    mark_failed(task["id"], str(error))
                    print(f"[TASK ERROR] {error}")
    except Exception as error:
        print(f"[WORKER FATAL] {error}")
    finally:
        browser.close()


def main():
    """Full mode: SQLite worker + Telegram polling (users can queue tasks)."""
    setup_local_db()

    worker = threading.Thread(target=worker_loop, name="wtr-worker", daemon=True)
    worker.start()

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
