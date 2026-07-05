"""
The Martin (Sunnyvale) apartment availability watcher — v2.

What it does:
  * Watches ALL bed types (studio / 1BR / 2BR / 3BR).
  * Alerts when a NEW listing appears.
  * Alerts when a previously-seen listing DISAPPEARS (leased / taken down).
  * Enriches alerts with facing direction + balcony from unit_features.json
    (a manual one-time mapping you build from the property sitemap).
  * Classifies 1BRs as "full-size" vs "studio-style" by square footage.

Usage:
  python watcher.py            # normal run
  python watcher.py --debug    # dump captured API JSON + rendered text

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
BASE_DIR = Path(__file__).parent
STATE_FILE = BASE_DIR / "state.json"
FEATURES_FILE = BASE_DIR / "unit_features.json"
DEBUG = "--debug" in sys.argv

# 1BRs at or below this square footage get labeled "studio-style 1BR".
# Tune to taste; ~650 sqft is a reasonable cutoff for a compact 1BR.
SMALL_1BR_SQFT = 650


# ---------------------------------------------------------------------------
# Capture: render page and collect JSON API responses
# ---------------------------------------------------------------------------

def capture_page_data():
    captured = []
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
                          "apartment", "unit", "wp-json", "sightmap", "engrain")
            )
            if "json" in ctype or looks_relevant:
                try:
                    captured.append((url, json.loads(resp.text())))
                except Exception:
                    pass

        page.on("response", on_response)
        page.goto(PAGE_URL, wait_until="networkidle", timeout=60_000)
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
# Parse: extract ALL listings (every bed count)
# ---------------------------------------------------------------------------

def _walk(obj):
    if isinstance(obj, dict):
        yield obj
        for v in obj.values():
            yield from _walk(v)
    elif isinstance(obj, list):
        for item in obj:
            yield from _walk(item)


def _get(d, *names):
    lower = {k.lower(): v for k, v in d.items()}
    for n in names:
        if n.lower() in lower:
            return lower[n.lower()]
    return None


def _bed_label(beds_n):
    return "Studio" if beds_n == 0 else f"{beds_n}BR"


def parse_listings(captured, rendered_text):
    """Return dict: key -> listing dict with structured fields."""
    listings = {}

    for _url, data in captured:
        for d in _walk(data):
            beds = _get(d, "Beds", "Bedrooms", "BedRooms", "bed", "bedroomcount")
            if beds is None:
                continue
            try:
                beds_n = int(float(str(beds).strip()))
            except (ValueError, TypeError):
                continue
            if beds_n < 0 or beds_n > 5:
                continue

            unit = _get(d, "ApartmentName", "UnitNumber", "Unit", "UnitId")
            plan = _get(d, "FloorplanName", "FloorPlanName", "Name", "PlanName")
            rent = _get(d, "MinimumRent", "MinRent", "Rent", "RentMin", "Price")
            sqft = _get(d, "SQFT", "SquareFeet", "Sqft", "MinimumSQFT")
            avail_date = _get(d, "AvailableDate", "AvailabilityDate",
                              "DateAvailable", "MoveInDate")
            avail_count = _get(d, "AvailableUnitsCount", "AvailableUnits",
                               "UnitsAvailable", "AvailableCount")
            # Defensive: capture balcony/amenity text if the feed ever has it
            amenities = _get(d, "Amenities", "AmenityList", "UnitAmenities",
                             "Features")

            if unit:
                key = f"unit:{unit}"
                rec = {
                    "unit": str(unit), "plan": plan, "beds": beds_n,
                    "sqft": _num(sqft), "rent": _num(rent),
                    "avail_date": str(avail_date) if avail_date else None,
                }
                if amenities:
                    txt = json.dumps(amenities).lower()
                    if "balcony" in txt or "patio" in txt or "terrace" in txt:
                        rec["balcony_from_feed"] = True
                listings[key] = rec
            elif plan and avail_count is not None:
                try:
                    n = int(float(str(avail_count)))
                except (ValueError, TypeError):
                    continue
                if n <= 0:
                    continue
                key = f"plan:{plan}:{n}"
                listings[key] = {
                    "unit": None, "plan": str(plan), "beds": beds_n,
                    "sqft": _num(sqft), "rent": _num(rent),
                    "avail_date": None, "plan_count": n,
                }

    # Fallback: rendered text (any bed count)
    if not listings and rendered_text:
        pattern = re.compile(
            r"([^\n]*(?:Studio|\d\s*(?:Bed|Bedroom|BD|BR))[^\n]*(?:\n[^\n]*){0,4})",
            re.IGNORECASE,
        )
        for m in pattern.finditer(rendered_text):
            block = m.group(1)
            if re.search(r"\$\s?[\d,]{3,}|available", block, re.IGNORECASE):
                clean = re.sub(r"\s+", " ", block.strip())[:200]
                listings["text:" + clean[:120]] = {"unit": None, "plan": None,
                                                   "beds": None, "text": clean}
    return listings


def _num(v):
    if v is None:
        return None
    try:
        return float(re.sub(r"[^\d.]", "", str(v)))
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Enrichment: facing direction / balcony from manual sitemap mapping
# ---------------------------------------------------------------------------

def load_unit_features():
    if FEATURES_FILE.exists():
        return json.loads(FEATURES_FILE.read_text())
    return {}


def lookup_features(unit, features):
    """Match a unit number against unit_features.json.

    Facing: exact match in facing_by_unit wins, else stack suffix
    (last 2 digits) in facing_by_stack — stacks are consistent vertically.
    Balcony: exact per-unit list (balconies vary floor to floor). Only
    reported for floors listed in balcony_floors_mapped; on unmapped
    floors the balcony field is omitted (unknown), not "no balcony".
    """
    if not unit or not features:
        return {}
    u = str(unit).strip()
    out = {}

    m = re.search(r"(\d{2})$", u)
    stack = m.group(1) if m else None
    facing = (features.get("facing_by_unit", {}).get(u)
              or (features.get("facing_by_stack", {}).get(stack) if stack else None))
    if facing:
        out["facing"] = facing

    floor = u[:-2] if len(u) > 2 else None
    mapped_floors = {str(f) for f in features.get("balcony_floors_mapped", [])}
    if floor and floor in mapped_floors:
        out["balcony"] = u in set(features.get("balcony_units", []))
    return out


def describe(rec, features):
    """Human-readable one-liner for a listing record."""
    if "text" in rec:
        return rec["text"]

    beds_n = rec.get("beds")
    parts = []

    if rec.get("unit"):
        head = f"Unit {rec['unit']}"
        if rec.get("plan"):
            head += f" ({rec['plan']})"
        parts.append(head)
    elif rec.get("plan_count"):
        parts.append(f"Floorplan {rec['plan']}: {rec['plan_count']} unit(s)")

    if beds_n is not None:
        label = _bed_label(beds_n)
        # 1BR size classification (inferred from sqft)
        if beds_n == 1 and rec.get("sqft"):
            kind = ("studio-style 1BR" if rec["sqft"] <= SMALL_1BR_SQFT
                    else "full-size 1BR")
            label = f"{label} [{kind}]"
        parts.append(label)

    if rec.get("sqft"):
        parts.append(f"{int(rec['sqft'])} sqft")
    if rec.get("rent"):
        parts.append(f"from ${int(rec['rent']):,}")
    if rec.get("avail_date"):
        parts.append(f"available {rec['avail_date']}")

    feat = lookup_features(rec.get("unit"), features)
    if feat.get("facing"):
        parts.append(f"facing {feat['facing']}")
    if feat.get("balcony") is True or rec.get("balcony_from_feed"):
        parts.append("balcony ✓")
    elif feat.get("balcony") is False:
        parts.append("no balcony")

    return ", ".join(parts)


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


def _prev_desc(v, features):
    """Describe a previous-state entry (handles old string-format state)."""
    return v if isinstance(v, str) else describe(v, features)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    features = load_unit_features()
    if not features:
        print("[info] unit_features.json not found or empty — alerts will omit "
              "facing/balcony. Fill it in from the property sitemap when ready.")

    captured, rendered_text = capture_page_data()
    current = parse_listings(captured, rendered_text)

    print(f"Found {len(current)} current listing record(s):")
    for rec in current.values():
        print("  -", describe(rec, features))

    state = load_state()
    previous = state.get("listings", {})
    is_first_run = state.get("last_run") is None

    new_keys = [k for k in current if k not in previous]
    gone_keys = [k for k in previous if k not in current]

    messages = []
    if not is_first_run:
        if new_keys:
            lines = ["🏠 New listing(s) at The Martin!"]
            lines += [f"• {describe(current[k], features)}" for k in new_keys]
            messages.append("\n".join(lines))
        if gone_keys:
            lines = ["📤 No longer available (likely leased):"]
            lines += [f"• {_prev_desc(previous[k], features)}" for k in gone_keys]
            messages.append("\n".join(lines))

    if messages:
        msg = "\n\n".join(messages) + f"\n\n{PAGE_URL}"
        print("\n=== ALERT ===\n" + msg)
        send_telegram(msg)
    elif is_first_run:
        print("\nFirst run — baseline saved.")
        body = ("✅ Martin watcher v2 is live. Tracking ALL unit types.\n"
                "Current listings:\n"
                + "\n".join(f"• {describe(r, features)}" for r in current.values())
                if current else
                "✅ Martin watcher v2 is live. No listings detected right now.")
        send_telegram(body)
    else:
        print("\nNo changes.")

    if not captured and not current:
        print("[warn] No API JSON captured and no listings parsed. "
              "Site may be blocking headless browsers or changed structure. "
              "Run locally with --debug to inspect.")

    save_state(current)


if __name__ == "__main__":
    main()
