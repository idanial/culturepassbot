#!/usr/bin/env python3
"""Monitor NYC Culture Pass attractions and notify via Telegram on changes."""

from __future__ import annotations

import json
import os
import re
import sys
import time
from html import escape, unescape
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Sequence, Tuple
from zoneinfo import ZoneInfo

import requests
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright

DEFAULT_URL = "https://culturepassnyc.quipugroup.net/?NYPL"
DEFAULT_SNAPSHOT_PATH = Path("data/attractions_snapshot.json")
DEFAULT_TIMEOUT_MS = 90000
TELEGRAM_SEND_URL = "https://api.telegram.org/bot{token}/sendMessage"
TELEGRAM_TEXT_LIMIT = 4000
LOCAL_TIMEZONE = ZoneInfo("America/New_York")


@dataclass(frozen=True)
class Attraction:
    id: str
    name: str
    url: str = ""


@dataclass(frozen=True)
class OfferEntry:
    date_text: str
    attraction_name: str
    offer_title: str
    start_time: str
    end_time: str
    venue_name: str
    offer_id: str


def _normalize_name(name: str) -> str:
    return " ".join(name.split()).strip()


def _try_parse_date(value: str) -> date | None:
    text = _normalize_name(value)
    if not text:
        return None

    formats = ("%Y-%m-%d", "%m-%d-%Y", "%m/%d/%Y", "%B %d, %Y", "%b %d, %Y")
    for fmt in formats:
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def _format_date_readable(value: str) -> str:
    parsed = _try_parse_date(value)
    if parsed is None:
        return _normalize_name(value)
    return f"{parsed.strftime('%b')} {parsed.day}, {parsed.year}"


def _normalize_time(value: str) -> str:
    text = _normalize_name(value)
    if not text:
        return ""

    formats = ("%I:%M %p", "%I %p", "%H:%M", "%H:%M:%S")
    for fmt in formats:
        try:
            parsed = datetime.strptime(text.upper(), fmt)
            return parsed.strftime("%I:%M %p").lstrip("0")
        except ValueError:
            continue
    return text


def _format_timestamp(value: datetime) -> str:
    month = value.strftime("%b")
    hour = value.strftime("%I").lstrip("0") or "0"
    am_pm = value.strftime("%p")
    zone_label = value.tzname() or "ET"
    return f"{month} {value.day}, {value.year} {hour}:{value.strftime('%M')} {am_pm} {zone_label}"


def _html(text: str) -> str:
    return escape(text, quote=False)


def _normalize_url(value: str) -> str:
    text = value.strip()
    if text.startswith("http://") or text.startswith("https://"):
        return text
    return ""


def _telegram_link(label: str, url: str) -> str:
    safe_label = _html(label)
    safe_url = _normalize_url(url)
    if not safe_url:
        return safe_label
    return f'<a href="{escape(safe_url, quote=True)}">{safe_label}</a>'


def _contains_explicit_event_date(text: str) -> bool:
    value = _normalize_name(unescape(text))
    if not value:
        return False

    numeric_date = re.search(r"\b\d{1,2}[/-]\d{1,2}[/-]\d{2,4}\b", value)
    if numeric_date:
        return True

    month_date = re.search(
        r"\b(?:jan|january|feb|february|mar|march|apr|april|may|jun|june|"
        r"jul|july|aug|august|sep|sept|september|oct|october|nov|november|"
        r"dec|december)\s+\d{1,2},?\s+\d{2,4}\b",
        value,
        flags=re.IGNORECASE,
    )
    return month_date is not None


def _stable_sort(attractions: Iterable[Attraction]) -> List[Attraction]:
    return sorted(
        attractions,
        key=lambda item: (item.name.casefold(), item.id.casefold()),
    )


def _stable_sort_offers(offers: Iterable[OfferEntry]) -> List[OfferEntry]:
    return sorted(
        offers,
        key=lambda item: (
            _try_parse_date(item.date_text) or date.max,
            item.date_text.casefold(),
            item.attraction_name.casefold(),
            item.offer_title.casefold(),
            item.start_time.casefold(),
            item.end_time.casefold(),
            item.offer_id.casefold(),
        ),
    )


