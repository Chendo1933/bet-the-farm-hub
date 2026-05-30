#!/usr/bin/env python3
"""
Fetch today's MLB market totals straight from the Odds API.

GROUND ZERO replacement for the hub-loading log_picks chain as the source of
market totals for the alt-total scanner. log_picks runs Playwright + loads the
entire 8500-line hub + scrapes its picks — heavy machinery, all to extract
one number per game. This script does just the number, with stdlib only.

Output: data/schedules/{date}.json  (same shape the alt-total scanner reads,
                                     and the same shape log_picks wrote)

  { "date": "YYYY-MM-DD", "logged": ISO, "has_odds": true,
    "games": [ {sport,home,away,spread,total,date,time,commence}, ... ] }

If log_picks already wrote today's schedule, we MERGE: the alt-total scanner
just needs market totals + game times to be present. Whichever source got there
first wins; this script is a safety net for the Ground Zero pipeline.

ENV
  ODDS_API_KEY      — required (same key log_picks/snapshot_odds use)
  ODDS_API_REGION   — optional (default "us")

Usage:
  ODDS_API_KEY=... python3 scripts/fetch_mlb_market_totals.py
  ODDS_API_KEY=... python3 scripts/fetch_mlb_market_totals.py --date 2026-05-28
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

ODDS_URL = ("https://api.the-odds-api.com/v4/sports/baseball_mlb/odds"
            "?regions={region}&markets=h2h,spreads,totals&oddsFormat=american&apiKey={key}")
SCHED_DIR = "data/schedules"
ET = ZoneInfo("America/New_York")


def _consensus(books: list[dict], market_key: str, outcome_filter=None):
    """Average a numeric field across books for the given market. Robust to
    a single book being weird — small consensus is sharper than one book."""
    vals = []
    for b in books:
        for m in b.get("markets", []):
            if m.get("key") != market_key:
                continue
            for o in m.get("outcomes", []):
                if outcome_filter and not outcome_filter(o):
                    continue
                v = o.get("point")
                if v is not None:
                    vals.append(float(v))
    return round(sum(vals) / len(vals), 2) if vals else None


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--date", help="ET date YYYY-MM-DD (default today)")
    args = ap.parse_args()

    key = os.environ.get("ODDS_API_KEY", "").strip()
    if not key:
        sys.exit("✗ ODDS_API_KEY not set")
    region = os.environ.get("ODDS_API_REGION", "us")

    now_et = datetime.now(ET)
    date = args.date or now_et.strftime("%Y-%m-%d")

    url = ODDS_URL.format(region=region, key=key)
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "btf-mlb-totals"}), timeout=20) as r:
            data = json.loads(r.read())
    except Exception as e:
        sys.exit(f"✗ Odds API fetch failed: {type(e).__name__}: {e}")

    games = []
    for ev in data:
        commence = ev.get("commence_time", "")
        # Filter to today's ET games
        try:
            ct = datetime.fromisoformat(commence.replace("Z", "+00:00")).astimezone(ET)
        except Exception:
            continue
        if ct.strftime("%Y-%m-%d") != date:
            continue
        home = ev.get("home_team"); away = ev.get("away_team")
        books = ev.get("bookmakers", [])
        # spread (home line — points where outcome.name == home)
        spread = _consensus(books, "spreads",
                            outcome_filter=lambda o: o.get("name") == home)
        # totals (line shared between over/under; just use the over)
        total = _consensus(books, "totals",
                           outcome_filter=lambda o: o.get("name", "").lower() == "over")
        games.append({
            "sport": "mlb", "home": home, "away": away,
            "spread": spread, "total": total,
            "date": date,
            "time": ct.strftime("%-I:%M %p ET"),
            "commence": commence,
        })

    # Merge with any existing schedule (log_picks may have already written it).
    sched_path = Path(f"{SCHED_DIR}/{date}.json")
    existing = {}
    if sched_path.exists():
        try:
            existing = json.loads(sched_path.read_text())
        except Exception:
            existing = {}
    existing_games = {(g.get("away"), g.get("home")): g for g in existing.get("games", [])
                      if (g.get("sport") or "").lower() == "mlb"}
    # Keep non-MLB entries from existing (other sports the legacy pipeline wrote).
    other = [g for g in existing.get("games", [])
             if (g.get("sport") or "").lower() != "mlb"]

    merged = list(other)
    for g in games:
        key2 = (g["away"], g["home"])
        old = existing_games.get(key2) or {}
        # Prefer fresher non-null fields from our fetch; fall back to existing.
        merged.append({**old, **{k: v for k, v in g.items() if v is not None}})

    out = {
        "date": date,
        "logged": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "has_odds": True,
        "games": merged,
        "source": "fetch_mlb_market_totals (Ground Zero)",
    }
    Path(SCHED_DIR).mkdir(parents=True, exist_ok=True)
    sched_path.write_text(json.dumps(out, indent=2))
    print(f"✅ Wrote {len(games)} MLB games to {sched_path}")
    for g in games:
        print(f"  {g['time']:<11} {g['away'][:18]:<18} @ {g['home'][:18]:<18}  total {g['total']}")


if __name__ == "__main__":
    main()
