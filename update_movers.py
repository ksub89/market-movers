#!/usr/bin/env python3
"""
Pulls TheStockCatalyst.com premarket/afterhours movers + earnings-movers pages,
filters out anything under $5, and writes the fresh data to docs/movers.json.

CHANGED from the original version: this used to bake the JSON straight into
docs/index.html by string-replacing markers in template.html. It now writes a
small JSON file instead, and index.html (now a static file, committed once)
fetches it client-side. Reason: index.html also has to host the new "Market
Maps" tab, which is updated once a day by a completely separate workflow
(update_market_maps.py / docs/market_maps.json). Two schedules independently
regenerating the *same* full HTML file from a template would each blow away
whatever the other had just written. Two small JSON files updated by two
independent workflows, read by one static page, avoids that entirely.

Run this on a schedule (cron) to keep the feed current.
"""

import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

# ---- CONFIG -----------------------------------------------------------
SCRIPT_DIR = Path(__file__).resolve().parent

# For GitHub Pages: docs/index.html (static, not written by this script) is
# your site's homepage once Pages is enabled with "Deploy from branch: main /docs".
OUTPUT_PATH = SCRIPT_DIR / "docs" / "movers.json"

# Two price/volume bands (OR'd together) instead of one flat cutoff:
#   $5 < price < $58  AND  volume > 300,000   (lower-priced names need more volume to count)
#   price >= $58      AND  volume > 100,000   (higher-priced names qualify on less volume)
PRICE_BAND_LOW = 5.0
PRICE_BAND_HIGH = 58.0
VOLUME_LOW_BAND = 300_000   # required when PRICE_BAND_LOW < price < PRICE_BAND_HIGH
VOLUME_HIGH_BAND = 100_000  # required when price >= PRICE_BAND_HIGH


def passes_price_volume_filter(price: float, volume: int) -> bool:
    if PRICE_BAND_LOW < price < PRICE_BAND_HIGH:
        return volume > VOLUME_LOW_BAND
    if price >= PRICE_BAND_HIGH:
        return volume > VOLUME_HIGH_BAND
    return False  # price <= $5

# Active windows: premarket (every 30 min, ending near the 9:30am open) and
# the late-day/afterhours window (every 30 min, starting just ahead of the
# 4:00pm close and running through early evening). Outside these marks the
# feed simply stays as it last was — nothing runs, nothing to commit — which
# is the "maintain the feed" behavior for the rest of the day/night.
#
# This replaces the old around-the-clock */5 schedule (288 runs/day), which
# is almost certainly why the feed went stale for hours at a time on a
# private repo: GitHub Free's 2,000 Actions-minutes/month cap gets burned
# through fast at that rate. ~11 real runs/day fixes that outright.
RUN_MARKS_ET = [
    (7, 15), (7, 45), (8, 15), (8, 45), (9, 15),          # premarket
    (15, 30), (16, 0), (16, 30), (17, 0), (17, 30), (18, 0),  # afterhours
]
RUN_TOLERANCE_MIN = 10

PAGES = {
    "https://www.thestockcatalyst.com/NYSEPMMovers": "Premarket",
    "https://www.thestockcatalyst.com/NYSEAHMovers": "Afterhours",
    "https://www.thestockcatalyst.com/NasdaqPMEarningsMovers": "Premarket Earnings",
    "https://www.thestockcatalyst.com/NasdaqAHEarningsMovers": "Afterhours Earnings",
}

HEADERS = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"}

TIME_RE = re.compile(r"\[(\d{1,2}/\d{1,2}/\d{4}\s+\d{1,2}:\d{2}\s*[AP]M)\]\s*$")


def in_run_window() -> bool:
    # Manual runs (workflow_dispatch) always proceed regardless of time of day.
    if os.environ.get("GITHUB_EVENT_NAME") == "workflow_dispatch":
        return True
    now = datetime.now(ZoneInfo("America/New_York"))
    now_minutes = now.hour * 60 + now.minute
    for h, m in RUN_MARKS_ET:
        if abs(now_minutes - (h * 60 + m)) <= RUN_TOLERANCE_MIN:
            return True
    return False


def fetch(url: str) -> str:
    r = requests.get(url, headers=HEADERS, timeout=20)
    r.raise_for_status()
    return r.text


