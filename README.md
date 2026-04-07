# Culture Pass Monitor

This repository monitors NYC Culture Pass data and sends Telegram updates when attraction listings or upcoming dated offers change.

## What It Detects

- New attractions added
- Attractions removed
- Name changes for existing attractions (rename detection)
- Newly published upcoming event-style offers (deduplicated)

## How It Works

1. A Python script logs into `https://culturepassnyc.quipugroup.net/?NYPL`.
2. It extracts the current attraction list from the rendered page.
3. It also queries Culture Pass offers for the first available date and a configurable lookahead window.
4. It compares current data against:
   - `data/attractions_snapshot.json`
   - `data/offers_snapshot.json`
5. If changes exist (or force-notify mode is enabled), it sends Telegram messages.
6. Updated snapshots are saved and committed by GitHub Actions.

## Telegram

- Bot updates are sent to the configured `TELEGRAM_CHAT_ID`.
- Public channel: https://t.me/culturepass

## Required GitHub Secrets

Set these in repository settings:

- `CULTUREPASS_USERNAME`
- `CULTUREPASS_PASSWORD`
- `TELEGRAM_BOT_TOKEN`
- `TELEGRAM_CHAT_ID`

## Main Runtime Environment Variables

The monitor supports these key environment variables:

- `CULTUREPASS_URL` (default: `https://culturepassnyc.quipugroup.net/?NYPL`)
- `SNAPSHOT_PATH` (default: `data/attractions_snapshot.json`)
- `OFFERS_SNAPSHOT_PATH` (default: `data/offers_snapshot.json`)
- `MONITOR_TIMEOUT_MS` (default: `90000`)
- `OFFERS_LOOKAHEAD_DAYS` (used for upcoming-offer window)
- `OFFERS_QUERY_TIMEOUT_MS` (timeout for each offers API query)
- `SEND_ON_FIRST_RUN` (send full list when snapshot does not yet exist)
- `FORCE_NOTIFY` (always notify, even with no detected changes)
- `INCLUDE_CURRENT_LIST` (include full attraction list in message)
- `INCLUDE_OFFER_LIST` (include grouped upcoming offers in message)
- `INCLUDE_EMPTY_SECTIONS` (render empty sections when enabled)
- `NO_SNAPSHOT_UPDATE` (send-only mode; do not rewrite snapshot files)

## Telegram Chat ID

If you do not know your `TELEGRAM_CHAT_ID`:

1. Send any message to your bot in Telegram.
2. Open:
   `https://api.telegram.org/bot<YOUR_BOT_TOKEN>/getUpdates`
3. Find the `chat.id` value in the response and store it as `TELEGRAM_CHAT_ID`.

## GitHub Actions Workflows

### Scheduled monitor

- File: `.github/workflows/culturepass-monitor.yml`
- Triggers: `schedule` (every 6 hours UTC) + `workflow_dispatch`
- Saves and commits both snapshots when changed:
  - `data/attractions_snapshot.json`
  - `data/offers_snapshot.json`

### Manual Telegram test

- File: `.github/workflows/telegram-test.yml`
- Trigger: `workflow_dispatch`
- Optional input: `message`

### Manual format-check notification

- File: `.github/workflows/culturepass-format-check.yml`
- Trigger: `workflow_dispatch`
- Always sends a message with:
  - Full current attraction list
  - Date-grouped upcoming offers list
- Useful for validating output formatting without persisting snapshot changes

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
