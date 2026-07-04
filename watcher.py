"""
The Martin (Sunnyvale) 1-bedroom availability watcher.

Strategy:
  1. Load https://livethemartin.com/floorplans/ in headless Chromium (Playwright).
  2. Intercept every JSON response the page fetches (RentCafe-backed sites load
     floorplan/unit availability via XHR). Collect anything that looks like
     floorplan or unit data.
  3. As a fallback, also scrape the rendered DOM text for 1-bedroom entries.
  4. Diff against state.json from the previous run.
  5. If new 1BR listings appeared (or a floorplan went from 0 -> some availability),
     send a Telegram message (and always print to stdout).

Usage:
  python watcher.py            # normal run
  python watcher.py --debug    # also dumps all captured API JSON to debug_capture.json

Env vars (only needed for notifications):
  TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
"""

import json
import os
import re
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from playwright.sync_api import sync_playwright

PAGE_URL = "https://livethemartin.com/floorplans/"
STATE_FILE = Path(__file__).parent / "state.json"
DEBUG = "--debug" in sys.argv


# ---------------------------------------------------------------------------
# Capture: render page and collect JSON API responses
# ---------------------------------------------------------------------------

def capture_page_data():
    captured = []          # list of (url, parsed_json)
    rendered_text = ""

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/126.0.0.0 Safari/537.36"
            )
        )

        def on_response(resp):
            ctype = (resp.headers.get("content-type") or "").lower()
            url = resp.url
            looks_relevant = any(
                k in url.lower()
                for k in ("rentcafe", "floorplan", "availab", "admin-ajax",
                          "apartment", "unit", "wp-json")
            )
            if "json" in ctype or looks_relevant:
                try:
                    body = resp.text()
                    data = json.loads(body)
                    captured.append((url, data))
                except Exception:
                    pass

        page.on("response", on_response)
        page.goto(PAGE_URL, wait_until="networkidle", timeout=60_000)
        # Give lazy widgets a moment, then scroll to trigger lazy loading
        page.mouse.wheel(0, 3000)
        page.wait_for_timeout(4000)
        rendered_text = page.inner_text("body")
        browser.close()

    if DEBUG:
        Path("debug_capture.json").write_text(
            json.dumps([{"url": u, "data": d} for u, d in captured], indent=2)
        )
        Path("debug_rendered.txt").write_text(rendered_text)
        print(f"[debug] captured {len(captured)} JSON responses -> debug_capture.json")

    return captured, rendered_text


# ---------------------------------------------------------------------------
# Parse: pull 1BR listings out of whatever we captured
# ---------------------------------------------------------------------------

def _walk(obj):
    """Yield every dict nested anywhere inside obj."""
    if isinstance(obj, dict):
        yield obj
        for v in obj.values():
            yield from _walk(v)
    elif isinstance(obj, list):
        for item in obj:
            yield from _walk(item)


def _get(d, *names):
    """Case-insensitive dict lookup across several candidate key names."""
    lower = {k.lower(): v for k, v in d.items()}
    for n in names:
        if n.lower() in lower:
            return lower[n.lower()]
    return None


