#!/usr/bin/env python3
"""
Pulls TheStockCatalyst.com premarket/afterhours movers + earnings-movers pages,
filters out anything under $5, and rebuilds market_movers_feed.html with the
fresh data baked in.

Run this on a schedule (cron) to keep the HTML file current.
"""

import json
import re
import sys
from datetime import datetime
from pathlib import Path

import requests
from bs4 import BeautifulSoup

# ---- CONFIG -----------------------------------------------------------
SCRIPT_DIR = Path(__file__).resolve().parent
TEMPLATE_PATH = SCRIPT_DIR / "template.html"

# For GitHub Pages: this writes to docs/index.html, which Pages serves as your
# site's homepage once Pages is enabled with "Deploy from branch: main /docs".
OUTPUT_PATH = SCRIPT_DIR / "docs" / "index.html"

MIN_PRICE = 5.0

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

            for a in tds[5].find_all("a"):
                text = a.get_text(" ", strip=True)
                href = a.get("href")
                mt = TIME_RE.search(text)
                if not mt:
                    continue
                ts = mt.group(1)
                headline = text[: mt.start()].strip()
                entries.append(
                    {
                        "source_page": label,
                        "direction": direction,
                        "symbol": symbol,
                        "name": name,
                        "price": price,
                        "chg_pct": chg_pct,
                        "headline": headline,
                        "url": href,
                        "dt": datetime.strptime(ts, "%m/%d/%Y %I:%M %p"),
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
    template = TEMPLATE_PATH.read_text(encoding="utf-8")
    html = template.replace("__DATA_JSON__", json.dumps(data, ensure_ascii=False))
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(html, encoding="utf-8")
    print(f"{datetime.now().isoformat(timespec='seconds')}  wrote {len(data)} entries to {OUTPUT_PATH}")


if __name__ == "__main__":
    main()
