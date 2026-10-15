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

import pandas_market_calendars as mcal
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
PREMARKET_MARKS_ET = [(7, 15), (7, 45), (8, 15), (8, 28), (8, 45), (9, 15)]
AFTERHOURS_MARKS_ET = [(15, 30), (16, 0), (16, 30), (17, 0), (17, 30), (18, 0)]
RUN_MARKS_ET = PREMARKET_MARKS_ET + AFTERHOURS_MARKS_ET
RUN_TOLERANCE_MIN = 10

PREMARKET_PAGES = {
    "https://www.thestockcatalyst.com/NYSEPMMovers": "Premarket",
    "https://www.thestockcatalyst.com/NasdaqPMEarningsMovers": "Premarket Earnings",
}
AFTERHOURS_PAGES = {
    "https://www.thestockcatalyst.com/NYSEAHMovers": "Afterhours",
    "https://www.thestockcatalyst.com/NasdaqAHEarningsMovers": "Afterhours Earnings",
}
PAGES = {**PREMARKET_PAGES, **AFTERHOURS_PAGES}


def _near_any(marks, now_minutes: int, tolerance: int) -> bool:
    return any(abs(now_minutes - (h * 60 + m)) <= tolerance for h, m in marks)


def pages_to_scrape() -> dict:
    """Which TheStockCatalyst leaderboard(s) to fetch THIS run.

    Previously every run fetched all 4 pages regardless of what time it was —
    a premarket run would also pull the Afterhours page, and an afterhours
    run would also pull the Premarket page. That's what caused a headline
    timestamped 9:25am (well into the regular session) to show up on the site
    tagged "Afterhours": the row was really on TheStockCatalyst's Afterhours
    leaderboard, which doesn't stop showing/updating just because it's now
    morning, so a premarket-window run innocently scraped it and inherited
    whatever headline that leaderboard happened to have attached at that
    moment — not necessarily anything that actually happened after hours.

    Restricting each run to only the page(s) that match its own window fixes
    that at the source: a premarket run only ever writes "Premarket"/
    "Premarket Earnings" entries, an afterhours run only ever writes
    "Afterhours"/"Afterhours Earnings" entries. A run outside either window
    (e.g. the website's manual refresh button clicked off-schedule) has no
    session to infer, so it falls back to fetching everything, same as this
    script's original behavior."""
    now = datetime.now(ZoneInfo("America/New_York"))
    now_minutes = now.hour * 60 + now.minute
    is_premarket = _near_any(PREMARKET_MARKS_ET, now_minutes, RUN_TOLERANCE_MIN)
    is_afterhours = _near_any(AFTERHOURS_MARKS_ET, now_minutes, RUN_TOLERANCE_MIN)
    if is_premarket and not is_afterhours:
        return PREMARKET_PAGES
    if is_afterhours and not is_premarket:
        return AFTERHOURS_PAGES
    return PAGES  # neither window (manual/off-schedule run) — fetch everything

HEADERS = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"}

TIME_RE = re.compile(r"\[(\d{1,2}/\d{1,2}/\d{4}\s+\d{1,2}:\d{2}\s*[AP]M)\]\s*$")


NYSE = mcal.get_calendar("NYSE")


def most_recent_market_open() -> datetime:
    """The most recent NYSE 9:30am-ET open that has already happened, as an
    ET-aware datetime (the project standardizes on ET everywhere — no UTC —
    to keep every timestamp in the code, logs, and JSON directly readable
    without a mental timezone conversion). A symbol often carries several
    headlines spanning multiple days (e.g. an analyst note two days ago, an
    after-hours release last night, a wire-service rehash this morning) —
    the one that actually explains a PREmarket or afterhours move is the
    newest one that landed before the open, not whatever's merely most
    recent chronologically. Anything stamped after that open is just
    same-day market-hours noise by the time this runs again (next premarket
    or afterhours window), so it's deprioritized in favor of the real
    pre-open catalyst. Computed via the exchange calendar (not hardcoded
    9:30) for the same reason the other two scripts do — holidays/early
    closes shift what "today's session" even is. pandas_market_calendars
    hands back its schedule in UTC internally (that's the library's own
    native format, not something we control), so each open is immediately
    converted to America/New_York here — nothing UTC-labeled ever leaves
    this function."""
    now_et = datetime.now(ZoneInfo("America/New_York"))
    sched = NYSE.schedule(
        start_date=(now_et - timedelta(days=10)).date(), end_date=now_et.date()
    )
    opens = [
        o.to_pydatetime().astimezone(ZoneInfo("America/New_York"))
        for o in sched["market_open"]
    ]
    opens = [o for o in opens if o <= now_et]
    if not opens:
        raise RuntimeError(
            f"pandas_market_calendars found no past NYSE opens in the 10 days "
            f"up to {now_et.date()} — that shouldn't happen."
        )
    return opens[-1]


