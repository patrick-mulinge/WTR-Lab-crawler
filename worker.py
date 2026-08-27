"""
WTR-Lab local worker only — no Telegram polling.

Runs the same Chrome + SQLite pipeline as app.py, but does not accept
new /download commands. Only tasks already stored as pending (or recovered
from running) in data/worker.sqlite3 are processed.

Usage:
  .venv\\Scripts\\python.exe worker.py
  or double-click Worker for Windows.bat

Requires the same .env and setup as app.py (BOT_TOKEN still needed to
send progress / finished EPUBs for tasks already in the queue).
"""

from __future__ import annotations

import app as wtr_app


if __name__ == "__main__":
    wtr_app.worker_main()
