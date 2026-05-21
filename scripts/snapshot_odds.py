#!/usr/bin/env python3
"""
Bet The Farm Hub — Intraday odds snapshot

Fetches current spreads, totals, and moneylines from The Odds API and saves
them to data/odds_snapshots/YYYY-MM-DD-{label}.json. The hub's F12 line-movement
factor (~index.html line 8150) compares the current spread to the most-recent
pre-game snapshot for that game, and separately measures the morning→pregame
drift to detect "sharps loaded up midday" patterns.

Labels (all run via this script with --label):
    morning   ~7:00 AM ET   — full-slate baseline (daily-update.yml)
    midday    ~12:00 PM ET  — captures bulk of pro money before evening games
    pregame   hourly        — filtered to games starting in the next 75 min;
                              merges with the existing day's pregame file so
                              hourly sweeps accumulate without losing prior runs

Requires ODDS_API_KEY environment variable.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone

import requests

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

# Default per-label commence-time window. None = capture everything.
DEFAULT_WINDOW_HOURS = {
    "morning": None,
    "midday": None,
    "pregame": 1.5,
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


def extract_lines(games: list[dict], capture_time_iso: str) -> list[dict]:
    """Extract spread, total, and ML from Odds API response.
    Uses the first available bookmaker (consensus-like).
    Stamps each line with the capture time so hourly pregame merges can keep
    the freshest snapshot per game."""
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
            "snapshot_time": capture_time_iso,
        })

    return lines


def filter_by_window(lines: list[dict], now: datetime, window_hours: float | None) -> list[dict]:
    """Drop games whose commence time is outside [now, now+window_hours].
    None disables filtering. Games with unparseable commence times are kept."""
    if window_hours is None:
        return lines
    cutoff = now + timedelta(hours=window_hours)
    kept = []
    for g in lines:
        ct = g.get("commence", "")
        if not ct:
            kept.append(g)
            continue
        try:
            # API returns Z-suffixed ISO; normalize to aware UTC
            commence_dt = datetime.fromisoformat(ct.replace("Z", "+00:00"))
        except ValueError:
            kept.append(g)
            continue
        if now <= commence_dt <= cutoff:
            kept.append(g)
    return kept


def merge_snapshot(existing: dict, new_games: dict[str, list[dict]]) -> dict[str, list[dict]]:
    """Merge new games into an existing snapshot dict-of-sport-lists.
    Newer entries (by snapshot_time) win per (home, away) key. Used for hourly
    pregame sweeps so each game's pre-game snapshot is the freshest one taken
    before tip-off."""
    merged: dict[str, list[dict]] = {}
    prior = existing.get("games", {}) if isinstance(existing, dict) else {}
    sports = set(prior.keys()) | set(new_games.keys())
    for sport in sports:
        by_key: dict[tuple[str, str], dict] = {}
        for g in prior.get(sport, []):
            by_key[(g.get("home", ""), g.get("away", ""))] = g
        for g in new_games.get(sport, []):
            key = (g.get("home", ""), g.get("away", ""))
            existing_g = by_key.get(key)
            if existing_g is None:
                by_key[key] = g
                continue
            # Keep whichever has the later snapshot_time
            new_t = g.get("snapshot_time", "")
            old_t = existing_g.get("snapshot_time", "")
            if new_t >= old_t:
                by_key[key] = g
        merged[sport] = list(by_key.values())
    return merged


def main():
    parser = argparse.ArgumentParser(description="Capture odds snapshot for line-movement tracking.")
    parser.add_argument("--label", default="morning",
                        help="Snapshot label: morning, midday, or pregame. Determines output filename and default window.")
    parser.add_argument("--window-hours", type=float, default=None,
                        help="Only capture games starting within this many hours. Defaults to per-label setting (pregame=1.5, others=unfiltered).")
    args = parser.parse_args()

    label = args.label
    window_hours = args.window_hours if args.window_hours is not None else DEFAULT_WINDOW_HOURS.get(label)

    api_key = os.environ.get("ODDS_API_KEY", "").strip()
    if not api_key:
        print(f"✗ ODDS_API_KEY not set — skipping {label} snapshot")
        sys.exit(0)  # soft exit so workflow continues

    now = datetime.now(timezone.utc)
    month = now.month
    date_key = now.strftime("%Y-%m-%d")
    now_iso = now.isoformat()

    win_desc = f"{window_hours:.1f}h window" if window_hours is not None else "no window"
    print(f"[snapshot_odds] {label} odds snapshot — {date_key} ({win_desc})")

    all_games: dict[str, list[dict]] = {}
    total_games = 0

    for sport, sport_key in SPORT_MAP.items():
        if month in OFF_SEASON.get(sport, []):
            continue

        print(f"  Fetching {sport.upper()}...")
        raw = fetch_odds(sport_key, api_key)
        if not raw:
            continue

        lines = extract_lines(raw, now_iso)
        # Filter to games with actual odds
        lines = [g for g in lines if g["spread"] is not None or g["total"] is not None]
        # Apply per-label commence-time window
        lines = filter_by_window(lines, now, window_hours)

        if lines:
            all_games[sport] = lines
            total_games += len(lines)
            print(f"    → {len(lines)} games with lines")

        # Stagger requests to be polite
        time.sleep(0.5)

    out_path = os.path.join(SNAPSHOT_DIR, f"{date_key}-{label}.json")

    if not all_games:
        # Pregame sweeps frequently find nothing in their window — that's normal.
        # Don't overwrite an existing file with an empty one.
        if label == "pregame" and os.path.exists(out_path):
            print(f"  No games in window — leaving existing {out_path} untouched")
        else:
            print(f"  No games with odds found — skipping snapshot")
        sys.exit(0)

    # Pregame snapshots merge with existing file so hourly sweeps accumulate
    # the freshest pre-game line per game across the day.
    if label == "pregame" and os.path.exists(out_path):
        try:
            with open(out_path, "r") as f:
                existing = json.load(f)
            all_games = merge_snapshot(existing, all_games)
            total_games = sum(len(v) for v in all_games.values())
            print(f"  Merged with existing pregame file → {total_games} games total")
        except Exception as e:
            print(f"  ⚠ Could not merge existing pregame file ({e}); overwriting")

    snapshot = {
        "date": date_key,
        "snapshot_time": now_iso,
        "type": label,
        "games": all_games,
    }

    os.makedirs(SNAPSHOT_DIR, exist_ok=True)
    with open(out_path, "w") as f:
        json.dump(snapshot, f, indent=2)

    print(f"\n✅ Saved {total_games} games across {len(all_games)} sports → {out_path}")


if __name__ == "__main__":
    main()