def parse_page(html: str, label: str):
    soup = BeautifulSoup(html, "lxml")
    entries = []
    for grid_name, direction in [("TopGaining", "up"), ("TopLosing", "down")]:
        grid = soup.find("div", attrs={"data-name": grid_name})
        if not grid:
            continue
        tbody = grid.find("tbody")
        if not tbody:
            continue
        for row in tbody.find_all("tr"):
            tds = row.find_all("td")
            if len(tds) < 6:
                continue
            chg_span = tds[0].find(
                "span", class_=lambda c: c and ("text-success" in c or "text-danger" in c)
            )
            if not chg_span:
                continue
            m = re.match(r"([\-\d.]+)\s*\(([\-\d.]+)%\)", chg_span.get_text(" ", strip=True))
            if not m:
                continue
            chg_pct = m.group(2)

            price_a = tds[1].find("a")
            price_text = price_a.get_text(strip=True) if price_a else tds[1].get_text(strip=True)
            try:
                price = float(price_text)
            except ValueError:
                continue

            sym_a = tds[2].find("a")
            symbol = sym_a.get_text(strip=True) if sym_a else tds[2].get_text(strip=True)
            name = tds[3].get_text(strip=True)

            vol_text = tds[4].get_text(strip=True).replace(",", "")
            try:
                volume = int(float(vol_text))
            except ValueError:
                continue

            if not passes_price_volume_filter(price, volume):
                continue

            for a in tds[5].find_all("a"):
                text = a.get_text(" ", strip=True)
                href = a.get("href")
                mt = TIME_RE.search(text)
                if not mt:
                    continue
                ts = mt.group(1)
                headline = text[: mt.start()].strip()
                # TheStockCatalyst.com's [MM/DD/YYYY H:MM AM/PM] timestamps are
                # in UTC (verified against a linked article's own stated
                # publish time, which was 4 hours behind what we were
                # displaying — exactly the EDT/UTC offset). Attach an
                # explicit UTC tzinfo here so downstream consumers (this
                # script's own generated_at, and the page's JS) do a real
                # timezone conversion instead of guessing.
                dt_utc = datetime.strptime(ts, "%m/%d/%Y %I:%M %p").replace(tzinfo=timezone.utc)
                entries.append(
                    {
                        "source_page": label,
                        "direction": direction,
                        "symbol": symbol,
                        "name": name,
                        "price": price,
                        "volume": volume,
                        "chg_pct": chg_pct,
                        "headline": headline,
                        "url": href,
                        "dt": dt_utc,
                    }
                )
    return entries


# How long a scraped entry stays in the feed after its own timestamp before
# it's pruned, even if nothing fresher for that (symbol, source_page,
# direction) has come in. Needs to comfortably survive the longest gap
# between active windows: Friday's 3:30-6pm afterhours run through to
# Monday's 7:15am premarket run is ~61 hours, so this gives real margin for
# a 3-day weekend (or a Monday holiday) without dropping Friday's feed early.
PRUNE_AFTER_HOURS = 96


def load_existing() -> dict:
    """Entries already in docs/movers.json, keyed the same way build_dataset
    keys fresh scrapes, with "dt" parsed back to a real datetime for merge
    comparisons. Each run OVERWRITES movers.json, so without this, anything
    the live site isn't showing at THIS exact moment (e.g. yesterday's 6pm
    afterhours movers, once it's past the 3:30-6pm window and the site's own
    grids have moved on) would vanish from the feed the next time a premarket
    run fires — even though nothing's actually "wrong", there's just no fresh
    data for that slot yet. Merging fresh scrapes into what's already stored
    (newest wins per key) keeps last session's entries visible until this
    session's actually replaces them."""
    if not OUTPUT_PATH.exists():
        return {}
    try:
        stored = json.loads(OUTPUT_PATH.read_text(encoding="utf-8")).get("data", [])
    except (json.JSONDecodeError, OSError):
        return {}
    existing = {}
    for e in stored:
        try:
            dt = datetime.fromisoformat(e["time_iso"])
            if dt.tzinfo is None:  # guard against any pre-UTC-tagging legacy entries
                dt = dt.replace(tzinfo=timezone.utc)
        except (KeyError, ValueError):
            continue
        key = (e["symbol"], e["source_page"], e["direction"])
        existing[key] = {**e, "dt": dt}
    return existing


def build_dataset():
    all_entries = []
    for url, label in PAGES.items():
        try:
            html = fetch(url)
        except Exception as exc:  # noqa: BLE001
            print(f"WARN: failed to fetch {url}: {exc}", file=sys.stderr)
            continue
        all_entries.extend(parse_page(html, label))

    # keep only the most recent headline per (symbol, source_page, direction)
    # out of what THIS run just scraped
    fresh = {}
    for e in all_entries:
        key = (e["symbol"], e["source_page"], e["direction"])
        if key not in fresh or e["dt"] > fresh[key]["dt"]:
            fresh[key] = e

    # merge with whatever was already stored — newest timestamp wins per key,
    # so a quiet run (nothing new on the live page right now) doesn't erase
    # entries from the last run that fetched something real
    merged = load_existing()
    for key, e in fresh.items():
        if key not in merged or e["dt"] > merged[key]["dt"]:
            merged[key] = e

    cutoff = datetime.now(timezone.utc) - timedelta(hours=PRUNE_AFTER_HOURS)
    final = [e for e in merged.values() if e["dt"] >= cutoff]
    final.sort(key=lambda x: x["dt"], reverse=True)

    for e in final:
        e["time_iso"] = e["dt"].isoformat()
        del e["dt"]

    return final


def main():
    if not in_run_window():
        now = datetime.now(ZoneInfo("America/New_York"))
        print(
            f"Skipping: {now.strftime('%H:%M %Z')} is not within {RUN_TOLERANCE_MIN} min of "
            f"a scheduled run mark — this is the every-15-min poller finding nothing to do, "
            f"not an error."
        )
        return

    data = build_dataset()
    generated_at = datetime.now(timezone.utc).isoformat()
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(
        json.dumps({"generated_at": generated_at, "data": data}, ensure_ascii=False),
        encoding="utf-8",
    )
    print(f"{datetime.now().isoformat(timespec='seconds')}  wrote {len(data)} entries to {OUTPUT_PATH}")


if __name__ == "__main__":
    main()
