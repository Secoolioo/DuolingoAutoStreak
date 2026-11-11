# DuolingoAutoStreak

Keeps your Duolingo streak alive automatically. A small Python/Playwright service on a Linux server does one short lesson in your active course every day at a random time, sends the result to your phone via [ntfy](https://ntfy.sh), and has a minimal web UI for status, history, account selection and settings.

> Duolingo's terms of service do not allow automation. Use at your own risk.

## How it works

- **Schedule:** Every day a new, uniformly random time between 03:00 and 22:00 is picked, to the second and never the same minute as the day before. If a run fails, up to 3 retries follow 15–75 minutes apart, the last one no later than 23:00. Times use the server's time zone.
- **Lesson:** Headless Chromium opens `duolingo.com/lesson` with your saved session. The bot reads each challenge's solution from the data the web app itself loads and answers correctly, so there are no repeat loops and no lost hearts. Listening and speaking challenges are turned off via “Can't listen/speak now”. Between clicks it pauses randomly for 0.8–3.5 s and moves the mouse along slightly varied paths.
- **Verification:** Before and after each run the bot reads the streak the same way the web app does. If today is already done, it skips the run.
- **Fallback:** If the lesson cannot be finished in the browser on the last attempt of the day (or on “Run now”), the bot finishes one of the lessons the web app preloaded through the same endpoint the app uses when you complete a lesson. You can switch this off in the settings.
- **Safety:** A visible captcha stops the bot immediately and pauses it without retrying. If the login page appears, it tells you to log in again. If all attempts fail on two days in a row, it pauses and notifies you. A lesson is capped at 45 steps and 7 minutes. It stops early when the same challenge fails twice or three challenges in a row have no solution. Unsolved challenges are logged with a screenshot (`debug/unsolved-<type>.png`) so new challenge types can be added quickly.

## Files

| File | Purpose |
| --- | --- |
| `bot.py` | Service and commands: `serve`, `run-now [--force] [--trace] [--api-only]`, `status`, `resume`, `plan`, `explore` |
| `web.py` | Web UI (standard library only, port 8095) |
| `login.py` | Log in on your desktop PC; adds the account to the server |
| `install-vm.sh` | One-time server setup (service user `duobot`, venv, Chromium, systemd) |
| `deploy.sh` | Copy the code to the server with rsync and restart the service |
| `ctl.sh` | Run commands on the server as the service user |

Runtime data lives on the server, and none of it belongs in the repo:
- `data/`: one session file per account in `accounts/`, plus state and settings. Only `duobot` can read it.
- `logs/`: rotating, 5 × 1 MB.
- `debug/`: the last 20 error screenshots.

## Setup

1. Copy `deploy.env.example` to `deploy.env` and fill in server, user, target folder and SSH key.
2. Copy the code and install once:
   ```bash
   ./deploy.sh
   ssh root@SERVER /opt/DuolingoAutoStreak/install-vm.sh
   ```
3. Log in on your desktop PC. A normal Chromium window opens, and it closes by itself once you are logged in. The script prints which account you logged in with.
   ```bash
   python3 -m venv .venv && .venv/bin/pip install -r requirements.txt && .venv/bin/playwright install chromium
   .venv/bin/python login.py
   ```
4. Start the service: `systemctl enable --now duolingo-streak-bot`
5. Open `http://SERVER:8095`, select the account, and enter a long random ntfy topic. Then tap “Subscribe on phone” and subscribe to the topic in the ntfy app.

## Usage

- **Web UI** at `http://SERVER:8095`:
  - streak, today's plan and history
  - **Run now**, Pause and Test notification
  - account selection: log in more accounts with `login.py`, then choose one
  - settings for the time window, retries and ntfy

  It has no login, so keep it inside your home network.
- **Logs:** `journalctl -u duolingo-streak-bot -f` or `/opt/DuolingoAutoStreak/logs/bot.log`
- **Restart:** `systemctl restart duolingo-streak-bot`
- **Run right away:** `/opt/DuolingoAutoStreak/ctl.sh run-now`
  - `--force` runs on a day that is already done.
  - `--trace` saves a screenshot per step to `debug/trace/`.
  - `--api-only` skips the browser lesson and uses only the fallback.
- **Session expired:** run `.venv/bin/python login.py` on your PC again. The new session lifts the block automatically.
- **Time zone:** the server's by default. Set `"timezone": "Europe/Berlin"` (for example) in `data/settings.json` and restart to override it.
