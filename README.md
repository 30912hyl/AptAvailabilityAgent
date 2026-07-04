# The Martin — 1BR Availability Watcher

Checks https://livethemartin.com/floorplans/ every ~20 minutes and sends you a
Telegram message the moment a new 1-bedroom listing appears.

## How it works

The Martin's website is built on Yardi RentCafe: the floorplans page loads unit
availability via JavaScript API calls. This watcher opens the page in a headless
Chromium browser (Playwright), intercepts those JSON API responses, extracts all
1-bedroom records, and diffs them against the previous run (`state.json`).
New listing → Telegram alert.

## Setup (one time, ~10 minutes)

### 1. Create a Telegram bot
1. In Telegram, message **@BotFather** → send `/newbot` → follow prompts.
   Copy the **bot token** it gives you.
2. Send any message to your new bot (this opens the chat).
3. Visit `https://api.telegram.org/bot<YOUR_TOKEN>/getUpdates` in a browser and
   find `"chat":{"id": <number>}` — that number is your **chat ID**.

### 2. Create the GitHub repo
1. Create a new **private** GitHub repository and push these files to it
   (`watcher.py`, `README.md`, `.github/workflows/watch.yml`).
2. In the repo: **Settings → Secrets and variables → Actions → New repository secret**:
   - `TELEGRAM_BOT_TOKEN` = your bot token
   - `TELEGRAM_CHAT_ID` = your chat ID

### 3. Test it
- Go to the **Actions** tab → "Watch The Martin 1BR listings" → **Run workflow**.
- First run establishes a baseline and sends you a "watcher is live" message
  listing current 1BR availability.
- After that, it runs every 20 minutes automatically and only messages you when
  something **new** appears.

## Testing locally (recommended once)

```bash
pip install playwright
playwright install chromium
python watcher.py --debug
```

`--debug` writes `debug_capture.json` (every API response the page made) and
`debug_rendered.txt` (rendered page text). If the parsed listings look wrong or
empty while the website clearly shows 1BR units, open `debug_capture.json` to
see the real data shape — the parser in `parse_one_bedroom_listings()` is easy
to adjust.

## Tuning

- **Check frequency**: edit the cron line in `.github/workflows/watch.yml`.
  GitHub Actions minimum is 5 minutes; scheduled runs can be delayed a few
  minutes during busy periods.
- **Other bedroom counts**: change `beds_n != 1` in `watcher.py`.
- **Email instead of Telegram**: swap `send_telegram()` for an SMTP call or a
  service like Resend — everything else stays the same.

## Notes

- `state.json` is committed back to the repo by the workflow so state persists
  between runs.
- Be a polite scraper: 20-minute intervals are gentle. Don't crank it to
  every minute.
