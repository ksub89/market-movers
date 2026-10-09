#!/usr/bin/env python3
"""
Earnings tab — last 4 NYSE trading days of earnings reports, filtered down to
only the names with a big surprise: abs(EPS Surprise %) > 20 OR
abs(Revenue/"Sales" Surprise %) > 20.

Data source: Finviz Elite's earnings-calendar export
(https://elite.finviz.com/export/calendar/earnings?dateFrom=...&dateTo=...&auth=...).
That endpoint already returns "EPS Surprise" and "Revenue Surprise" as percentages
per report, so no screener trickery is needed to combine the two with OR logic —
Finviz's screener filter string only supports AND between criteria, but here
we're not using the screener at all, just filtering the calendar rows ourselves
in Python, so EPS-or-Revenue is trivial.

Writes docs/earnings.json: one bucket per trading day (today + the 3 trading
days before it, most-recent first), each holding only the qualifying rows for
that day. A day with zero qualifying names still gets an (empty) entry so the
page can show its tab.

Run this once per trading day, after the close — same timing as
update_market_maps.py — so both the pre-market (BMO) and after-hours (AMC)
reports for "today" are already posted by the time this runs. Independent
output file (docs/earnings.json), so it can't collide with update_movers.py's
or update_market_maps.py's own files/schedules.
"""

import csv
import io
import os
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas_market_calendars as mcal
import requests

# ---- CONFIG -------------------------------------------------------------
SCRIPT_DIR = Path(__file__).resolve().parent
OUTPUT_PATH = SCRIPT_DIR / "docs" / "earnings.json"

FINVIZ_TOKEN = os.environ.get("FINVIZ_API_TOKEN")
if not FINVIZ_TOKEN:
    print("ERROR: FINVIZ_API_TOKEN environment variable is not set.", file=sys.stderr)
    sys.exit(1)

BASE = "https://elite.finviz.com/export/calendar/earnings"

MIN_SURPRISE_PCT = 20.0
NUM_DAYS = 4

# The workflow fires 4x/day (not 2x) because a GitHub Actions cron schedule is
# a fixed UTC time with no DST awareness, so "8am ET" and "5pm ET" each need
# two cron lines — one for EDT, one for EST — to land at the right wall-clock
# time year-round. Only one of each pair is ever actually correct at a given
# time of year; the other fires an hour off. This guard makes the "wrong"
# firing a harmless no-op (exits before touching the file) instead of writing
# a redundant/misleading update, so docs/earnings.json only ever actually
# changes at true 8am ET and true 5pm ET (BMO and AMC), Mon-Fri.
RUN_WINDOWS_ET = [(8, 0), (17, 0)]  # (hour, minute), 24h clock, America/New_York
RUN_TOLERANCE_MIN = 20

NYSE = mcal.get_calendar("NYSE")


def in_run_window() -> bool:
    # Manual runs (workflow_dispatch) always proceed regardless of time of day.
    if os.environ.get("GITHUB_EVENT_NAME") == "workflow_dispatch":
        return True
    now = datetime.now(ZoneInfo("America/New_York"))
    now_minutes = now.hour * 60 + now.minute
    for h, m in RUN_WINDOWS_ET:
        if abs(now_minutes - (h * 60 + m)) <= RUN_TOLERANCE_MIN:
            return True
    return False


def last_n_trading_days(n: int):
    """Last n actual NYSE trading sessions as of right now (ET), most-recent
    last — so [oldest, ..., newest]. Mirrors update_market_maps.py's approach:
    weekends/holidays are resolved by the exchange calendar, not hardcoded."""
    today = datetime.now(ZoneInfo("America/New_York")).date()
    sched = NYSE.schedule(start_date=today - timedelta(days=n + 10), end_date=today)
    if len(sched.index) < n:
        raise RuntimeError(
            f"pandas_market_calendars found fewer than {n} NYSE trading days "
            f"in the window up to {today} — that shouldn't happen."
        )
    return [d.date() for d in sched.index[-n:]]


