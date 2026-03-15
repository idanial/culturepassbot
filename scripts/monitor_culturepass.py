#!/usr/bin/env python3
"""Monitor NYC Culture Pass attractions and notify via Telegram on changes."""

from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Iterable, List, Sequence, Tuple

import requests
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright

DEFAULT_URL = "https://culturepassnyc.quipugroup.net/?NYPL"
DEFAULT_SNAPSHOT_PATH = Path("data/attractions_snapshot.json")
DEFAULT_TIMEOUT_MS = 90000
TELEGRAM_SEND_URL = "https://api.telegram.org/bot{token}/sendMessage"
TELEGRAM_TEXT_LIMIT = 4000


@dataclass(frozen=True)
class Attraction:
    id: str
    name: str


def _normalize_name(name: str) -> str:
    return " ".join(name.split()).strip()


def _stable_sort(attractions: Iterable[Attraction]) -> List[Attraction]:
    return sorted(
        attractions,
        key=lambda item: (item.name.casefold(), item.id.casefold()),
    )


def _to_payload(attractions: Sequence[Attraction]) -> Dict[str, List[Dict[str, str]]]:
    return {
        "attractions": [{"id": item.id, "name": item.name} for item in attractions],
    }


def load_snapshot(snapshot_path: Path) -> List[Attraction]:
    if not snapshot_path.exists():
        return []

    data = json.loads(snapshot_path.read_text(encoding="utf-8"))
    raw_items = data.get("attractions", [])
    snapshot: List[Attraction] = []
    for raw_item in raw_items:
        raw_id = str(raw_item.get("id", "")).strip()
        raw_name = _normalize_name(str(raw_item.get("name", "")))
        if not raw_name:
            continue
        snapshot.append(Attraction(id=raw_id, name=raw_name))
    return _stable_sort(snapshot)


