# Personal WTR-Lab local worker

Download novels from **wtr-lab.com** on **your own PC**.  
Tasks are stored in a **local SQLite file** on that machine — not in anyone else’s database.

Telegram is only used to:

1. Accept `/download` commands  
2. Show progress  
3. Deliver the finished EPUB  

Inspired by [WebToEpub](https://github.com/dteviot/WebToEpub) and [lightnovel-crawler](https://github.com/dipu-bd/lightnovel-crawler).

## Requirements

- Windows, macOS, or Linux  
- Python 3.10+  
- Google Chrome installed  
- A Telegram account  
- A bot token from [@BotFather](https://t.me/BotFather)

## Easy start (recommended)

### Windows

1. Open the `wtrlab-local-standalone` folder.  
2. **Unblock the scripts** (Windows often blocks files from the internet / zip / chat):  
   - Right-click **`Start for Windows.bat`** → **Properties** → check **Unblock** → **OK**  
   - Right-click **`start-windows.ps1`** → **Properties** → check **Unblock** → **OK**  
   - If **Unblock** is missing, the file is already allowed.  
   - Optional (same folder in PowerShell):  
     `Unblock-File ".\Start for Windows.bat"; Unblock-File ".\start-windows.ps1"`  
3. **Double-click `Start for Windows.bat`**.  
   - Run as your **normal user** (admin is not required and can cause Chrome profile permission issues).  
   - If antivirus still blocks it, add this folder to AV exclusions, or install manually with `python -m venv` / `pip` / `python app.py` (see troubleshooting).

**First run** will:

- Create a Python virtual environment (`.venv`)  
- Install dependencies from `requirements.txt`  
- Prompt for config (writes `.env` for you — no manual file editing)  
  - `BOT_TOKEN` — **required** (from [@BotFather](https://t.me/BotFather))  
  - `ALLOWED_USER_IDS` — optional; **press Enter** to leave empty (open bot)  
  - `CHAPTER_CAP` — default unlimited (`0`); press Enter to keep it  
  - throttle min/max — defaults `10` / `18`  
- Check for Google Chrome; offer install via `winget` if missing  
- Ask whether to close other Chrome processes  
- Start `app.py`  

**Later runs:** setup is skipped (marker in `data/.setup_done`). The script only starts the worker.

To force full setup again, delete `data/.setup_done` and/or `.env`, then run `Start for Windows.bat` again.

### macOS / Linux

```bash
chmod +x "Start for Linux.sh"
./"Start for Linux.sh"
```

Same guided setup, then starts the app.

### Optional: auto-download from GitHub

If you publish this repo, set the zip URL inside `start-windows.ps1` / `Start for Linux.sh` (`$GithubZipUrl` / `GITHUB_ZIP_URL`).  
If `app.py` is missing, the script can download the project automatically. Leave the URL empty if you always ship the full folder.

### Config reference (written by the script into `.env`)

| Variable | Meaning |
|----------|---------|
| `BOT_TOKEN` | **Required.** Token from BotFather |
| `ALLOWED_USER_IDS` | Optional lock to numeric Telegram user ids. **Empty = open**. Formats: `123` or `123,456` or `123, 456` |
| `ADMIN_USER_IDS` | Admins (same ID formats). **Immune to `CHAPTER_CAP` and `DAILY_TASK_LIMIT`**. Not the same as `ADMIN_CHAT_ID` |
| `ADMIN_CHAT_ID` | Optional chat that only **receives copies** of finished books (no extra privileges) |
| `CHAPTER_CAP` | **`0` or empty = unlimited**. Set e.g. `1000` to clamp each request's chapter span |
| `DAILY_TASK_LIMIT` | Max **new** novel tasks per user per 24h (`0` = unlimited). Same-link resume after fail/partial does not use another slot |
| `CHAPTER_THROTTLE_MIN` / `MAX` | Random delay between chapters (see **Chapter throttle**) |
| `OUTPUT_GROUPS` | Optional chats to copy finished messages to |
| `CHROME_PROFILE_DIR` | Where Chrome stores the WTR-Lab login |

### `ALLOWED_USER_IDS` — can be left empty

- **Leave it empty** if you do not know how to get a numeric user id, or if you are fine with anyone who knows your bot’s username being able to queue downloads on **your** PC.
- Nobody can use the bot unless they know it exists and can message it (Telegram bots are not listed in a public directory by default).
- If you want a hard lock later, put your numeric id(s) there (comma-separated). You can get an id from [@userinfobot](https://t.me/userinfobot).

Empty list = open to whoever can reach the bot. That is intentional and fine for a private personal bot.

## Before you run — Chrome

1. **Fully quit Google Chrome** (and any Chrome background processes) before starting `app.py`.  
   If Chrome is already open with your normal profile, the worker’s dedicated profile window can fail to start or behave oddly.
2. The app opens its **own** Chrome window using `data/chrome-profile/`.  
   Log into [wtr-lab.com](https://wtr-lab.com) **once** in that window. The login is saved in that profile folder so you usually will not need to log in again.
3. **Leave that Chrome window open** while the worker runs. Do not close it manually unless you are shutting down the app.
4. Prefer not to use that same window for casual browsing while a download is in progress.

## Run

Preferred: **`Start for Windows.bat`** or **`Start for Linux.sh`**.

Or after setup:

```bash
# Windows
.venv\Scripts\python app.py

# macOS / Linux
.venv/bin/python app.py
```

What happens:

1. A dedicated **Chrome** window opens. Log into WTR-Lab there if needed.  
2. Telegram polling + background worker start.  
3. Message your bot on Telegram (`/download`).

Stop with `Ctrl+C` when **no** Turnstile solve is in progress (see below).

## Bot commands

| Command | Action |
|---------|--------|
| `/start` | Help |
| `/download` | Queue a novel (URL → chapter range) |
| `/queue` | List your pending/running tasks |
| `/cancel` | Cancel your pending/running tasks |
| `/cap` | Show chapter cap |

Only **wtr-lab.com** URLs are accepted.

## Resuming failed or partial downloads

Chapter HTML is **cached on disk**. If a job stops early (Turnstile, network error, you cancelled, PC sleep, etc.):

- Queue the **same novel URL** again (same or overlapping chapter range).
- The worker **skips chapters already cached** and continues from where it left off.
- You get an EPUB of whatever was available; a later run can fill in the rest and produce a fuller book.

You do not need to delete the library folder to resume.

## Chapter throttle (speed vs Cloudflare)

Between uncached chapters the worker waits a **random** delay so traffic looks less like a fixed bot.

| Setting | Typical effect |
|---------|----------------|
| **`CHAPTER_THROTTLE_MIN=10`** and **`CHAPTER_THROTTLE_MAX=18`** (defaults) | Safe range. Roughly a **~95% chance of never triggering** a Cloudflare challenge on a normal long download. |
| Lower values (e.g. 3–8s) | Faster downloads, **more** Turnstile challenges. |
| Higher values (e.g. 15–30s) | Slower, even quieter on Cloudflare. |

The bot will **try to auto-solve** Turnstile when it appears (see next section). Auto-solve works most of the time, but it can still fail — slower throttle means fewer solves needed.

## Cloudflare / Turnstile

The worker uses SeleniumBase **UC mode** and will **attempt to solve Turnstile automatically** when a challenge is detected (on page load and when a chapter API call is blocked).

- In practice auto-solve works about **~90% of the time**.
- If it fails, a **human** must complete the checkbox in the Chrome window. The terminal may ask you to press Enter after the site works again.

**While it is solving a challenge:**

- **Do not move the mouse or click** in that Chrome window for about a minute.
- **Do not** press Ctrl+C / stop the script mid-solve.
- **Do not** minimize in a way that breaks UC input on some systems; leave the window alone and wait.

If a run still dies on a challenge, fix it in Chrome if needed, then **send the same link again** — download continues from the last cached chapter.

## Data on disk

Everything stays under this folder:

```text
data/
  worker.sqlite3      # task queue + chapter cache index
  chrome-profile/     # browser login session (keep this)
  library/<novel_id>/ # cached XHTML, images, EPUBs
```

Keep `chrome-profile/` if you want to stay logged in between runs. Deleting it forces a fresh Chrome profile (you will need to log into WTR-Lab again).

## Limits

- **Chapter cap** (`CHAPTER_CAP`): default **`0` = unlimited**. Set e.g. `1000` to clamp each request (All → `1–1000`, long custom ranges trimmed). Admins in `ADMIN_USER_IDS` ignore this.  
- **Daily task limit** (`DAILY_TASK_LIMIT`): optional; `0` disables it. Counts **new** novels. If a download **fails or stops early**, re-sending the **same link** resumes from **disk cache** and does **not** use another daily slot. Admins ignore this.  
- **EPUB contents**: every chapter already cached for that novel inside your requested range is packed into the EPUB (resume + new chapters together). A partial run still sends what is on disk.  
- **`ADMIN_CHAT_ID`**: only a destination for copies — it does **not** grant limit immunity. Use `ADMIN_USER_IDS` for that.  
- This package does **not** connect to any shared Postgres or third-party queue.

## Privacy / sharing

When you share this project:

- Do **not** include your real `.env` or `data/` folder.  
- Recipients create **their own** BotFather token and run on **their** laptop.  
- Each install is isolated: separate SQLite, separate Chrome profile, separate bot.

## Troubleshooting

| Problem | What to try |
|---------|-------------|
| `BOT_TOKEN is missing` | Create `.env` from `.env.example` |
| `telebot` / import errors | `pip install -r requirements.txt` inside the venv |
| Chrome won’t open | Fully quit Chrome first; install Google Chrome; if needed delete `data/chrome-profile` and retry |
| Always “Not allowed” | Clear `ALLOWED_USER_IDS` or put your numeric id in it |
| Partial EPUB only | Turnstile, AI-lock, or network stop; **re-queue the same URL** to continue from cache |
| Many Turnstile prompts | Raise throttle (e.g. min 12 / max 20); don’t lower min/max too aggressively |
| Auto-solve failed | Leave the mouse alone, complete the checkbox in Chrome, press Enter in the terminal if asked |
| Telegram slow / rate limits | Wait and retry; EPUB files stay under `data/library/` |

## Credits

Inspired by:

- [WebToEpub](https://github.com/dteviot/WebToEpub) — browser-side novel → EPUB workflow and WTR-Lab reading patterns  
- [lightnovel-crawler](https://github.com/dipu-bd/lightnovel-crawler) — multi-source light novel crawling ecosystem  

Also uses [SeleniumBase](https://github.com/seleniumbase/SeleniumBase), [ebooklib](https://github.com/aachman98/ebooklib), and [pyTelegramBotAPI](https://github.com/eternnoir/pyTelegramBotAPI).

Built for personal self-hosting of WTR-Lab downloads on your own machine.
