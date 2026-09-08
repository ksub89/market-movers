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
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

# ---- CONFIG -----------------------------------------------------------
SCRIPT_DIR = Path(__file__).resolve().parent

# For GitHub Pages: docs/index.html (static, not written by this script) is
# your site's homepage once Pages is enabled with "Deploy from branch: main /docs".
OUTPUT_PATH = SCRIPT_DIR / "docs" / "movers.json"

MIN_PRICE = 5.0
MIN_VOLUME = 300_000

PAGES = {
    "https://www.thestockcatalyst.com/NYSEPMMovers": "Premarket",
    "https://www.thestockcatalyst.com/NYSEAHMovers": "Afterhours",
    "https://www.thestockcatalyst.com/NasdaqPMEarningsMovers": "Premarket Earnings",
    "https://www.thestockcatalyst.com/NasdaqAHEarningsMovers": "Afterhours Earnings",
}

HEADERS = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"}

TIME_RE = re.compile(r"\[(\d{1,2}/\d{1,2}/\d{4}\s+\d{1,2}:\d{2}\s*[AP]M)\]\s*$")


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
            if price < MIN_PRICE:
                continue

            sym_a = tds[2].find("a")
            symbol = sym_a.get_text(strip=True) if sym_a else tds[2].get_text(strip=True)
            name = tds[3].get_text(strip=True)

            vol_text = tds[4].get_text(strip=True).replace(",", "")
            try:
                volume = int(float(vol_text))
            except ValueError:
                continue
            if volume <= MIN_VOLUME:
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
    best = {}
    for e in all_entries:
        key = (e["symbol"], e["source_page"], e["direction"])
        if key not in best or e["dt"] > best[key]["dt"]:
            best[key] = e

    final = list(best.values())
    final.sort(key=lambda x: x["dt"], reverse=True)

    for e in final:
        e["time_iso"] = e["dt"].isoformat()
        del e["dt"]

    return final


def main():
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