def _iter_response_items(raw: Any) -> List[Dict[str, Any]]:
    if isinstance(raw, list):
        return [item for item in raw if isinstance(item, dict)]
    if isinstance(raw, dict):
        return [item for item in raw.values() if isinstance(item, dict)]
    return []


def _chunk_message(message: str, limit: int) -> List[str]:
    lines = message.splitlines()
    chunks: List[str] = []
    current = ""

    for line in lines:
        candidate = line if not current else f"{current}\n{line}"
        if len(candidate) <= limit:
            current = candidate
            continue

        if current:
            chunks.append(current)
            current = ""

        if len(line) <= limit:
            current = line
            continue

        # Extremely long single line fallback.
        start = 0
        while start < len(line):
            end = start + limit
            chunk = line[start:end]
            if len(chunk) == limit:
                chunks.append(chunk)
            else:
                current = chunk
            start = end

    if current:
        chunks.append(current)

    return chunks if chunks else [""]


def _wait_for_authenticated_login(page: Any, timeout_ms: int) -> None:
    page.wait_for_function(
        """
        () => {
          if (!window.ePASS) return false;
          const pid = ePASS.patronID;
          return !!pid && pid !== 0 && pid !== "0";
        }
        """,
        timeout=timeout_ms,
    )
    page.wait_for_selector("#ePASSLogoutLink", state="visible", timeout=timeout_ms)


