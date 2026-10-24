# WTR-Lab Crawler (Telegram bot) — **seleniumbase / Linux**

Download novels from **wtr-lab.com** as EPUB files, driven from Telegram.

This **seleniumbase** branch is aimed at **Linux servers** (dnf-based, **x86_64 and aarch64**). It runs a local SQLite queue, caches chapters on disk, drives Chrome via SeleniumBase UC mode, and optionally exposes the browser over **Xvfb + TigerVNC + noVNC + cloudflared** so users can solve Cloudflare Turnstile through a one-time remote link (`REMOTE_SOLVE`).

**Tested on Oracle Linux and AlmaLinux only.** Other dnf-based distros (RHEL, Rocky, CentOS Stream) should work the same way but have not been verified.

For a simple **Windows desktop** install, use the **main** branch instead.

Inspired by [WebToEpub](https://github.com/dteviot/WebToEpub) and [lightnovel-crawler](https://github.com/dipu-bd/lightnovel-crawler).

## Requirements

- **dnf-based Linux** (**x86_64** or **aarch64**)
  - **Tested:** Oracle Linux, AlmaLinux
  - **Untested but expected to work:** RHEL, Rocky Linux, CentOS Stream
- Python 3.10+ (the start script installs a suitable package if needed)
- Google Chrome (installed by the script when missing)
- A Telegram bot token from [@BotFather](https://t.me/BotFather)

macOS and non-dnf distros are not covered by the start script. You can adapt `Start for Linux.sh` or set up a venv by hand (`python -m venv`, `pip install -r requirements.txt`, `python app.py`).

## Quick start

```bash
chmod +x "Start for Linux.sh"
./"Start for Linux.sh"
```

Run as a **normal user, not with sudo**. The script only uses sudo for package installs.

On the first run it will:

- Download project files from GitHub if `app.py` / `requirements.txt` are missing
- Install basic tools, Python, display/VNC packages, Google Chrome
- Create `.venv`, install `requirements.txt` (+ websockify)
- Install noVNC and cloudflared when needed
- Prompt for settings and write `.env` (including `HEADLESS` and `REMOTE_SOLVE`)
- Start Xvfb, x0vncserver, websockify, cron watchdogs
- Start `app.py` in the background (`nohup` → `~/bot.log`)

Later runs skip installs (`data/.setup_done`) and only ensure services + start the app.

Useful commands:

```bash
tail -f ~/bot.log
pkill -9 -f 'app.py|uc_driver|google-chrome|chromedriver'
```

### Non-interactive

```bash
export BOT_TOKEN="123456:ABC..."
export ALLOWED_USER_IDS="111111111"
# optional: HEADLESS=0 REMOTE_SOLVE=1
./"Start for Linux.sh"
```

### Manual start (after setup)

```bash
export DISPLAY=:99   # if using the script's Xvfb
.venv/bin/python app.py
```

## Configuration (`.env`)

Written by the start script. Important keys:

| Variable | Meaning | Typical default |
|----------|---------|-----------------|
| `BOT_TOKEN` | **Required.** BotFather token | — |
| `ALLOWED_USER_IDS` | Lock to numeric Telegram IDs; empty = open | empty |
| `ADMIN_USER_IDS` | Admins (limits + admin commands) | empty |
| `ADMIN_CHAT_ID` | Receives copies of finished books / error shots | empty |
| `OUTPUT_GROUPS` | Extra chats that get finished books | empty |
| `CHAPTER_CAP` | Fresh site chapters / user / rolling 24h (`0` = unlimited) | `0` |
| `DAILY_TASK_LIMIT` | New tasks / user / 24h (`0` = unlimited) | `0` |
| `CHAPTER_THROTTLE_MIN` / `MAX` | Random delay between uncached chapters (seconds) | `18` / `24` |
| `PROGRESS_UPDATE_SECONDS` | Progress message edit interval | `25` |
| `HEADLESS` | `0` = visible Chrome (needed for Turnstile); `1` = no window | `0` |
| `REMOTE_SOLVE` | `1` = share a temporary noVNC link so the **user** can tick Turnstile | `1` |
| `REMOTE_LINK_MAX_MINUTES` | Remote link lifetime; `0` = until `/solved` or task ends | `20` |
| `VNC_PORT` / `NOVNC_PORT` | Local VNC and noVNC ports | `5901` / `6080` |
| `CLOUDFLARED_BIN` | Path to cloudflared binary | `~/bin/cloudflared` |
| `NOVNC_WEB_DIR` | noVNC web root | `~/noVNC` |
| `CHROME_PROFILE_DIR` | Chrome profile | `data/chrome-profile` |
| `ACTIVITY_DEEP` | Deeper activity logging while a remote link is open | `1` |

There is **no** `SERVER_ID` or `DATABASE_URL` — this build is single-host SQLite only (`data/worker.sqlite3`).

## Telegram commands

| Command | Who | What |
|---------|-----|------|
| `/download` | Users | Queue a wtr-lab.com novel |
| `/login` | Users | Magic-link login on the shared Chrome profile |
| `/queue` | Users | Global wait list (no names) |
| `/tasks` | Users | Recent tasks (no names) |
| `/mytasks` | Users | Your tasks only |
| `/continue` | Users / admin | Resume latest failed; admin: `/continue <id>` |
| `/cancel` | Users / admin | Cancel yours; admin: `/cancel <id>` |
| `/status` | Users | Worker status |
| `/cap` | Users | Daily limits |
| `/solved` | Users | Resume after Turnstile is cleared |
| `/help` | Users | Help |
| `/start` | Users / admin | Help; admin: `/start <id>` = next-in-line |
| `/logs` | Admins | Full logs with names |
| `/trial` | Admins | Full task table |
| `/pause` `/stop` | Admins | Pause a task (auto-pending ~2 min) |
| `/set KEY value` | Admins | Edit `.env` (with yes/no confirm); some keys need `/restart` |
| `/restart` | Admins | Relaunch via `Start for Linux.sh` |
| `/banuser` `/unbanuser` | Admins | Ban / unban |
| `/userlimit` | Admins | Per-user chapter cap override |
| `/actions` | Admins | Remote Chrome activity log |

## Cloudflare / Turnstile

Auto-click is **disabled** (it crashed Chrome on headless Linux).

With **`HEADLESS=0`** (default):

1. Captcha blocks the worker; cached chapters are kept.
2. If **`REMOTE_SOLVE=1`**, the bot can open a short-lived public noVNC link (cloudflared quick tunnel) so the **task owner** (or anyone allowed) can tick the box.
3. Send **`/solved`** when done, or wait for the ~10 minute automatic recheck.

With **`HEADLESS=1`** there is no window to solve in — set `HEADLESS=0` and restart.

Do not interact with Chrome while a normal download is running (except during a deliberate solve).

## Limits

- **`CHAPTER_CAP`**: first-time network chapter fetches per user, rolling 24h, all novels. Cached chapters are free. See `/cap`.
- **`DAILY_TASK_LIMIT`**: new tasks per user per 24h. Same-link resume after fail/partial does not consume another slot.
- Admins ignore both. Per-user overrides: `/userlimit`.

## Data on disk

```text
data/
  worker.sqlite3       # task queue + cache index
  chrome-profile/      # shared WTR-Lab login session
  library/             # cached chapter XHTML + built EPUBs
  debug/               # error screenshots
  .setup_done          # start-script marker
```

## Privacy

Do not publish `.env` or `data/`. Each deploy needs its own bot token and profile.

## Troubleshooting

| Problem | What to try |
|---------|-------------|
| `BOT_TOKEN is missing` | Set it in `.env` or export before running the script |
| `dnf required` | This script targets dnf-based distros only |
| Chrome / driver on ARM | Script fetches linux-arm64 chromedriver for UC |
| Captcha with no window | `HEADLESS=0`, restart, use VNC or remote link, then `/solved` |
| Remote link fails | Check cloudflared, websockify (`~/websockify.log`), noVNC dir |
| Partial book | Resend same URL or `/continue` |
| Many Turnstile hits | Keep throttle around 18–24s |
| Config change not applied | Some keys need `/restart` after `/set` |

## Credits

Inspired by [WebToEpub](https://github.com/dteviot/WebToEpub) and [lightnovel-crawler](https://github.com/dipu-bd/lightnovel-crawler).

Built with [SeleniumBase](https://github.com/seleniumbase/SeleniumBase), [ebooklib](https://github.com/aachman98/ebooklib) and [pyTelegramBotAPI](https://github.com/eternnoir/pyTelegramBotAPI).