def parse_one_bedroom_listings(captured, rendered_text):
    """Return dict: listing_key -> human-readable description."""
    listings = {}

    # --- Primary: structured JSON (RentCafe schemas and lookalikes) ---
    for _url, data in captured:
        for d in _walk(data):
            beds = _get(d, "Beds", "Bedrooms", "BedRooms", "bed", "bedroomcount")
            if beds is None:
                continue
            try:
                beds_n = int(float(str(beds).strip()))
            except (ValueError, TypeError):
                continue
            if beds_n != 1:
                continue

            unit = _get(d, "ApartmentName", "UnitNumber", "Unit", "UnitId")
            plan = _get(d, "FloorplanName", "FloorPlanName", "Name", "PlanName")
            rent = _get(d, "MinimumRent", "MinRent", "Rent", "RentMin", "Price")
            sqft = _get(d, "SQFT", "SquareFeet", "Sqft", "MinimumSQFT")
            avail_date = _get(d, "AvailableDate", "AvailabilityDate",
                              "DateAvailable", "MoveInDate")
            avail_count = _get(d, "AvailableUnitsCount", "AvailableUnits",
                               "UnitsAvailable", "AvailableCount")

            if unit:
                # Unit-level record: one key per physical unit
                key = f"unit:{unit}"
                desc = f"Unit {unit}"
                if plan:
                    desc += f" ({plan})"
            elif plan and avail_count is not None:
                # Floorplan-level record: track availability count changes
                try:
                    n = int(float(str(avail_count)))
                except (ValueError, TypeError):
                    continue
                if n <= 0:
                    continue
                key = f"plan:{plan}:{n}"
                desc = f"Floorplan {plan}: {n} unit(s) available"
            else:
                continue

            if sqft:
                desc += f", {sqft} sqft"
            if rent:
                desc += f", from ${rent}"
            if avail_date:
                desc += f", available {avail_date}"
            listings[key] = desc

    # --- Fallback: rendered page text ---
    if not listings and rendered_text:
        # Look for blocks mentioning "1 Bed" together with availability/pricing
        pattern = re.compile(
            r"([^\n]*1\s*(?:Bed|Bedroom|BD|BR)[^\n]*(?:\n[^\n]*){0,4})",
            re.IGNORECASE,
        )
        for m in pattern.finditer(rendered_text):
            block = m.group(1)
            if re.search(r"\$\s?[\d,]{3,}|available", block, re.IGNORECASE):
                key = "text:" + re.sub(r"\s+", " ", block.strip())[:120]
                listings[key] = re.sub(r"\s+", " ", block.strip())[:200]

    return listings


# ---------------------------------------------------------------------------
# State + notify
# ---------------------------------------------------------------------------

def load_state():
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {"listings": {}, "last_run": None}


def save_state(listings):
    STATE_FILE.write_text(json.dumps({
        "listings": listings,
        "last_run": datetime.now(timezone.utc).isoformat(),
    }, indent=2))


def send_telegram(message):
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        print("[warn] TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID not set; printing only.")
        return
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = json.dumps({
        "chat_id": chat_id,
        "text": message,
        "disable_web_page_preview": True,
    }).encode()
    req = urllib.request.Request(
        url, data=payload, headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        r.read()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    captured, rendered_text = capture_page_data()
    current = parse_one_bedroom_listings(captured, rendered_text)

    print(f"Found {len(current)} current 1BR listing record(s):")
    for desc in current.values():
        print("  -", desc)

    state = load_state()
    previous = state.get("listings", {})
    is_first_run = state.get("last_run") is None

    new_keys = [k for k in current if k not in previous]
    gone_keys = [k for k in previous if k not in current]

    if new_keys and not is_first_run:
        lines = ["🏠 New 1BR at The Martin (Sunnyvale)!"]
        lines += [f"• {current[k]}" for k in new_keys]
        lines.append(f"\n{PAGE_URL}")
        msg = "\n".join(lines)
        print("\n=== ALERT ===\n" + msg)
        send_telegram(msg)
    elif is_first_run:
        print("\nFirst run — baseline saved, no alert sent.")
        if current:
            send_telegram(
                "✅ Martin watcher is live. Current 1BR listings:\n"
                + "\n".join(f"• {d}" for d in current.values())
            )
        else:
            send_telegram(
                "✅ Martin watcher is live. No 1BR listings detected right now "
                "(if you believe some exist, run with --debug and check parsing)."
            )
    else:
        print("\nNo new 1BR listings.")

    if gone_keys:
        print(f"({len(gone_keys)} listing(s) no longer shown.)")

    # Warn loudly if we captured nothing at all — likely blocked or site changed
    if not captured and not current:
        print("[warn] No API JSON captured and no listings parsed. "
              "The site may be blocking headless browsers or changed structure. "
              "Run locally with --debug to inspect.")

    save_state(current)


if __name__ == "__main__":
    main()