def save_snapshot(snapshot_path: Path, attractions: Sequence[Attraction]) -> None:
    snapshot_path.parent.mkdir(parents=True, exist_ok=True)
    payload = _to_payload(attractions)
    snapshot_path.write_text(
        json.dumps(payload, indent=2, ensure_ascii=True, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def fetch_attractions(
    url: str,
    username: str,
    password: str,
    timeout_ms: int,
    headless: bool = True,
) -> List[Attraction]:
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=headless)
        context = browser.new_context()
        page = context.new_page()

        try:
            page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
            page.wait_for_selector("#ePASSPatronNumber", timeout=timeout_ms)
            page.fill("#ePASSPatronNumber", username)
            page.fill("#ePASSPatronPassword", password)
            page.click("#ePASSButtonLogin")

            page.wait_for_selector(
                "#ePASSNav, #ePASSLoginErrorMsg",
                timeout=timeout_ms,
            )
            if page.is_visible("#ePASSLoginErrorMsg"):
                error_text = _normalize_name(page.locator("#ePASSLoginErrorMsg").inner_text())
                if error_text:
                    raise RuntimeError(f"Culture Pass login failed: {error_text}")
                raise RuntimeError("Culture Pass login failed with an unknown error.")

            if page.locator("#ePASSAllAttractionsAnchor").count() > 0:
                page.click("#ePASSAllAttractionsAnchor")

            page.wait_for_selector("#ePASSAttractionsList", timeout=timeout_ms)
            page.wait_for_timeout(2500)

            rows = page.evaluate(
                """
                () => {
                  const normalize = (value) => (value || "").replace(/\\s+/g, " ").trim();
                  const mapped = [];
                  const listRows = Array.from(
                    document.querySelectorAll('[id^="ePASSAttractionDiv"]')
                  );

                  for (const row of listRows) {
                    const nameNode = row.querySelector('.ePASSAttractionName');
                    if (!nameNode) continue;

                    const rawName = normalize(nameNode.textContent);
                    if (!rawName) continue;

                    const rawId = normalize(row.id.replace('ePASSAttractionDiv', ''));
                    mapped.push({ id: rawId, name: rawName });
                  }

                  if (mapped.length > 0) {
                    return mapped;
                  }

                  return Array.from(
                    document.querySelectorAll('.ePASSAttractionName')
                  )
                    .map((node, index) => ({
                      id: `fallback-${index}`,
                      name: normalize(node.textContent),
                    }))
                    .filter((item) => item.name.length > 0);
                }
                """
            )
            if not rows:
                raise RuntimeError("No attractions were found after successful login.")

            unique: Dict[Tuple[str, str], Attraction] = {}
            for row in rows:
                attraction_id = str(row.get("id", "")).strip()
                name = _normalize_name(str(row.get("name", "")))
                if not name:
                    continue
                key = (attraction_id, name.casefold())
                unique[key] = Attraction(id=attraction_id, name=name)

            attractions = _stable_sort(unique.values())
            if not attractions:
                raise RuntimeError("No attractions were extracted from the page.")
            return attractions

        except PlaywrightTimeoutError as exc:
            screenshot_name = f"debug-timeout-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}.png"
            page.screenshot(path=screenshot_name, full_page=True)
            raise RuntimeError(
                f"Timed out while loading Culture Pass data. Saved screenshot: {screenshot_name}"
            ) from exc
        finally:
            context.close()
            browser.close()


def diff_attractions(
    old_items: Sequence[Attraction],
    new_items: Sequence[Attraction],
) -> Dict[str, List]:
    old_by_id = {item.id: item.name for item in old_items if item.id}
    new_by_id = {item.id: item.name for item in new_items if item.id}

    old_id_keys = set(old_by_id.keys())
    new_id_keys = set(new_by_id.keys())

    added = sorted([new_by_id[item_id] for item_id in new_id_keys - old_id_keys], key=str.casefold)
    removed = sorted([old_by_id[item_id] for item_id in old_id_keys - new_id_keys], key=str.casefold)

    renamed: List[Tuple[str, str]] = []
    for item_id in sorted(old_id_keys & new_id_keys):
        old_name = old_by_id[item_id]
        new_name = new_by_id[item_id]
        if old_name != new_name:
            renamed.append((old_name, new_name))

    old_without_id = {item.name for item in old_items if not item.id}
    new_without_id = {item.name for item in new_items if not item.id}
    added.extend(sorted(new_without_id - old_without_id, key=str.casefold))
    removed.extend(sorted(old_without_id - new_without_id, key=str.casefold))

    return {"added": added, "removed": removed, "renamed": renamed}


def build_message(changes: Dict[str, List], old_count: int, new_count: int) -> str:
    now_text = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    lines = [f"Culture Pass update detected ({now_text})", f"Total attractions: {new_count} (previously {old_count})"]

    if changes["added"]:
        lines.append("")
        lines.append(f"Added ({len(changes['added'])}):")
        lines.extend([f"- {name}" for name in changes["added"]])

    if changes["removed"]:
        lines.append("")
        lines.append(f"Removed ({len(changes['removed'])}):")
        lines.extend([f"- {name}" for name in changes["removed"]])

    if changes["renamed"]:
        lines.append("")
        lines.append(f"Renamed ({len(changes['renamed'])}):")
        lines.extend([f"- {old_name} -> {new_name}" for old_name, new_name in changes["renamed"]])

    return "\n".join(lines)


def send_telegram(bot_token: str, chat_id: str, message: str) -> None:
    if len(message) > TELEGRAM_TEXT_LIMIT:
        message = message[: TELEGRAM_TEXT_LIMIT - 120].rstrip()
        message += "\n\n[message truncated: too many changes to fit in one Telegram message]"

    response = requests.post(
        TELEGRAM_SEND_URL.format(token=bot_token),
        json={"chat_id": chat_id, "text": message},
        timeout=30,
    )
    response.raise_for_status()
    payload = response.json()
    if not payload.get("ok"):
        raise RuntimeError(f"Telegram API returned failure: {payload}")


def env_required(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


def main() -> int:
    url = os.getenv("CULTUREPASS_URL", DEFAULT_URL).strip() or DEFAULT_URL
    snapshot_path = Path(os.getenv("SNAPSHOT_PATH", str(DEFAULT_SNAPSHOT_PATH)))
    timeout_ms = int(os.getenv("MONITOR_TIMEOUT_MS", str(DEFAULT_TIMEOUT_MS)))
    headless = os.getenv("HEADLESS", "true").strip().lower() != "false"
    send_on_first_run = os.getenv("SEND_ON_FIRST_RUN", "false").strip().lower() == "true"

    username = env_required("CULTUREPASS_USERNAME")
    password = env_required("CULTUREPASS_PASSWORD")
    bot_token = env_required("TELEGRAM_BOT_TOKEN")
    chat_id = env_required("TELEGRAM_CHAT_ID")

    old_snapshot = load_snapshot(snapshot_path)
    new_snapshot = fetch_attractions(
        url=url,
        username=username,
        password=password,
        timeout_ms=timeout_ms,
        headless=headless,
    )
    changes = diff_attractions(old_snapshot, new_snapshot)
    changed = bool(changes["added"] or changes["removed"] or changes["renamed"])

    if not old_snapshot:
        save_snapshot(snapshot_path, new_snapshot)
        print(f"Initialized snapshot with {len(new_snapshot)} attractions.")
        if send_on_first_run:
            message = f"Culture Pass monitor initialized with {len(new_snapshot)} attractions."
            send_telegram(bot_token, chat_id, message)
            print("Initialization message sent to Telegram.")
        return 0

    if not changed:
        print(f"No listing changes detected ({len(new_snapshot)} attractions).")
        return 0

    message = build_message(changes, old_count=len(old_snapshot), new_count=len(new_snapshot))
    send_telegram(bot_token, chat_id, message)
    print("Change notification sent to Telegram.")

    save_snapshot(snapshot_path, new_snapshot)
    print("Snapshot updated.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:  # pylint: disable=broad-except
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
