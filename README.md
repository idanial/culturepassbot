# Culture Pass Monitor

This repository monitors the NYC Culture Pass listing page and sends a Telegram message when attractions change.

## What It Detects

- New attractions added
- Attractions removed
- Name changes for existing attractions (rename detection)

## How It Works

1. A Python script logs into `https://culturepassnyc.quipugroup.net/?NYPL`.
2. It extracts the current attraction list from the rendered page.
3. It compares the results to `data/attractions_snapshot.json`.
4. If changes exist, it sends a Telegram message.
5. The new snapshot is saved and committed by GitHub Actions.

## Required GitHub Secrets

Set these in repository settings:

- `CULTUREPASS_USERNAME`
- `CULTUREPASS_PASSWORD`
- `TELEGRAM_BOT_TOKEN`
- `TELEGRAM_CHAT_ID`

## Telegram Chat ID

If you do not know your `TELEGRAM_CHAT_ID`:

1. Send any message to your bot in Telegram.
2. Open:
   `https://api.telegram.org/bot<YOUR_BOT_TOKEN>/getUpdates`
3. Find the `chat.id` value in the response and store it as `TELEGRAM_CHAT_ID`.

## GitHub Actions Schedule

The workflow runs every 6 hours (UTC) and can also be run manually:

- File: `.github/workflows/culturepass-monitor.yml`
- Trigger: `schedule` + `workflow_dispatch`

Manual Telegram test button:

- File: `.github/workflows/telegram-test.yml`
- Trigger: `workflow_dispatch`
- Optional input: `message`

Manual format-check notification (uses real Culture Pass data and always sends):

- File: `.github/workflows/culturepass-format-check.yml`
- Trigger: `workflow_dispatch`
- Includes full current attraction list in the Telegram output

## Local Run

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python -m playwright install --with-deps chromium

cp .env.example .env
# Fill in .env values, then:
set -a
source .env
set +a

python scripts/monitor_culturepass.py
```