def _to_payload(attractions: Sequence[Attraction]) -> Dict[str, List[Dict[str, str]]]:
    return {
        "attractions": [{"id": item.id, "name": item.name, "url": item.url} for item in attractions],
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
        raw_url = _normalize_url(str(raw_item.get("url", "")))
        if not raw_name:
            continue
        snapshot.append(Attraction(id=raw_id, name=raw_name, url=raw_url))
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
            page.wait_for_selector("#ePASSPatronNumber", state="visible", timeout=timeout_ms)
            page.fill("#ePASSPatronNumber", username)
            page.fill("#ePASSPatronPassword", password)
            page.click("#ePASSButtonLogin")

            page.wait_for_selector(
                "#ePASSLoginErrorMsg, #ePASSLogoutLink",
                timeout=timeout_ms,
            )
            if page.is_visible("#ePASSLoginErrorMsg"):
                error_text = _normalize_name(page.locator("#ePASSLoginErrorMsg").inner_text())
                if error_text:
                    raise RuntimeError(f"Culture Pass login failed: {error_text}")
                raise RuntimeError("Culture Pass login failed with an unknown error.")
            _wait_for_authenticated_login(page, timeout_ms)

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
                    const anchorNode = nameNode.closest('a') || row.querySelector('a[href]');
                    const rawUrl = anchorNode && anchorNode.href ? normalize(anchorNode.href) : "";
                    mapped.push({ id: rawId, name: rawName, url: rawUrl });
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
                      url: (() => {
                        const anchorNode = node.closest('a') || node.parentElement?.querySelector('a[href]');
                        return anchorNode && anchorNode.href ? normalize(anchorNode.href) : "";
                      })(),
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
                url_value = _normalize_url(str(row.get("url", "")))
                if not name:
                    continue
                key = (attraction_id, name.casefold())
                unique[key] = Attraction(id=attraction_id, name=name, url=url_value)

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


def _query_offers_for_date(page: Any, date_selected: str, timeout_ms: int) -> Dict[str, Any]:
    response = page.evaluate(
        """
        async ({ dateSelected, timeoutMs }) => {
          return await new Promise((resolve, reject) => {
            if (typeof ePASSAPIRequest !== "function" || !window.ePASS || !ePASS.apiURL) {
              reject(new Error("ePASS API bridge is unavailable."));
              return;
            }

            const ajaxData = {
              dataType: "json",
              method: "ePASS_Search",
              functionFile: "Attractions",
              searchType: "Offers",
              dateSelected,
              limits: "",
              language: ePASS.language || "en"
            };

            let completed = false;
            const timer = setTimeout(() => {
              if (completed) return;
              completed = true;
              reject(new Error(`ePASS_Search timeout for dateSelected=${dateSelected}`));
            }, timeoutMs);

            try {
              ePASSAPIRequest(ePASS.apiURL, ajaxData, (data) => {
                if (completed) return;
                completed = true;
                clearTimeout(timer);
                resolve(data || {});
              });
            } catch (error) {
              if (completed) return;
              completed = true;
              clearTimeout(timer);
              reject(error);
            }
          });
        }
        """,
        {"dateSelected": date_selected, "timeoutMs": timeout_ms},
    )
    if not isinstance(response, dict):
        raise RuntimeError(f"Unexpected offers response for {date_selected}: {response}")
    return response


def _extract_offer_entries(response: Dict[str, Any], fallback_date: str = "") -> List[OfferEntry]:
    selected_date = _normalize_name(str(response.get("dateSelected", ""))) or fallback_date
    attraction_items = _iter_response_items(response.get("attractionList", []))

    entries: List[OfferEntry] = []
    for attraction_info in attraction_items:
        attraction_name = _normalize_name(str(attraction_info.get("name", "")))
        offers = _iter_response_items(attraction_info.get("offers", []))

        for offer_info in offers:
            offer_title = _normalize_name(unescape(str(offer_info.get("offerTitle", ""))))
            if not offer_title:
                continue
            internal_offer_name = _normalize_name(unescape(str(offer_info.get("internalOfferName", ""))))

            # Keep only event-style offers that explicitly include a date token.
            if not (_contains_explicit_event_date(offer_title) or _contains_explicit_event_date(internal_offer_name)):
                continue

            entries.append(
                OfferEntry(
                    date_text=selected_date,
                    attraction_name=attraction_name,
                    offer_title=offer_title,
                    start_time=_normalize_name(str(offer_info.get("startTime", ""))),
                    end_time=_normalize_name(str(offer_info.get("endTime", ""))),
                    venue_name=_normalize_name(str(offer_info.get("venueName", ""))),
                    offer_id=_normalize_name(str(offer_info.get("offerID", ""))),
                )
            )
    return entries


def _dedupe_offers(offers: Iterable[OfferEntry]) -> List[OfferEntry]:
    unique: Dict[Tuple[str, str, str, str, str, str, str], OfferEntry] = {}
    for entry in offers:
        key = (
            entry.date_text,
            entry.attraction_name,
            entry.offer_title,
            entry.start_time,
            entry.end_time,
            entry.venue_name,
            entry.offer_id,
        )
        unique[key] = entry
    return _stable_sort_offers(unique.values())


def _format_grouped_offer_line(entry: OfferEntry) -> str:
    date_display = _format_date_readable(entry.date_text)
    start_display = _normalize_time(entry.start_time)
    end_display = _normalize_time(entry.end_time)
    datetime_display = date_display
    if start_display and end_display:
        datetime_display = f"{date_display} {start_display} - {end_display}"
    elif start_display:
        datetime_display = f"{date_display} {start_display}"
    elif end_display:
        datetime_display = f"{date_display} - {end_display}"

    return datetime_display


def _group_offers_by_venue(offers: Sequence[OfferEntry]) -> List[Tuple[str, List[OfferEntry]]]:
    grouped: Dict[str, List[OfferEntry]] = {}
    for entry in offers:
        venue_name = _normalize_name(entry.venue_name) or "Unknown venue"
        grouped.setdefault(venue_name, []).append(entry)

    grouped_items = sorted(grouped.items(), key=lambda item: item[0].casefold())
    return [
        (
            venue_name,
            sorted(
                venue_entries,
                key=lambda item: (
                    _try_parse_date(item.date_text) or date.max,
                    item.date_text.casefold(),
                    _normalize_time(item.start_time),
                    item.attraction_name.casefold(),
                    item.offer_title.casefold(),
                ),
            ),
        )
        for venue_name, venue_entries in grouped_items
    ]


def fetch_upcoming_offers(
    url: str,
    username: str,
    password: str,
    timeout_ms: int,
    lookahead_days: int,
    query_timeout_ms: int,
    headless: bool = True,
) -> List[OfferEntry]:
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=headless)
        context = browser.new_context()
        page = context.new_page()

        try:
            page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
            page.wait_for_selector("#ePASSPatronNumber", state="visible", timeout=timeout_ms)
            page.fill("#ePASSPatronNumber", username)
            page.fill("#ePASSPatronPassword", password)
            page.click("#ePASSButtonLogin")

            page.wait_for_selector(
                "#ePASSLoginErrorMsg, #ePASSLogoutLink",
                timeout=timeout_ms,
            )
            if page.is_visible("#ePASSLoginErrorMsg"):
                error_text = _normalize_name(page.locator("#ePASSLoginErrorMsg").inner_text())
                if error_text:
                    raise RuntimeError(f"Culture Pass login failed while fetching offers: {error_text}")
                raise RuntimeError("Culture Pass login failed while fetching offers.")
            _wait_for_authenticated_login(page, timeout_ms)

            first_response = _query_offers_for_date(page, "firstAvailable", query_timeout_ms)
            offer_entries = _extract_offer_entries(first_response)

            first_date_text = _normalize_name(str(first_response.get("dateSelected", "")))
            first_date = _try_parse_date(first_date_text)

            if first_date is not None and lookahead_days > 0:
                for offset in range(1, lookahead_days + 1):
                    date_text = (first_date + timedelta(days=offset)).strftime("%Y-%m-%d")
                    try:
                        response = _query_offers_for_date(page, date_text, query_timeout_ms)
                    except RuntimeError:
                        continue
                    if _normalize_name(str(response.get("status", ""))) != "Passed":
                        continue
                    offer_entries.extend(_extract_offer_entries(response, fallback_date=date_text))

            return _dedupe_offers(offer_entries)

        except PlaywrightTimeoutError as exc:
            screenshot_name = f"debug-offers-timeout-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}.png"
            page.screenshot(path=screenshot_name, full_page=True)
            raise RuntimeError(
                f"Timed out while loading Culture Pass offers. Saved screenshot: {screenshot_name}"
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


def build_message(
    changes: Dict[str, List],
    old_count: int,
    new_count: int,
    include_empty_sections: bool = False,
    title: str = "Culture Pass update detected",
    current_names: Sequence[str] | None = None,
    offer_entries: Sequence[OfferEntry] | None = None,
    name_links: Dict[str, str] | None = None,
    offer_venue_links: Dict[str, str] | None = None,
    include_full_offer_list: bool = False,
) -> str:
    def place_label(name: str) -> str:
        linked_url = ""
        if name_links is not None:
            linked_url = name_links.get(name.casefold(), "")
        return _telegram_link(name, linked_url)

    now_text = _format_timestamp(datetime.now(LOCAL_TIMEZONE))
    lines = [f"{_html(title)} ({_html(now_text)})", f"Total attractions: {new_count} (previously {old_count})"]

    if changes["added"] or include_empty_sections:
        lines.append("")
        lines.append(f"<b>Added ({len(changes['added'])}):</b>")
        if changes["added"]:
            lines.extend([f"- {place_label(name)}" for name in changes["added"]])
        else:
            lines.append("- none")

    if not include_full_offer_list and offer_entries is not None and (changes["added"] or include_empty_sections):
        added_names = {name.casefold() for name in changes["added"]}
        added_offers = [entry for entry in offer_entries if entry.attraction_name.casefold() in added_names]
        lines.append("")
        lines.append(f"<b>Upcoming offers for newly added places ({len(added_offers)}):</b>")
        if added_offers:
            grouped_added_offers = _group_offers_by_venue(added_offers)
            for index, (venue_name, venue_offers) in enumerate(grouped_added_offers):
                if index > 0:
                    lines.append("")
                venue_url = ""
                if offer_venue_links is not None:
                    venue_url = offer_venue_links.get(venue_name.casefold(), "")
                if not venue_url and name_links is not None:
                    venue_url = name_links.get(venue_name.casefold(), "")
                lines.append(f"<b>{_telegram_link(venue_name, venue_url)}</b>")
                lines.extend([f"- {_html(_format_grouped_offer_line(entry))}" for entry in venue_offers])
        else:
            lines.append("- none")

    if changes["removed"] or include_empty_sections:
        lines.append("")
        lines.append(f"<b>Removed ({len(changes['removed'])}):</b>")
        if changes["removed"]:
            lines.extend([f"- {place_label(name)}" for name in changes["removed"]])
        else:
            lines.append("- none")

    if changes["renamed"] or include_empty_sections:
        lines.append("")
        lines.append(f"<b>Renamed ({len(changes['renamed'])}):</b>")
        if changes["renamed"]:
            lines.extend([f"- {place_label(old_name)} -> {place_label(new_name)}" for old_name, new_name in changes["renamed"]])
        else:
            lines.append("- none")

    if current_names is not None:
        lines.append("")
        lines.append(f"<b>Current attractions ({len(current_names)}):</b>")
        if current_names:
            lines.extend([f"- {place_label(name)}" for name in current_names])
        else:
            lines.append("- none")

    if include_full_offer_list and offer_entries is not None:
        lines.append("")
        lines.append(f"<b>Upcoming offers ({len(offer_entries)}):</b>")
        if offer_entries:
            grouped_offers = _group_offers_by_venue(offer_entries)
            for index, (venue_name, venue_offers) in enumerate(grouped_offers):
                if index > 0:
                    lines.append("")
                venue_url = ""
                if offer_venue_links is not None:
                    venue_url = offer_venue_links.get(venue_name.casefold(), "")
                if not venue_url and name_links is not None:
                    venue_url = name_links.get(venue_name.casefold(), "")
                lines.append(f"<b>{_telegram_link(venue_name, venue_url)}</b>")
                lines.extend([f"- {_html(_format_grouped_offer_line(entry))}" for entry in venue_offers])
        else:
            lines.append("- none")

    return "\n".join(lines)


def _build_name_link_map(*snapshots: Sequence[Attraction]) -> Dict[str, str]:
    links: Dict[str, str] = {}
    for snapshot in snapshots:
        for item in snapshot:
            key = item.name.casefold()
            value = _normalize_url(item.url)
            if value:
                links[key] = value
    return links


def _build_offer_venue_link_map(
    offer_entries: Sequence[OfferEntry] | None,
    name_links: Dict[str, str],
) -> Dict[str, str]:
    if not offer_entries:
        return {}

    links: Dict[str, str] = {}
    for entry in offer_entries:
        venue_key = entry.venue_name.casefold()
        if not venue_key or venue_key in links:
            continue
        attraction_url = name_links.get(entry.attraction_name.casefold(), "")
        if attraction_url:
            links[venue_key] = attraction_url
    return links


def send_telegram(bot_token: str, chat_id: str, message: str) -> None:
    chunks = _chunk_message(message, TELEGRAM_TEXT_LIMIT - 24)
    total = len(chunks)

    for index, chunk in enumerate(chunks, start=1):
        text = chunk
        if total > 1:
            prefix = f"[{index}/{total}]\n"
            text = prefix + chunk

        max_attempts = 4
        for attempt in range(max_attempts):
            response = requests.post(
                TELEGRAM_SEND_URL.format(token=bot_token),
                json={"chat_id": chat_id, "text": text, "parse_mode": "HTML"},
                timeout=30,
            )

            payload: Dict[str, Any]
            try:
                payload = response.json()
            except ValueError:
                payload = {}

            if response.status_code == 429:
                retry_after = 2
                if isinstance(payload, dict):
                    params = payload.get("parameters", {})
                    if isinstance(params, dict):
                        retry_after = int(params.get("retry_after", retry_after))
                if attempt == max_attempts - 1:
                    raise RuntimeError(f"Telegram rate limit persisted after retries: {payload}")
                time.sleep(max(retry_after, 1))
                continue

            response.raise_for_status()
            if not payload.get("ok"):
                raise RuntimeError(f"Telegram API returned failure: {payload}")
            break

        # Reduce chance of 429 when message spans many chunks.
        if index < total:
            time.sleep(0.45)


def env_flag(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


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
    send_on_first_run = env_flag("SEND_ON_FIRST_RUN", False)
    force_notify = env_flag("FORCE_NOTIFY", False)
    include_empty_sections = env_flag("INCLUDE_EMPTY_SECTIONS", False)
    include_current_list = env_flag("INCLUDE_CURRENT_LIST", False)
    include_offer_list = env_flag("INCLUDE_OFFER_LIST", False)
    no_snapshot_update = env_flag("NO_SNAPSHOT_UPDATE", False)
    offers_lookahead_days = int(os.getenv("OFFERS_LOOKAHEAD_DAYS", "30"))
    offers_query_timeout_ms = int(os.getenv("OFFERS_QUERY_TIMEOUT_MS", "25000"))
    if offers_lookahead_days < 0:
        offers_lookahead_days = 0
    if offers_query_timeout_ms < 5000:
        offers_query_timeout_ms = 5000
    if offers_query_timeout_ms > timeout_ms:
        offers_query_timeout_ms = timeout_ms

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
    include_added_place_offers = bool(old_snapshot and changes["added"])
    offer_entries: List[OfferEntry] | None = None
    if include_offer_list or include_added_place_offers:
        offer_entries = fetch_upcoming_offers(
            url=url,
            username=username,
            password=password,
            timeout_ms=timeout_ms,
            lookahead_days=offers_lookahead_days,
            query_timeout_ms=offers_query_timeout_ms,
            headless=headless,
        )
    name_links = _build_name_link_map(old_snapshot, new_snapshot)
    offer_venue_links = _build_offer_venue_link_map(offer_entries, name_links)

    if not old_snapshot:
        if not no_snapshot_update:
            save_snapshot(snapshot_path, new_snapshot)
            print(f"Initialized snapshot with {len(new_snapshot)} attractions.")
        else:
            print(
                f"Snapshot initialization skipped (NO_SNAPSHOT_UPDATE=true). "
                f"Current attractions: {len(new_snapshot)}."
            )

        if send_on_first_run or force_notify:
            if force_notify and (include_current_list or include_offer_list or include_empty_sections):
                message = build_message(
                    changes=diff_attractions([], new_snapshot),
                    old_count=0,
                    new_count=len(new_snapshot),
                    include_empty_sections=include_empty_sections,
                    title="Culture Pass format check (initial snapshot)",
                    current_names=[item.name for item in new_snapshot] if include_current_list else None,
                    offer_entries=offer_entries,
                    name_links=name_links,
                    offer_venue_links=offer_venue_links,
                    include_full_offer_list=include_offer_list,
                )
            else:
                message = f"Culture Pass monitor initialized with {len(new_snapshot)} attractions."
            send_telegram(bot_token, chat_id, message)
            print("Initialization message sent to Telegram.")
        return 0

    if not changed and not force_notify:
        print(f"No listing changes detected ({len(new_snapshot)} attractions).")
        return 0

    title = "Culture Pass update detected" if changed else "Culture Pass format check (no changes)"
    current_names = [item.name for item in new_snapshot] if include_current_list else None
    message = build_message(
        changes,
        old_count=len(old_snapshot),
        new_count=len(new_snapshot),
        include_empty_sections=include_empty_sections,
        title=title,
        current_names=current_names,
        offer_entries=offer_entries,
        name_links=name_links,
        offer_venue_links=offer_venue_links,
        include_full_offer_list=include_offer_list,
    )
    send_telegram(bot_token, chat_id, message)
    if changed:
        print("Change notification sent to Telegram.")
    else:
        print("Format-check notification sent to Telegram.")

    if not no_snapshot_update:
        save_snapshot(snapshot_path, new_snapshot)
        print("Snapshot updated.")
    else:
        print("Snapshot update skipped (NO_SNAPSHOT_UPDATE=true).")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:  # pylint: disable=broad-except
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
