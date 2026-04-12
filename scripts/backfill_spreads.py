#!/usr/bin/env python3
"""
Bet The Farm Hub — Historical spread backfill

Reads results files in data/results/ that are missing spread data,
fetches historical closing odds from The Odds API, and attaches
spread/total to each game. Re-saves the results files in place.

This is a ONE-TIME migration script to populate historical ATS data
so compute_ats_records() in update_stats.py can replace BettingPros scraping.

Uses the /v4/historical/sports/{sport}/odds/ endpoint (paid tier).
Each API call costs ~1 request. With 6 sports × 18 dates = ~108 calls max.

Required: ODDS_API_KEY environment variable.
Usage:    ODDS_API_KEY=xxx python scripts/backfill_spreads.py
"""
from __future__ import annotations

import json
import os
import sys
import glob
import time
import requests
from datetime import datetime, timezone

RESULTS_DIR = "data/results"
TIMEOUT = 15

# Map hub sport keys to Odds API sport keys
SPORT_MAP = {
    "nba": "basketball_nba",
    "nhl": "icehockey_nhl",
    "mlb": "baseball_mlb",
    "nfl": "americanfootball_nfl",
}

# Common team name aliases (Odds API name → ESPN/hub name patterns)
# The matching is fuzzy (substring), so we don't need an exhaustive map.


def fetch_historical_odds(sport_key: str, date_iso: str, api_key: str) -> list:
    """
    Fetch historical odds for a sport on a given date.
    date_iso should be like '2026-03-22T18:00:00Z' (afternoon ET covers most games).
    Returns list of event dicts with bookmaker odds.
    """
    url = (
        f"https://api.the-odds-api.com/v4/historical/sports/{sport_key}/odds/"
        f"?apiKey={api_key}&regions=us&markets=spreads,totals"
        f"&oddsFormat=american&dateFormat=iso&date={date_iso}"
    )
    try:
        resp = requests.get(url, timeout=TIMEOUT)
        if resp.status_code == 401:
            print("  ✗ API key invalid or expired")
            return []
        if resp.status_code == 422:
            # Date not available (too old or future)
            return []
        if resp.status_code != 200:
            print(f"  ✗ HTTP {resp.status_code} for {sport_key} on {date_iso}")
            return []
        data = resp.json()
        remaining = resp.headers.get("x-requests-remaining", "?")
        print(f"    API calls remaining: {remaining}")
        # Historical endpoint wraps data in a 'data' key
        return data.get("data", data) if isinstance(data, dict) else data
    except Exception as e:
        print(f"  ✗ Error fetching {sport_key}: {e}")
        return []


def normalize(name: str) -> str:
    """Lowercase and strip for fuzzy matching."""
    return name.lower().strip()


def extract_spread_total(event: dict) -> dict:
    """
    Extract home spread and total from the first bookmaker's odds.
    Returns {home_team, away_team, spread, total}.
    """
    home = event.get("home_team", "")
    away = event.get("away_team", "")
    spread = None
    total = None

    for bm in event.get("bookmakers", []):
        for market in bm.get("markets", []):
            if market["key"] == "spreads":
                for outcome in market.get("outcomes", []):
                    if normalize(outcome.get("name", "")) == normalize(home):
                        spread = outcome.get("point")
                        break
            elif market["key"] == "totals":
                for outcome in market.get("outcomes", []):
                    if outcome.get("name") == "Over":
                        total = outcome.get("point")
                        break
        if spread is not None:
            break  # use first bookmaker that has data

    return {"home_team": home, "away_team": away, "spread": spread, "total": total}


def match_game(result_game: dict, odds_games: list) -> dict:
    """
    Find the matching odds game for a result game using fuzzy team name matching.
    Returns the matched odds dict or None.
    """
    rh = normalize(result_game.get("home", ""))
    ra = normalize(result_game.get("away", ""))
    if not rh or not ra:
        return None

    for og in odds_games:
        oh = normalize(og["home_team"])
        oa = normalize(og["away_team"])
        # Fuzzy match: check if one contains the other (handles "Boston Celtics" vs "Celtics")
        home_match = rh in oh or oh in rh
        away_match = ra in oa or oa in ra
        if home_match and away_match:
            return og
    return None