def most_recent_market_close() -> datetime:
    """The most recent NYSE 4:00pm-ET close that has already happened, as an
    ET-aware datetime — the Afterhours-side counterpart to
    most_recent_market_open() above, and computed the same way (via the
    exchange calendar, so holidays/early closes shift it correctly)."""
    now_et = datetime.now(ZoneInfo("America/New_York"))
    sched = NYSE.schedule(
        start_date=(now_et - timedelta(days=10)).date(), end_date=now_et.date()
    )
    closes = [
        c.to_pydatetime().astimezone(ZoneInfo("America/New_York"))
        for c in sched["market_close"]
    ]
    closes = [c for c in closes if c <= now_et]
    if not closes:
        raise RuntimeError(
            f"pandas_market_calendars found no past NYSE closes in the 10 days "
            f"up to {now_et.date()} — that shouldn't happen."
        )
    return closes[-1]


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
                # UTC. This has flip-flopped twice in this project's history —
                # worth recording why the current answer is the one that
                # sticks. A prior version tagged these as UTC; that got
                # reverted to "it's already ET" based on one coincidental
                # data point (a freshest premarket bracket landing at 9:34 AM,
                # close to the 9:30 AM ET open). That coincidence didn't hold
                # up: pulling the FULL live page and comparing every unique
                # bracket against an independently-verified true clock (local
                # UTC time cross-checked against an HTTP Date response header)
                # showed the freshest bracket was only explicable as UTC (a
                # 22-minute lag) — read as ET, the same bracket would mean a
                # headline posted 3h38m in the future, which is impossible.
                # So: parse as UTC here, and convert to US/Eastern only when
                # serializing (see build_dataset below) — this keeps every
                # stored/displayed timestamp in ET as the project standard,
                # while parsing the source data at the timezone it's actually
                # in.
                dt_utc = datetime.strptime(ts, "%m/%d/%Y %I:%M %p").replace(
                    tzinfo=timezone.utc
                )
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
            if dt.tzinfo is None:  # guard against any legacy entries saved without tzinfo
                dt = dt.replace(tzinfo=ZoneInfo("America/New_York"))
        except (KeyError, ValueError):
            continue
        key = (e["symbol"], e["source_page"], e["direction"])
        existing[key] = {**e, "dt": dt}
    return existing


def better_entry(a, b, window):
    """Which of two candidate entries for the same (symbol, source_page,
    direction) key should win — preferring whichever headline actually falls
    inside `window` (the real session that source_page is supposed to
    represent) over one that falls outside it, and only falling back to
    "simply newer" when both candidates are on the same side (both inside,
    or both outside — so there's no genuine in-session catalyst to prefer and
    the latest one is the best available).

    This used to be a single before/after split around one cutoff timestamp,
    which worked for Premarket (the real catalyst for a premarket move is the
    newest headline before the open — open-ended on the early side, since any
    headline that old is still a valid pre-open catalyst) but broke for
    Afterhours: using the same "before/after" cutoff there meant a stray
    headline from THE NEXT MORNING could still read as "after the close" and,
    being newer, would win over the actual prior-evening afterhours headline
    — which is exactly the "AM headline next to an Afterhours entry" bug.
    A proper two-sided window (closed on the late side too) fixes that: a
    next-morning headline falls outside the Afterhours window entirely, so it
    can never outrank a real in-session one."""
    if b is None:
        return a
    start, end = window
    a_in = start <= a["dt"] < end
    b_in = start <= b["dt"] < end
    if a_in != b_in:
        return a if a_in else b
    return a if a["dt"] > b["dt"] else b


_EPOCH = datetime(2000, 1, 1, tzinfo=timezone.utc)  # open-ended lower bound for Premarket windows


def window_for(source_page: str, open_cutoff: datetime, close: datetime) -> tuple:
    """The acceptance window a candidate headline must fall in to be
    considered "really" from that source_page's session — see better_entry()
    above for why Afterhours needs a closed window and Premarket doesn't.
    Afterhours's window runs from the most recent close through the end of
    that same ET calendar day (generous enough for a late post-close
    earnings-call headline, but never into the next morning)."""
    if "Afterhours" in source_page:
        end_of_day = close.replace(hour=23, minute=59, second=59, microsecond=999999)
        return (close, end_of_day)
    return (_EPOCH, open_cutoff)  # Premarket / Premarket Earnings


def build_dataset():
    open_cutoff = most_recent_market_open()
    close = most_recent_market_close()

    all_entries = []
    for url, label in pages_to_scrape().items():
        try:
            html = fetch(url)
        except Exception as exc:  # noqa: BLE001
            print(f"WARN: failed to fetch {url}: {exc}", file=sys.stderr)
            continue
        all_entries.extend(parse_page(html, label))

    # Per (symbol, source_page, direction), keep whichever headline this run
    # scraped is the best candidate — see better_entry()/window_for() above.
    fresh = {}
    for e in all_entries:
        key = (e["symbol"], e["source_page"], e["direction"])
        window = window_for(e["source_page"], open_cutoff, close)
        fresh[key] = better_entry(e, fresh.get(key), window)

    # merge with whatever was already stored, using the same preference, so a
    # quiet run (nothing new on the live page right now) doesn't erase
    # yesterday's pre-open catalyst in favor of nothing, and a later run
    # doesn't let a fresh out-of-session rehash bump out an already-stored
    # in-session headline that's still the real explanation for the move
    merged = load_existing()
    for key, e in fresh.items():
        window = window_for(key[1], open_cutoff, close)
        merged[key] = better_entry(e, merged.get(key), window)

    cutoff = datetime.now(ZoneInfo("America/New_York")) - timedelta(hours=PRUNE_AFTER_HOURS)
    final = [e for e in merged.values() if e["dt"] >= cutoff]
    final.sort(key=lambda x: x["dt"], reverse=True)

    for e in final:
        # Source brackets are parsed as UTC (see parse_page above), but every
        # stored/displayed timestamp in this project is ET — convert here,
        # at the point of serialization, so time_iso always carries a real
        # America/New_York offset.
        e["time_iso"] = e["dt"].astimezone(ZoneInfo("America/New_York")).isoformat()
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
    generated_at = datetime.now(ZoneInfo("America/New_York")).isoformat()
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(
        json.dumps({"generated_at": generated_at, "data": data}, ensure_ascii=False),
        encoding="utf-8",
    )
    print(
        f"{datetime.now(ZoneInfo('America/New_York')).isoformat(timespec='seconds')}  "
        f"wrote {len(data)} entries to {OUTPUT_PATH}"
    )


if __name__ == "__main__":
    main()
