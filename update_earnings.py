#!/usr/bin/env python3
"""
Earnings tab — last 4 NYSE trading days of earnings reports, filtered down to
only the names that BEAT estimates by 20% or more: EPS Surprise % >= 20 OR
Revenue/"Sales" Surprise % >= 20. A big miss (e.g. -76%) does NOT qualify —
only positive beats count.

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
SCREENER_BASE = "https://elite.finviz.com/export/screener"
MOVERS_PATH = SCRIPT_DIR / "docs" / "movers.json"

MIN_SURPRISE_PCT = 20.0
NUM_DAYS = 4

# This guard is effectively dead code now — every trigger arrives as
# workflow_dispatch (from the Cloudflare Worker's scheduler or a manual
# click), and in_run_window() below always lets that through regardless of
# time. Kept in sync with the Worker's SCHEDULES anyway as documentation /
# a safety net should a `schedule:` trigger ever come back. The evening mark
# is 17:45, not 17:00 — update_movers.yml also has a 17:00 mark of its own,
# and two different workflows firing at the same moment can race to commit
# to main at the same time (one workflow's push getting rejected because the
# other just moved main out from under it); see cloudflare-worker.js's
# SCHEDULES comment for the full explanation.
RUN_WINDOWS_ET = [(8, 0), (17, 45)]  # (hour, minute), 24h clock, America/New_York
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


def fetch_quotes(tickers: list[str]) -> dict:
    """Most-recent regular-session close price per ticker, via Finviz Elite's
    screener export restricted to specific symbols (t=TICK1,TICK2,... instead
    of a filter string) — same endpoint/auth Market Maps already uses, just
    targeted at exactly the tickers that qualified today. Best-effort: a
    ticker Finviz doesn't recognize (delisted, OTC, renamed) is simply absent
    from the result rather than an error, and shows as "–" on the page."""
    if not tickers:
        return {}
    quotes = {}
    CHUNK = 100  # defensive cap on symbols per request; Finviz's own limit is higher
    for i in range(0, len(tickers), CHUNK):
        chunk = tickers[i : i + CHUNK]
        url = f"{SCREENER_BASE}?v=111&t={','.join(chunk)}&auth={FINVIZ_TOKEN}"
        try:
            r = requests.get(url, timeout=30)
            r.raise_for_status()
        except requests.RequestException as exc:
            print(f"WARN: quote lookup failed for a chunk of tickers: {exc}", file=sys.stderr)
            continue
        reader = csv.DictReader(io.StringIO(r.text))
        if reader.fieldnames is None or "Ticker" not in reader.fieldnames:
            print("WARN: unexpected response from Finviz quote lookup, skipping chunk", file=sys.stderr)
            continue
        for row in reader:
            t = (row.get("Ticker") or "").strip().upper()
            price = to_float(row.get("Price"))
            if t and price is not None:
                quotes[t] = price
    return quotes


def load_movers_lookup() -> dict:
    """Best-effort cross-reference for the afterhours/BMO move: keyed by
    (TICKER, "YYYY-MM-DD", "premarket"|"afterhours") -> signed % move, built
    from whatever update_movers.py has currently stored in docs/movers.json.
    This only covers tickers that were big enough movers to appear in that
    scrape on that session — most earnings names won't be, and that's
    expected, not a bug; those show as "–" rather than a fabricated number."""
    if not MOVERS_PATH.exists():
        return {}
    try:
        data = json.loads(MOVERS_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    lookup = {}
    for e in data.get("data", []):
        src = e.get("source_page", "")
        if "Premarket" in src:
            session = "premarket"
        elif "Afterhours" in src:
            session = "afterhours"
        else:
            continue
        time_iso = e.get("time_iso")
        symbol = (e.get("symbol") or "").upper()
        if not time_iso or not symbol:
            continue
        try:
            date_str = datetime.fromisoformat(time_iso).date().isoformat()
            chg_pct = float(e.get("chg_pct"))
        except (ValueError, TypeError):
            continue
        # If both the general and "*Earnings"-labeled movers page produced an
        # entry for the same (symbol, date, session), either is the same
        # real move — last one in wins, no need to prefer one over the other.
        lookup[(symbol, date_str, session)] = chg_pct
    return lookup


SESSION_TO_MOVERS_KEY = {"BMO": "premarket", "AMC": "afterhours"}


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

        # Positive beats only — a name that missed estimates (even by a lot)
        # doesn't qualify, only one that beat EPS or Revenue by 20%+.
        hits_eps = eps_surprise is not None and eps_surprise >= MIN_SURPRISE_PCT
        hits_rev = rev_surprise is not None and rev_surprise >= MIN_SURPRISE_PCT
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
            "report_date": date_part,  # kept internally for the movers cross-reference below; stripped before writing
        }
        buckets[date_part].append(entry)

    def sort_key(e):
        mags = [abs(v) for v in (e["eps_surprise"], e["revenue_surprise"]) if v is not None]
        return -(max(mags) if mags else 0)

    all_entries = [e for bucket in buckets.values() for e in bucket]

    # CMP: one bulk Finviz quote lookup for every qualifying ticker across all
    # 4 days, rather than a request per entry.
    tickers = sorted({e["ticker"].upper() for e in all_entries if e["ticker"]})
    quotes = fetch_quotes(tickers)

    # Afterhours/BMO move: cross-reference against whatever update_movers.py
    # currently has stored — see load_movers_lookup()'s docstring for why most
    # entries will legitimately show "–" here.
    movers_lookup = load_movers_lookup()
    for e in all_entries:
        e["cmp"] = quotes.get(e["ticker"].upper())
        movers_key = SESSION_TO_MOVERS_KEY.get(e["session"])
        e["ah_bmo_move"] = (
            movers_lookup.get((e["ticker"].upper(), e["report_date"], movers_key))
            if movers_key
            else None
        )
        del e["report_date"]

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
        print(f"  {d['date']}: {len(d['entries'])} name(s) beating EPS/Revenue by {MIN_SURPRISE_PCT:.0f}%+")

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
