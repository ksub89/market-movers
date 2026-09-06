#!/usr/bin/env python3
"""
Market Maps — daily US-stock breadth snapshot (% of stocks above/below their
5/10/20/50-day moving averages), built from Finviz Elite screener exports.

Universe for every count below: US common stock, price > $3, volume > 100,000
shares (the same base filter is baked into every one of the 9 Finviz URLs, so
numerator and denominator are always measuring the same population).

Run this once per trading day, after the close (see the accompanying
workflow — it fires at 22:00 UTC, which is after 4pm ET in both EST and EDT).
It appends one row per trading day to docs/market_maps.json; it does NOT
touch docs/index.html or docs/movers.json, so it can run on a completely
independent schedule from update_movers.py without clobbering it.
"""

import csv
import io
import json
import os
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

# ---- CONFIG -------------------------------------------------------------
SCRIPT_DIR = Path(__file__).resolve().parent
OUTPUT_PATH = SCRIPT_DIR / "docs" / "market_maps.json"

FINVIZ_TOKEN = os.environ.get("FINVIZ_API_TOKEN")
if not FINVIZ_TOKEN:
    print("ERROR: FINVIZ_API_TOKEN environment variable is not set.", file=sys.stderr)
    sys.exit(1)

BASE = "https://elite.finviz.com/export/screener"

# Every filter string already includes the shared base criteria:
#   sh_curvol_o100  -> current volume > 100,000
#   sh_price_o3     -> price > $3
# so each MA count and the universe count are measured over the identical
# population.
FILTERS = {
    "universe": "sh_curvol_o100,sh_price_o3",
    "above_5": "sh_curvol_o100,sh_price_o3,tad_0_sma:5:sma:d|abv:::1|close::close:d",
    "below_5": "sh_curvol_o100,sh_price_o3,tad_0_sma:5:sma:d|blw:::1|close::close:d",
    "above_10": "sh_curvol_o100,sh_price_o3,tad_0_sma:10:sma:d|abv:::1|close::close:d",
    "below_10": "sh_curvol_o100,sh_price_o3,tad_0_sma:10:sma:d|blw:::1|close::close:d",
    "above_20": "sh_curvol_o100,sh_price_o3,ta_sma20_pa",
    "below_20": "sh_curvol_o100,sh_price_o3,ta_sma20_pb",
    "above_50": "sh_curvol_o100,sh_price_o3,ta_sma50_pa",
    "below_50": "sh_curvol_o100,sh_price_o3,ta_sma50_pb",
}


def fetch_count(filter_str: str) -> int:
    """Hit one Finviz Elite screener export and return the number of matching rows."""
    url = f"{BASE}?v=111&f={filter_str}&ft=4&auth={FINVIZ_TOKEN}"
    r = requests.get(url, timeout=30)
    r.raise_for_status()
    text = r.text

    reader = csv.reader(io.StringIO(text))
    rows = list(reader)
    if not rows or "Ticker" not in rows[0]:
        raise RuntimeError(
            f"Unexpected response from Finviz (not a CSV export — check the auth "
            f"token / filter string). First 200 chars: {text[:200]!r}"
        )
    return len(rows) - 1  # minus header row


def build_snapshot():
    counts = {}
    for key, filt in FILTERS.items():
        counts[key] = fetch_count(filt)
        print(f"  {key}: {counts[key]}")

    total = counts["universe"]
    if total == 0:
        raise RuntimeError("Universe count came back as 0 — aborting, something is wrong.")

    def pct(n):
        return round(100.0 * n / total, 2)

    trading_date = datetime.now(ZoneInfo("America/New_York")).strftime("%Y-%m-%d")

    return {
        "date": trading_date,
        "universe": total,
        "pct_above_5": pct(counts["above_5"]),
        "pct_below_5": pct(counts["below_5"]),
        "pct_above_10": pct(counts["above_10"]),
        "pct_below_10": pct(counts["below_10"]),
        "pct_above_20": pct(counts["above_20"]),
        "pct_below_20": pct(counts["below_20"]),
        "pct_above_50": pct(counts["above_50"]),
        "pct_below_50": pct(counts["below_50"]),
        "counts": counts,
    }


def load_history():
    if OUTPUT_PATH.exists():
        try:
            return json.loads(OUTPUT_PATH.read_text(encoding="utf-8")).get("history", [])
        except (json.JSONDecodeError, OSError):
            return []
    return []


def is_same_as_last(new_row, history):
    """Cheap holiday/duplicate guard: if every % matches the most recent stored
    row exactly, Finviz almost certainly hasn't rolled to a new session (e.g.
    the workflow fired on a market holiday) — skip adding a duplicate row."""
    if not history:
        return False
    last = history[-1]
    keys = [k for k in new_row if k.startswith("pct_")]
    return last["date"] != new_row["date"] and all(last.get(k) == new_row.get(k) for k in keys)


def main():
    print("Fetching Finviz Elite breadth counts...")
    snapshot = build_snapshot()
    history = load_history()

    if is_same_as_last(snapshot, history):
        print(
            f"Skipping {snapshot['date']}: percentages identical to last stored day "
            f"({history[-1]['date']}) — likely a non-trading day."
        )
        return

    history = [row for row in history if row["date"] != snapshot["date"]]
    history.append(snapshot)
    history.sort(key=lambda r: r["date"])

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(
        json.dumps(
            {
                "generated_at": datetime.now(ZoneInfo("America/New_York")).isoformat(),
                "history": history,
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"Wrote {len(history)} day(s) of history to {OUTPUT_PATH}, latest: {snapshot['date']}")


if __name__ == "__main__":
    main()
