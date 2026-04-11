#!/usr/bin/env python3
"""
Bet The Farm Hub — Morning odds snapshot
Fetches current spreads, totals, and moneylines from The Odds API
and saves to data/odds_snapshots/YYYY-MM-DD-morning.json.

This snapshot is compared against the afternoon schedule snapshot
(from log_picks.py at ~11:50 AM ET) to detect line movement.

Runs as part of daily-update.yml at ~7:00 AM ET.
Requires ODDS_API_KEY environment variable.
"""

import json
import os
import sys
import time
import requests
from datetime import datetime, timezone

SNAPSHOT_DIR = "data/odds_snapshots"
TIMEOUT = 15
HEADERS = {"User-Agent": "Mozilla/5.0 (BetTheFarm/1.0 odds-snapshot)"}

# Same sport keys as the hub's ODDS_SPORT_MAP
SPORT_MAP = {
    "nba": "basketball_nba",
    "nhl": "icehockey_nhl",
    "mlb": "baseball_mlb",
    "nfl": "americanfootball_nfl",
    "cfb": "americanfootball_ncaaf",
    "cbb": "basketball_ncaab",
}

# Off-season months per sport (don't waste API calls)
OFF_SEASON = {
    "nfl": [3, 4, 5, 6, 7],
    "cfb": [3, 4, 5, 6, 7, 8],
    "mlb": [11, 12, 1, 2],
    "cbb": [5, 6, 7, 8, 9, 10],
}


def fetch_odds(sport_key: str, api_key: str) -> list[dict]:
    """Fetch current odds for one sport from The Odds API."""
    url = (
        f"https://api.the-odds-api.com/v4/sports/{sport_key}/odds/"
        f"?apiKey={api_key}&regions=us&markets=spreads,totals,h2h"
        f"&oddsFormat=american&dateFormat=iso"
    )
    try:
        r = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
        if r.status_code == 401:
            print(f"  ✗ API key invalid (401)")
            return []
        if r.status_code == 429:
            print(f"  ✗ Rate limited (429)")
            return []
        r.raise_for_status()
        remaining = r.headers.get("x-requests-remaining", "?")
        print(f"    API calls remaining: {remaining}")
        return r.json()
    except Exception as e:
        print(f"  ✗ Fetch failed: {e}")
        return []


def extract_lines(games: list[dict]) -> list[dict]:
    """Extract spread, total, and ML from Odds API response.
    Uses the first available bookmaker (consensus-like)."""
    lines = []
    for game in games:
        home = game.get("home_team", "")
        away = game.get("away_team", "")
        commence = game.get("commence_time", "")

        spread = None
        total = None
        ml_home = None
        ml_away = None

        for book in game.get("bookmakers", []):
            for market in book.get("markets", []):
                key = market.get("key")
                outcomes = market.get("outcomes", [])

                if key == "spreads" and spread is None:
                    for o in outcomes:
                        if o.get("name") == home:
                            spread = o.get("point")

                if key == "totals" and total is None:
                    for o in outcomes:
                        if o.get("name") == "Over":
                            total = o.get("point")

                if key == "h2h" and ml_home is None:
                    for o in outcomes:
                        if o.get("name") == home:
                            ml_home = o.get("price")
                        elif o.get("name") == away:
                            ml_away = o.get("price")

            # Stop after first bookmaker with data
            if spread is not None or total is not None:
                break

        lines.append({
            "home": home,
            "away": away,
            "commence": commence,
            "spread": spread,
            "total": total,
            "ml_home": ml_home,
            "ml_away": ml_away,
        })

    return lines


def main():
    api_key = os.environ.get("ODDS_API_KEY", "").strip()
    if not api_key:
        print("✗ ODDS_API_KEY not set — skipping morning snapshot")
        sys.exit(0)  # soft exit so workflow continues

    now = datetime.now(timezone.utc)
    month = now.month
    date_key = now.strftime("%Y-%m-%d")

    print(f"[snapshot_odds] Morning odds snapshot — {date_key}")

    all_games = {}
    total_games = 0

    for sport, sport_key in SPORT_MAP.items():
        if month in OFF_SEASON.get(sport, []):
            continue

        print(f"  Fetching {sport.upper()}...")
        raw = fetch_odds(sport_key, api_key)
        if not raw:
            continue

        lines = extract_lines(raw)
        # Filter to games with actual odds
        lines = [g for g in lines if g["spread"] is not None or g["total"] is not None]

        if lines:
            all_games[sport] = lines
            total_games += len(lines)
            print(f"    → {len(lines)} games with lines")

        # Stagger requests to be polite
        time.sleep(0.5)

    if not all_games:
        print("  No games with odds found — skipping snapshot")
        sys.exit(0)

    snapshot = {
        "date": date_key,
        "snapshot_time": now.isoformat(),
        "type": "morning",
        "games": all_games,
    }

    os.makedirs(SNAPSHOT_DIR, exist_ok=True)
    out_path = os.path.join(SNAPSHOT_DIR, f"{date_key}-morning.json")
    with open(out_path, "w") as f:
        json.dump(snapshot, f, indent=2)

    print(f"\n✅ Saved {total_games} games across {len(all_games)} sports → {out_path}")


if __name__ == "__main__":
    main()
