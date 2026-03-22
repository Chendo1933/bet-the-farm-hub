#!/usr/bin/env python3
"""
Bet The Farm Hub — Nightly game results logger
Fetches completed game scores from ESPN for all active sports and
appends them to data/results/YYYY-MM-DD.json.

Over time this builds a pick-history archive you can replay to:
  - Compute how many games the hub correctly predicted
  - Auto-calculate ATS records once odds data is added
  - Tune factor weights based on real outcomes

Run after games end (~11 PM ET / 03:00 UTC next day).
"""

import json
import os
import sys
import requests
from datetime import datetime, timezone, timedelta

DATA_DIR = "data/results"
TIMEOUT  = 15
HEADERS  = {"User-Agent": "Mozilla/5.0 (BetTheFarm/1.0 results-logger)"}

SPORTS = [
    ("nba",  "basketball", "nba"),
    ("nhl",  "hockey",     "nhl"),
    ("mlb",  "baseball",   "mlb"),
    ("nfl",  "football",   "nfl"),
    ("cfb",  "football",   "college-football"),
    ("cbb",  "basketball", "mens-college-basketball"),
]


def espn_get(url):
    try:
        r = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        print(f"  ⚠  fetch failed: {url}\n     {e}")
        return None


def fetch_completed_games(espn_sport, espn_league, date_str):
    """
    Fetch completed games for a given date (YYYYMMDD).
    Returns list of game dicts: {sport, home, away, home_score, away_score,
                                  winner, status, game_id, date}
    """
    url = (f"https://site.api.espn.com/apis/site/v2/sports"
           f"/{espn_sport}/{espn_league}/scoreboard?dates={date_str}&limit=50")
    data = espn_get(url)
    if not data:
        return []

    games = []
    for event in data.get("events", []):
        comp = event.get("competitions", [{}])[0]
        status = comp.get("status", {}).get("type", {})
        state  = status.get("state", "")       # pre / in / post
        detail = status.get("description", "") # "Final", "Final/OT", etc.

        if state != "post":
            continue   # skip games not yet finished

        competitors = comp.get("competitors", [])
        home = next((c for c in competitors if c.get("homeAway") == "home"), {})
        away = next((c for c in competitors if c.get("homeAway") == "away"), {})

        home_name  = home.get("team", {}).get("displayName", "")
        away_name  = away.get("team", {}).get("displayName", "")
        home_score = int(home.get("score", 0) or 0)
        away_score = int(away.get("score", 0) or 0)

        if home_score == 0 and away_score == 0:
            continue   # likely no score data

        winner = "home" if home_score > away_score else (
                 "away" if away_score > home_score else "tie")

        games.append({
            "game_id":    event.get("id", ""),
            "name":       event.get("name", f"{away_name} @ {home_name}"),
            "home":       home_name,
            "away":       away_name,
            "home_score": home_score,
            "away_score": away_score,
            "winner":     winner,
            "status":     detail,
        })

    return games


def main():
    now_utc = datetime.now(timezone.utc)
    # Log yesterday's games — run at 03:00 UTC so all US games are finished
    target = now_utc - timedelta(days=1)
    date_str  = target.strftime("%Y%m%d")   # ESPN format: 20260322
    date_key  = target.strftime("%Y-%m-%d") # file/JSON key: 2026-03-22

    print(f"[log_results] Logging results for {date_key}")
    os.makedirs(DATA_DIR, exist_ok=True)

    all_results = {
        "date":   date_key,
        "logged": now_utc.isoformat(),
        "sports": {}
    }
    total_games = 0

    for sport_key, espn_sport, espn_league in SPORTS:
        games = fetch_completed_games(espn_sport, espn_league, date_str)
        if games:
            all_results["sports"][sport_key] = games
            total_games += len(games)
            print(f"  ✓ {sport_key.upper()}: {len(games)} completed game(s)")
            for g in games:
                print(f"      {g['away']} {g['away_score']} @ {g['home']} {g['home_score']}"
                      f"  ({g['status']})")
        else:
            print(f"  · {sport_key.upper()}: no completed games")

    if total_games == 0:
        print(f"\nℹ  No games found for {date_key} — skipping file write")
        sys.exit(0)

    out_path = os.path.join(DATA_DIR, f"{date_key}.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(all_results, f, indent=2)

    print(f"\n✅ {total_games} game(s) saved → {out_path}")

    # Also maintain a running index file for quick lookups
    index_path = os.path.join(DATA_DIR, "index.json")
    index = []
    if os.path.exists(index_path):
        try:
            with open(index_path) as f:
                index = json.load(f)
        except Exception:
            index = []

    # Add today's entry if not already present
    if date_key not in index:
        index.append(date_key)
        index.sort(reverse=True)   # most recent first
        with open(index_path, "w") as f:
            json.dump(index, f, indent=2)
        print(f"✅ index.json updated ({len(index)} date(s) on record)")

    sys.exit(0)


if __name__ == "__main__":
    main()