def fetch_calendar(date_from: str, date_to: str) -> list[dict]:
    url = f"{BASE}?dateFrom={date_from}&dateTo={date_to}&auth={FINVIZ_TOKEN}"
    r = requests.get(url, timeout=30)
    r.raise_for_status()
    text = r.text
    reader = csv.DictReader(io.StringIO(text))
    rows = list(reader)
    if not rows or reader.fieldnames is None or "Ticker" not in reader.fieldnames:
        raise RuntimeError(
            f"Unexpected response from Finviz earnings calendar (not the "
            f"expected CSV — check the auth token). First 200 chars: {text[:200]!r}"
        )
    return rows


def to_float(s):
    s = (s or "").strip()
    if not s:
        return None
    try:
        return float(s)
    except ValueError:
        return None


def session_label(time_str: str) -> str:
    if time_str == "08:30":
        return "BMO"
    if time_str == "16:30":
        return "AMC"
    return time_str or "—"


def build_days():
    trading_days = last_n_trading_days(NUM_DAYS)  # oldest -> newest
    date_from = trading_days[0].strftime("%Y-%m-%d")
    date_to = trading_days[-1].strftime("%Y-%m-%d")

    rows = fetch_calendar(date_from, date_to)

    buckets = {d.strftime("%Y-%m-%d"): [] for d in trading_days}

    for row in rows:
        raw_dt = (row.get("Date") or "").strip()  # "YYYY-MM-DD HH:MM"
        if not raw_dt:
            continue
        date_part, _, time_part = raw_dt.partition(" ")
        if date_part not in buckets:
            continue  # outside our trading-day window (shouldn't happen given dateFrom/dateTo)

        eps_surprise = to_float(row.get("EPS Surprise"))
        rev_surprise = to_float(row.get("Revenue Surprise"))

        hits_eps = eps_surprise is not None and abs(eps_surprise) > MIN_SURPRISE_PCT
        hits_rev = rev_surprise is not None and abs(rev_surprise) > MIN_SURPRISE_PCT
        if not (hits_eps or hits_rev):
            continue

        entry = {
            "ticker": row.get("Ticker", "").strip(),
            "company": row.get("Company", "").strip(),
            "session": session_label(time_part.strip()),
            "report_time": time_part.strip(),
            "market_cap": to_float(row.get("Market Cap")),
            "eps_estimate": to_float(row.get("EPS Estimate")),
            "eps_actual": to_float(row.get("EPS Actual")),
            "eps_surprise": eps_surprise,
            "revenue_estimate": to_float(row.get("Revenue Estimate")),
            "revenue_actual": to_float(row.get("Revenue Actual")),
            "revenue_surprise": rev_surprise,
            "price_reaction": to_float(row.get("1-Day Price Reaction")),
        }
        buckets[date_part].append(entry)

    def sort_key(e):
        mags = [abs(v) for v in (e["eps_surprise"], e["revenue_surprise"]) if v is not None]
        return -(max(mags) if mags else 0)

    days = []
    for d in reversed(trading_days):  # most recent first
        key = d.strftime("%Y-%m-%d")
        entries = sorted(buckets[key], key=sort_key)
        days.append({"date": key, "entries": entries})

    return days


def main():
    if not in_run_window():
        now = datetime.now(ZoneInfo("America/New_York"))
        print(
            f"Skipping: {now.strftime('%H:%M %Z')} is not within {RUN_TOLERANCE_MIN} min of "
            f"a scheduled run time ({RUN_WINDOWS_ET} ET) — this is the DST-offset cron "
            f"firing a no-op, not an error."
        )
        return

    print("Fetching Finviz Elite earnings calendar...")
    days = build_days()
    for d in days:
        print(f"  {d['date']}: {len(d['entries'])} name(s) with >{MIN_SURPRISE_PCT:.0f}% EPS/Revenue surprise")

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(
        json.dumps(
            {
                "generated_at": datetime.now(ZoneInfo("America/New_York")).isoformat(),
                "min_surprise_pct": MIN_SURPRISE_PCT,
                "days": days,
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    total = sum(len(d["entries"]) for d in days)
    print(f"Wrote {len(days)} day(s), {total} qualifying name(s) total, to {OUTPUT_PATH}")


if __name__ == "__main__":
    main()
