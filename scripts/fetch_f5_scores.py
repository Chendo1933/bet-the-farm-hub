#!/usr/bin/env python3
"""
Fetch first-5-innings (F5) scores from the MLB Stats API for every
completed MLB game and stamp them into data/results/{date}.json.

Used by reconcile.py to grade f5_ou paper picks. Without F5 scores
we'd have no way to know whether the paper f5_ou model was right.

How it works:
  • Walk data/results/*.json — every MLB entry with a final score
    but no f5_total field gets enriched.
  • For each game, call /api/v1/game/{gamePk}/linescore from MLB Stats
    API (free, no key). Sum innings 1-5 for both teams.
  • Write the enriched results file back.

The challenge: ESPN game_id ≠ MLB gamePk. We resolve gamePk via the
same MLB schedule endpoint used by backfill_pitcher_data.py — match
on (date, home_team, away_team).

Idempotent: games already enriched with f5_total are skipped.

Usage:
  python3 scripts/fetch_f5_scores.py             # backfill all
  python3 scripts/fetch_f5_scores.py --date 2026-05-20  # one date
  python3 scripts/fetch_f5_scores.py --force     # refetch all
"""
from __future__ import annotations

import argparse
import glob
import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Optional

HEADERS = {"User-Agent": "bet-the-farm-hub f5-fetcher (chendo1933@github.com)"}
TIMEOUT = 20
PAUSE = 0.15


def _http_get(url: str) -> Optional[dict]:
    for attempt in range(3):
        try:
            req = urllib.request.Request(url, headers=HEADERS)
            with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt < 2:
                time.sleep(2.0 * (attempt + 1)); continue
            return None
        except Exception:
            return None
    return None


def _norm_team(name: str) -> str:
    """Match ESPN names to MLB Stats API names where they differ."""
    fixes = {"Oakland Athletics": "Athletics", "Sacramento Athletics": "Athletics"}
    return fixes.get(name, name)


def fetch_schedule_with_linescore(date: str) -> dict:
    """
    Get every game's gamePk + linescore for one date in one API call.
    Linescore hydration includes per-inning scoring totals — exactly
    what we need for F5.
    """
    url = (f"https://statsapi.mlb.com/api/v1/schedule"
           f"?date={date}&sportId=1&hydrate=linescore")
    data = _http_get(url)
    if not data: return {}
    out = {}
    for d in data.get("dates", []):
        for g in d.get("games", []):
            pk = g.get("gamePk")
            if pk is None: continue
            home = _norm_team(g.get("teams",{}).get("home",{}).get("team",{}).get("name",""))
            away = _norm_team(g.get("teams",{}).get("away",{}).get("team",{}).get("name",""))
            # Only enrich completed games
            status = g.get("status",{}).get("abstractGameState","")
            if status != "Final": continue
            ls = g.get("linescore", {})
            innings = ls.get("innings", [])
            # Sum runs from innings 1..5. Each inning has home.runs and away.runs.
            h_f5 = sum((i.get("home",{}).get("runs",0) or 0) for i in innings[:5])
            a_f5 = sum((i.get("away",{}).get("runs",0) or 0) for i in innings[:5])
            # Some final boxes won't have all 5 innings populated (rain shortened,
            # walkoff in 5th, etc.) — record what we have but flag completeness.
            innings_available = len(innings[:5])
            out[(date, home, away)] = {
                "gamePk":       pk,
                "f5_home":      h_f5,
                "f5_away":      a_f5,
                "f5_total":     h_f5 + a_f5,
                "innings_with_data": innings_available,
                # If <5 innings of data, treat as partial. Reconcile will
                # skip grading these to avoid biased samples.
                "f5_complete":  innings_available >= 5,
            }
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--date", help="Only process this ET date (YYYY-MM-DD)")
    ap.add_argument("--force", action="store_true",
                    help="Re-fetch even if f5_total is already present")
    args = ap.parse_args()

    files = sorted(glob.glob("data/results/*.json"))
    files = [f for f in files if "/index.json" not in f]
    if args.date:
        files = [f for f in files if f.endswith(f"/{args.date}.json")]
    if not files:
        sys.exit("No results files to process")

    print(f"Fetching F5 scores for {len(files)} result file(s)")

    total_enriched = 0
    for fpath in files:
        try:
            data = json.loads(Path(fpath).read_text())
        except Exception:
            print(f"  ⚠ Could not parse {fpath}"); continue

        date = data.get("date", Path(fpath).stem)
        mlb = data.get("sports", {}).get("mlb", [])
        if not mlb: continue

        # Skip the date entirely if every game already has f5_total
        needs_fetch = any(g.get("f5_total") is None or args.force for g in mlb)
        if not needs_fetch:
            continue

        f5_by_teams = fetch_schedule_with_linescore(date)
        time.sleep(PAUSE)

        enriched_this_file = 0
        for g in mlb:
            home = _norm_team(g.get("home_db") or g.get("home") or "")
            away = _norm_team(g.get("away_db") or g.get("away") or "")
            if not args.force and g.get("f5_total") is not None:
                continue
            f5 = f5_by_teams.get((date, home, away))
            if not f5: continue
            g["f5_home"] = f5["f5_home"]
            g["f5_away"] = f5["f5_away"]
            g["f5_total"] = f5["f5_total"]
            g["f5_complete"] = f5["f5_complete"]
            enriched_this_file += 1

        if enriched_this_file:
            Path(fpath).write_text(json.dumps(data, indent=2, default=str))
            print(f"  {date}: enriched {enriched_this_file} game(s) with F5 scores")
            total_enriched += enriched_this_file

    print(f"\n✓ Done. Enriched {total_enriched} game(s) total.")


if __name__ == "__main__":
    main()