def main():
    api_key = os.environ.get("ODDS_API_KEY", "").strip()
    if not api_key:
        print("ERROR: Set ODDS_API_KEY environment variable")
        sys.exit(1)

    # Find results files missing spread data
    result_files = sorted(glob.glob(os.path.join(RESULTS_DIR, "*.json")))
    files_to_backfill = []

    for fpath in result_files:
        if "index" in os.path.basename(fpath):
            continue
        try:
            with open(fpath) as f:
                data = json.load(f)
        except Exception:
            continue
        if not isinstance(data, dict) or "sports" not in data:
            continue

        # Count games missing spreads
        missing = 0
        total = 0
        for sport, games in data.get("sports", {}).items():
            for g in games:
                total += 1
                if g.get("spread") is None:
                    missing += 1

        if missing > 0:
            files_to_backfill.append((fpath, missing, total))

    if not files_to_backfill:
        print("All results files already have spread data — nothing to backfill")
        sys.exit(0)

    print(f"Found {len(files_to_backfill)} results file(s) needing spread backfill:\n")
    for fpath, missing, total in files_to_backfill:
        print(f"  {os.path.basename(fpath)}: {missing}/{total} games missing spreads")

    total_filled = 0
    total_api_calls = 0

    for fpath, _, _ in files_to_backfill:
        date_key = os.path.basename(fpath).replace(".json", "")
        print(f"\n── Backfilling {date_key} ──")

        with open(fpath) as f:
            data = json.load(f)

        # Use 6 PM ET (23:00 UTC) as the snapshot time — most games have started
        api_date = f"{date_key}T23:00:00Z"

        file_filled = 0
        for sport, games in data.get("sports", {}).items():
            if sport not in SPORT_MAP:
                continue

            # Check if any games in this sport need spreads
            needs_spread = [g for g in games if g.get("spread") is None]
            if not needs_spread:
                continue

            print(f"  {sport.upper()}: {len(needs_spread)} game(s) need spreads...")

            # Fetch historical odds
            events = fetch_historical_odds(SPORT_MAP[sport], api_date, api_key)
            total_api_calls += 1

            if not events:
                print(f"    No historical odds available")
                time.sleep(0.5)
                continue

            # Extract spread/total from each event
            odds_games = [extract_spread_total(e) for e in events]

            # Match and fill
            for g in needs_spread:
                matched = match_game(g, odds_games)
                if matched:
                    spread_info = extract_spread_total(
                        next((e for e in events
                              if normalize(e.get("home_team", "")) == normalize(matched["home_team"])),
                             {})
                    )
                    if spread_info["spread"] is not None:
                        g["spread"] = spread_info["spread"]
                        g["total"] = spread_info["total"]
                        # Also set home_db/away_db if not present
                        g.setdefault("home_db", g.get("home", ""))
                        g.setdefault("away_db", g.get("away", ""))
                        file_filled += 1
                        print(f"    ✓ {g['home']} vs {g['away']}: spread={g['spread']:+.1f}, total={g['total']}")

            time.sleep(0.5)  # rate limit courtesy

        if file_filled > 0:
            with open(fpath, "w") as f:
                json.dump(data, f, indent=2)
            print(f"  → Saved {file_filled} spread(s) to {os.path.basename(fpath)}")
            total_filled += file_filled

    print(f"\n{'='*60}")
    print(f"Backfill complete: {total_filled} game(s) enriched across {len(files_to_backfill)} file(s)")
    print(f"API calls used: {total_api_calls}")
    print(f"\nRun 'python scripts/update_stats.py' to recompute ATS records from this data.")


if __name__ == "__main__":
    main()
