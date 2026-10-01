# WTR-Lab Crawler (Telegram bot)

Download novels from **wtr-lab.com** as EPUB files, driven from Telegram.

The bot runs on **your own machine or VPS**. Tasks live in a local SQLite file, chapters are cached on disk, and a dedicated Chrome window (SeleniumBase UC mode) does the actual fetching. Telegram is used to:

1. Accept `/download` and the other commands
2. Show progress
3. Deliver the finished EPUB

Inspired by [WebToEpub](https://github.com/dteviot/WebToEpub) and [lightnovel-crawler](https://github.com/dipu-bd/lightnovel-crawler).

## Requirements

- Windows 10/11, or a **dnf-based Linux** (Oracle Linux, RHEL, Rocky, Alma, CentOS Stream), x86_64 only
- Python 3.10+ (the Windows script installs 3.12 for you if it is missing)
- Google Chrome (both start scripts install it if it is missing)
- A Telegram bot token from [@BotFather](https://t.me/BotFather)

macOS and other Linux distributions are not covered by the start scripts. You can still set things up by hand with `python -m venv`, `pip install -r requirements.txt` and `python app.py`.

## Quick start

### Windows

1. Put the project folder on your local disk (Desktop or Downloads), not on an external drive.
2. Unblock the script if Windows flagged it: right-click **`Start for Windows.bat`** → **Properties** → tick **Unblock** → **OK**. If there is no Unblock box, it is already allowed.
3. Double-click **`Start for Windows.bat`**. Run it as your normal user, not as administrator, because admin can cause Chrome profile permission problems.

The script is self-contained. On the first run it will:

- Download the project files from GitHub if `app.py` or `requirements.txt` are missing
- Install Python 3.12 if no Python 3.10+ is found
- Install Chrome (official Enterprise MSI) if it is missing
- Create `.venv` and install `requirements.txt`
- Ask for your settings and write `.env` for you (see the table below)
- Ask whether to close other Chrome processes, then start `app.py`

Later runs skip setup (marker file `data/.setup_done`) and just start the app. To force setup again, delete `data/.setup_done` and/or `.env`.

### Linux (Oracle Linux / RHEL family)

```bash
chmod +x "Start for Linux.sh"
./"Start for Linux.sh"
```

Run it as a **normal user, not with sudo**. It asks for sudo only when installing packages. On the first run it installs the basics, Python, Google Chrome, creates `.venv`, installs dependencies, and writes `.env`.

Unlike Windows, the Linux script:

- Stops any previous `app.py` / Chrome / chromedriver processes before starting
- Starts the bot **in the background** with `nohup` and writes logs to `~/bot.log`
- Defaults to `HEADLESS=1` and a 18–24 s chapter delay

Useful commands:

```bash
tail -f ~/bot.log                                             # follow the log
pkill -9 -f 'app.py|uc_driver|google-chrome|chrome|chromedriver'   # stop everything
```

**Non-interactive setup (no TTY):** export the values first, or pre-create `.env`:

```bash
export BOT_TOKEN="123456:ABC..."
export ALLOWED_USER_IDS="111111111"
export HEADLESS=1
./"Start for Linux.sh"
```

### Running by hand

```bash
# Windows
.venv\Scripts\python app.py

# Linux
.venv/bin/python app.py
```

## Configuration (`.env`)

The start scripts write this file for you. `.env.example` shows every option.

| Variable | Meaning |
|----------|---------|
| `BOT_TOKEN` | **Required.** Token from BotFather |
| `ALLOWED_USER_IDS` | Optional lock to numeric Telegram user IDs (`123` or `123,456`). **Empty = anyone who can message the bot** |
| `ADMIN_USER_IDS` | Admins. Immune to `CHAPTER_CAP` and `DAILY_TASK_LIMIT`, and can use `/logs` and `/trial` |
| `ADMIN_CHAT_ID` | Optional chat that only **receives copies** of finished books. It gives no extra privileges |
| `OUTPUT_GROUPS` | Optional chats (comma-separated) that also receive copies of finished books |
| `CHAPTER_CAP` | Max **fresh chapters per user per rolling 24 h** pulled from the site. `0` or empty = unlimited. Chapters already cached on disk are free |
| `DAILY_TASK_LIMIT` | Max **new** novel tasks per user per 24 h (`0` = unlimited). Resending the same link after a failed or partial run is a resume and does not need a new slot |
| `CHAPTER_THROTTLE_MIN` / `MAX` | Random delay in seconds between uncached chapters (see below) |
| `CHAPTER_THROTTLE_SECONDS` | Alternative single value; ±25% jitter is applied automatically |
| `PROGRESS_UPDATE_SECONDS` | How often the Telegram progress message is edited (default 25) |
| `HEADLESS` | `1` = no Chrome window. `0` = visible window (**default if unset**). See the Cloudflare section |
| `CHROME_PROFILE_DIR` | Where the Chrome profile and WTR-Lab login are stored (default `data/chrome-profile`) |

Defaults differ slightly by entry point. If `CHAPTER_THROTTLE_MIN/MAX` are not set at all, `app.py` uses 18 / 24. The Windows script writes 10 / 18 into `.env`, and the Linux script writes 18 / 24.

### About `ALLOWED_USER_IDS`

Leave it empty if you are fine with anyone who finds your bot being able to queue downloads on your machine. For a hard lock, put your numeric ID(s) there. You can get an ID from [@userinfobot](https://t.me/userinfobot).

## Before the first download: Chrome and login

1. **Fully quit Google Chrome** before starting the bot. The Linux script does this for you; on Windows the script offers to close Chrome processes.
2. The bot opens its **own** Chrome using `data/chrome-profile/`. The WTR-Lab login is stored in that profile, so you normally log in only once.
3. Leave that Chrome window alone while the bot runs, and avoid browsing in it during a download.
4. If the shared profile gets logged out, send **`/login`** to the bot. It starts a magic-link login by email (WTR-Lab accounts are free, any email works).

## Bot commands

| Command | Action |
|---------|--------|
| `/start`, `/help` | Show help and your current limits |
| `/download` | Queue a novel: send the URL, then choose all chapters or a custom range |
| `/login` | Magic-link login if the Chrome profile is logged out |
| `/queue` | Your pending and running tasks |
| `/mytasks` (or `/tasks`) | Your recent tasks, any status |
| `/continue` | Re-queue your latest failed or upload-failed task |
| `/cancel` | Cancel your pending and running tasks |
| `/status` | Worker status |
| `/cap` | How many fresh chapters and tasks you have left right now |
| `/solved` | Tell the bot you cleared a Cloudflare captcha (not shown in `/help`) |
| `/logs` | **Admin.** Task log with user details |
| `/trial` | **Admin.** Full task table |

Only **wtr-lab.com** links are accepted. The worker handles **one task at a time**, so other tasks wait in the queue.

## Resuming failed or partial downloads

Chapter HTML is cached on disk. If a job stops early (Cloudflare, network error, cancel, PC sleep and so on):

- Send the **same novel URL** again, or use `/continue`.
- Chapters already cached are skipped, and the worker continues from where it stopped.
- The EPUB contains every cached chapter in your requested range, so a later run produces a fuller book.

You do not need to delete anything in the library folder to resume.

## Chapter throttle (speed vs Cloudflare)

Between uncached chapters the worker waits a random delay between `CHAPTER_THROTTLE_MIN` and `CHAPTER_THROTTLE_MAX`.

| Setting | Effect |
|---------|--------|
| 18–24 s (`app.py` default) | Slow and quiet, the least likely to trigger Cloudflare, currently triggers cloudflare at most 2 times in 24 hours |
| 10–18 s (Windows script default) | Faster, a bit more likely to hit a challenge |
| Under about 8 s | Fast, but expect more Turnstile challenges |

Temporary server errors are retried automatically. Origin timeouts (522) and HTTP 503 get up to 8 attempts, waiting 2, then 5, then 10 minutes between tries, before that chapter is failed. Progress is saved, so you can resume.

## Cloudflare / Turnstile

The bot **no longer tries to auto-click Turnstile**. That approach crashed Chrome on Linux servers, so a human clears the challenge instead.

When a captcha appears:

1. The bot stops on the blocked page, keeps already-downloaded chapters, and notifies the admin and the user in Telegram.
2. Open the Chrome window (on a server, over VNC) and complete the checkbox.
3. Send **`/solved`** in Telegram. The bot reloads the page and resumes if the challenge is gone.
4. If nobody sends `/solved`, the bot rechecks by itself every **10 minutes**.

This needs a visible window, so on a server use `HEADLESS=0` with a VNC desktop. With `HEADLESS=1` there is no window to solve in: the task fails with a message telling you to switch to `HEADLESS=0`. The repo includes `start-vnc-bot-display.sh` on some deployments for starting a virtual display, but it is not part of the repository itself.

Tips: do not click or move the mouse in Chrome while a download is running, and avoid pressing Ctrl+C in the middle of a solve.

## Worker-only mode

`Worker for Windows.bat` (or `.venv\Scripts\python worker.py`) runs the same Chrome and SQLite pipeline **without Telegram polling**. It only processes tasks already stored as pending in `data/worker.sqlite3`; users cannot queue new ones while it is the only thing running. `BOT_TOKEN` is still needed to send progress and finished EPUBs. It does not run first-time setup, so run `Start for Windows.bat` once first.

## Data on disk

```text
data/
  worker.sqlite3          # task queue and chapter cache index
  chrome-profile/         # browser login session (keep this)
  library/<novel_id>/
    chapters/             # cached chapter XHTML
    images/
    cover.jpg
    metadata.json
    artifacts/            # EPUB is built here temporarily
```

Housekeeping the bot does by itself:

- **EPUBs are deleted right after they are delivered.** The `artifacts` folder is only a build area. If the upload to Telegram fails, the EPUB is kept and the task is marked `upload_failed`, so `/continue` can retry it.
- **Finished task rows** (done, failed, cancelled, upload_failed) older than 7 days are purged from the database, at startup and every 24 hours. Cached chapters are not touched.
- **Bot messages clean up after themselves**: notices are removed after about 24 hours and list-style replies after about an hour.

Keep `chrome-profile/` if you want to stay logged in. Deleting it forces a fresh profile and a new login.

## Limits

- **`CHAPTER_CAP`** counts fresh chapters pulled from the site per user in a rolling 24 hours, across all novels. Each pulled chapter frees up again 24 hours after it was pulled, so the number refills gradually. Check yours with `/cap`.
- **`DAILY_TASK_LIMIT`** counts new tasks per user per 24 hours, including failed ones. Resuming the same link does not take another slot.
- Admins in `ADMIN_USER_IDS` ignore both limits.
- **`ADMIN_CHAT_ID`** only receives copies. It is not a privilege setting.
- This project does not connect to any shared database or third-party queue.

## Privacy and sharing

If you share this project, do **not** include your real `.env` or the `data/` folder. Everyone should create their own BotFather token and run their own copy. Each install has its own SQLite database, Chrome profile and bot. The `.gitignore` already excludes these.

## Troubleshooting

| Problem | What to try |
|---------|-------------|
| `BOT_TOKEN is missing` | Create `.env` from `.env.example` and set the token |
| `telebot` / import errors | Run `pip install -r requirements.txt` inside the `.venv` |
| Chrome will not open | Fully quit Chrome first, make sure Google Chrome is installed, and as a last resort delete `data/chrome-profile` and log in again |
| Always "Not allowed" | Clear `ALLOWED_USER_IDS` or add your numeric ID |
| Partial EPUB only | Cloudflare, an AI-unlock limit, or a network stop. Send the same URL or `/continue` to resume from cache |
| "Cloudflare blocked headless Chrome" | Set `HEADLESS=0` in `.env`, restart, solve the captcha over VNC, then send `/solved` |
| Bot seems stuck on a captcha | Clear it in Chrome and send `/solved`, or wait up to 10 minutes for the automatic recheck |
| Many Turnstile prompts | Raise the throttle (for example min 18 / max 24) |
| Telegram is slow or rate-limited | Wait and retry. If an upload failed, the EPUB stays in `artifacts` until you `/continue` |
| Linux script says dnf is required | The script supports dnf-based distributions only. On others, install Python, Chrome and the requirements manually |

## Credits

Inspired by [WebToEpub](https://github.com/dteviot/WebToEpub) and [lightnovel-crawler](https://github.com/dipu-bd/lightnovel-crawler).

Built with [SeleniumBase](https://github.com/seleniumbase/SeleniumBase), [ebooklib](https://github.com/aachman98/ebooklib) and [pyTelegramBotAPI](https://github.com/eternnoir/pyTelegramBotAPI).

Made for personal self-hosting of WTR-Lab downloads.
