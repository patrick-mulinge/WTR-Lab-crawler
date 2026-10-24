# WTR-Lab Crawler (Telegram bot) — **main / Windows**

Download novels from **wtr-lab.com** as EPUB files, driven from Telegram.

This **main** branch is aimed at **Windows 10/11** desktop use. Tasks live in a local SQLite file, chapters are cached on disk, and a dedicated Chrome window (SeleniumBase UC mode) does the fetching. Telegram is used to queue downloads, show progress, and deliver the finished EPUB.

For a Linux VPS setup (Xvfb, VNC, noVNC, cloudflared, remote Turnstile links, admin `/set` / `/restart`, and aarch64), use the **`seleniumbase`** branch instead.

Inspired by [WebToEpub](https://github.com/dteviot/WebToEpub) and [lightnovel-crawler](https://github.com/dipu-bd/lightnovel-crawler).

## Requirements

- **Windows 10/11** (primary)
- Python 3.10+ (the Windows start script installs 3.12 if needed)
- Google Chrome (the start script installs it if missing)
- A Telegram bot token from [@BotFather](https://t.me/BotFather)

A `Start for Linux.sh` is still in the tree for dnf-based hosts, but Linux-focused features and docs live on the **seleniumbase** branch.

## Quick start (Windows)

1. Put the project folder on a local disk (Desktop or Downloads), not on an external drive.
2. If Windows blocked the script: right-click **`Start for Windows.bat`** → **Properties** → tick **Unblock** → **OK**.
3. Double-click **`Start for Windows.bat`**. Run as a normal user, not as administrator (admin can break the Chrome profile).

On the first run the script will:

- Download project files from GitHub if `app.py` or `requirements.txt` are missing
- Install Python 3.12 if no Python 3.10+ is found
- Install Chrome if it is missing
- Create `.venv` and install `requirements.txt`
- Prompt for settings and write `.env`
- Optionally close other Chrome processes, then start `app.py`

Later runs skip setup (`data/.setup_done`) and only start the app. To force setup again, delete `data/.setup_done` and/or `.env`.

### Worker-only (Windows)

`Worker for Windows.bat` (or `.venv\Scripts\python worker.py`) processes pending SQLite tasks **without** Telegram polling. Run `Start for Windows.bat` once first so setup and `.env` exist. `BOT_TOKEN` is still required to send progress and EPUBs.

### Manual start

```bat
.venv\Scripts\python app.py
```

## Configuration (`.env`)

Written by the start script. See `.env.example`.

| Variable | Meaning |
|----------|---------|
| `BOT_TOKEN` | **Required.** Token from BotFather |
| `ALLOWED_USER_IDS` | Optional lock to numeric Telegram user IDs. Empty = anyone who can message the bot |
| `ADMIN_USER_IDS` | Admins: ignore chapter/task limits; can use `/logs` and `/trial` |
| `ADMIN_CHAT_ID` | Optional chat that receives **copies** of finished books (not a privilege by itself) |
| `OUTPUT_GROUPS` | Optional extra chats that also get finished books |
| `CHAPTER_CAP` | Max **fresh** site chapters per user per rolling 24h. `0` = unlimited. Cached chapters are free |
| `DAILY_TASK_LIMIT` | Max **new** novel tasks per user per 24h (`0` = unlimited). Same-link resume does not take a new slot |
| `CHAPTER_THROTTLE_MIN` / `MAX` | Random delay (seconds) between uncached chapter fetches |
| `CHAPTER_THROTTLE_SECONDS` | Optional single value; ±25% jitter applied |
| `PROGRESS_UPDATE_SECONDS` | How often the progress message is edited |
| `HEADLESS` | `0` = visible Chrome (default, needed for Turnstile). `1` = no window |
| `CHROME_PROFILE_DIR` | Chrome profile path (default `data/chrome-profile`) |

## Telegram commands

| Command | Who | What |
|---------|-----|------|
| `/download` | Users | Queue a wtr-lab.com novel URL |
| `/login` | Users | Magic-link login for the shared Chrome profile |
| `/queue` | Users | Global wait list (no usernames) |
| `/tasks` | Users | Recent tasks (no usernames) |
| `/mytasks` | Users | Your tasks only |
| `/continue` | Users | Re-queue your latest failed task |
| `/cancel` | Users | Cancel your pending/running tasks |
| `/status` | Users | Worker status |
| `/cap` | Users | Show your daily limits |
| `/solved` | Users | Resume after a Cloudflare Turnstile solve |
| `/help`, `/start` | Users | Help text |
| `/logs` | Admins | Full task log with names |
| `/trial` | Admins | Full task table |

## Cloudflare / Turnstile

The bot does **not** auto-click Turnstile. When a challenge appears:

1. Keep the Chrome window visible (`HEADLESS=0`).
2. Complete the checkbox in Chrome.
3. Send **`/solved`** in Telegram (or wait for the ~10 minute automatic recheck).

## Limits

- **`CHAPTER_CAP`**: fresh network chapter pulls per user, rolling 24h, across all novels. Check with `/cap`.
- **`DAILY_TASK_LIMIT`**: new tasks per user per 24h (failed ones count; same-link resume does not).
- Admins in `ADMIN_USER_IDS` ignore both limits.
- No shared database or third-party queue — everything is local SQLite + disk cache.

## Privacy

Do **not** share your real `.env` or `data/` folder. Each install needs its own BotFather token, SQLite DB, and Chrome profile. `.gitignore` already excludes these.

## Troubleshooting

| Problem | What to try |
|---------|-------------|
| `BOT_TOKEN is missing` | Create `.env` from `.env.example` and set the token |
| Import / `telebot` errors | `pip install -r requirements.txt` inside `.venv` |
| Chrome will not open | Quit all Chrome processes; last resort: delete `data/chrome-profile` and `/login` again |
| Always "Not allowed" | Clear `ALLOWED_USER_IDS` or add your numeric ID |
| Partial EPUB | Resend the same URL or `/continue` to resume from cache |
| Stuck on captcha | Solve in Chrome, send `/solved`, or wait ~10 minutes |
| Many Turnstile prompts | Raise throttle (e.g. min 18 / max 24) |

## Credits

Inspired by [WebToEpub](https://github.com/dteviot/WebToEpub) and [lightnovel-crawler](https://github.com/dipu-bd/lightnovel-crawler).

Built with [SeleniumBase](https://github.com/seleniumbase/SeleniumBase), [ebooklib](https://github.com/aachman98/ebooklib) and [pyTelegramBotAPI](https://github.com/eternnoir/pyTelegramBotAPI).
