#!/usr/bin/env python3
"""
Scrape Top of the Rock ticket prices for each available 10-minute timeslot.

This scraper uses Playwright so it can navigate the live website and interact
with JavaScript-rendered booking widgets.
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from playwright.sync_api import BrowserContext, Page, TimeoutError as PlaywrightTimeoutError, sync_playwright

TARGET_URL = "https://www.rockefellercenter.com/buy-tickets/top-of-the-rock/"


@dataclass
class TicketSlot:
    date: str
    time: str
    price: str
    source: str


def _create_context(playwright, headless: bool) -> BrowserContext:
    browser = playwright.chromium.launch(headless=headless)
    return browser.new_context(
        ignore_https_errors=True,
        user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/123.0.0.0 Safari/537.36"
        ),
        viewport={"width": 1440, "height": 1000},
        locale="en-US",
    )


def _collect_slots_from_json(payload: Any, out: list[TicketSlot], requested_date: str) -> None:
    if isinstance(payload, list):
        for item in payload:
            _collect_slots_from_json(item, out, requested_date)
        return

    if not isinstance(payload, dict):
        return

    keys = {k.lower(): k for k in payload.keys()}

    time_key = next((keys[k] for k in ["time", "starttime", "start_time", "slot_time"] if k in keys), None)
    price_key = next((keys[k] for k in ["price", "adultprice", "adult_price", "displayprice", "formattedprice"] if k in keys), None)

    if time_key and price_key:
        raw_time = str(payload.get(time_key, "")).strip()
        raw_price = str(payload.get(price_key, "")).strip()

        # Keep 10-minute tour times only when we can parse minutes
        minute_match = re.search(r"(\d{1,2}):(\d{2})", raw_time)
        if minute_match:
            if int(minute_match.group(2)) % 10 == 0 and raw_price:
                out.append(TicketSlot(date=requested_date, time=raw_time, price=raw_price, source="network-json"))

    for value in payload.values():
        _collect_slots_from_json(value, out, requested_date)


def _parse_currency(text: str) -> str:
    m = re.search(r"\$\s*\d+(?:\.\d{2})?", text)
    return m.group(0).replace(" ", "") if m else ""


def _collect_slots_from_dom(page: Page, requested_date: str) -> list[TicketSlot]:
    slots: list[TicketSlot] = []

    # Broad selector sweep to support slight site redesigns.
    candidates = page.locator("button, li, div, span")
    count = min(candidates.count(), 4000)

    for i in range(count):
        text = candidates.nth(i).inner_text(timeout=1000).strip()
        if not text or len(text) > 200:
            continue

        # Example matches: "10:30 AM", "10:40 PM", "13:20"
        tm = re.search(r"\b\d{1,2}:\d{2}(?:\s?[AP]M)?\b", text, re.IGNORECASE)
        if not tm:
            continue

        minutes = int(tm.group(0).split(":")[1][:2])
        if minutes % 10 != 0:
            continue

        price = _parse_currency(text)
        if not price:
            # Sometimes time and price are sibling/ancestor nodes.
            parent_text = candidates.nth(i).locator("xpath=..").inner_text(timeout=1000)
            price = _parse_currency(parent_text)

        if not price:
            continue

        slots.append(TicketSlot(date=requested_date, time=tm.group(0).upper().replace("  ", " "), price=price, source="dom"))

    # Deduplicate
    uniq: dict[tuple[str, str], TicketSlot] = {}
    for s in slots:
        uniq[(s.time, s.price)] = s
    return list(uniq.values())


def _save_json(slots: list[TicketSlot], path: Path) -> None:
    path.write_text(json.dumps([asdict(s) for s in slots], indent=2), encoding="utf-8")


def _save_csv(slots: list[TicketSlot], path: Path) -> None:
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=["date", "time", "price", "source"])
        writer.writeheader()
        for slot in slots:
            writer.writerow(asdict(slot))


def scrape_top_of_rock(date: str, output_prefix: str, headless: bool, timeout_ms: int) -> list[TicketSlot]:
    requested_date = date
    slots_from_network: list[TicketSlot] = []

    with sync_playwright() as playwright:
        context = _create_context(playwright, headless=headless)
        page = context.new_page()

        def on_response(response):
            content_type = response.headers.get("content-type", "")
            if "json" not in content_type:
                return
            url = response.url.lower()
            if not any(k in url for k in ["availability", "timeslot", "ticket", "calendar", "book"]):
                return
            try:
                payload = response.json()
            except Exception:
                return
            _collect_slots_from_json(payload, slots_from_network, requested_date)

        page.on("response", on_response)

        try:
            page.goto(TARGET_URL, wait_until="domcontentloaded", timeout=timeout_ms)
        except PlaywrightTimeoutError:
            print("Timed out opening target page", file=sys.stderr)
        except Exception as exc:
            print(f"Navigation error: {exc}", file=sys.stderr)

        # Allow Cloudflare / booking widget / async calls to settle.
        page.wait_for_timeout(12_000)

        # Attempt to open the ticket widget if a CTA exists.
        for selector in [
            "text=Buy Tickets",
            "text=Check availability",
            "text=Select Date",
            "button:has-text('Buy')",
            "a:has-text('Buy')",
        ]:
            loc = page.locator(selector)
            if loc.count() > 0:
                try:
                    loc.first.click(timeout=2_500)
                    page.wait_for_timeout(2_000)
                    break
                except Exception:
                    continue

        dom_slots = _collect_slots_from_dom(page, requested_date)

        all_slots = slots_from_network + dom_slots
        dedup: dict[tuple[str, str], TicketSlot] = {}
        for slot in all_slots:
            dedup[(slot.time, slot.price)] = slot

        context.close()

    slots = sorted(dedup.values(), key=lambda s: (s.time, s.price))

    out_json = Path(f"{output_prefix}.json")
    out_csv = Path(f"{output_prefix}.csv")
    _save_json(slots, out_json)
    _save_csv(slots, out_csv)

    print(f"Saved {len(slots)} slots -> {out_json} and {out_csv}")
    return slots


def main() -> None:
    parser = argparse.ArgumentParser(description="Scrape Top of the Rock ticket times and prices")
    parser.add_argument("--date", default=datetime.now().strftime("%Y-%m-%d"), help="Date label to include in output")
    parser.add_argument("--output-prefix", default="top_of_the_rock_prices", help="Output filename prefix")
    parser.add_argument("--headed", action="store_true", help="Run browser in headed mode")
    parser.add_argument("--timeout-ms", type=int, default=90_000, help="Navigation timeout in milliseconds")
    args = parser.parse_args()

    scrape_top_of_rock(
        date=args.date,
        output_prefix=args.output_prefix,
        headless=not args.headed,
        timeout_ms=args.timeout_ms,
    )


if __name__ == "__main__":
    main()
