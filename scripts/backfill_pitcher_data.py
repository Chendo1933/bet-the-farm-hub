#!/usr/bin/env python3
"""
Backfill historical pitcher data for the MLB O/U backtest.

Two-pass fetch from the MLB Stats API (free, no key):

Pass 1 — STARTERS per game:
  Walk every date in data/results/*.json. For each date, hit
  /schedule?date=YYYY-MM-DD&hydrate=probablePitcher and record
  {gamePk: {home_pitcher_id, away_pitcher_id, names}}.

Pass 2 — GAME LOGS per pitcher:
  For each unique pitcher ID, hit /people/{id}/stats?stats=gameLog
  to pull every start they made this season with full raw stats
  (IP, ER, H, BB, K, HR). We compute cumulative ERA + FIP + WHIP
  ourselves so we can ask "what were their stats BEFORE game X"
  for the backtest.

Output: data/pitcher_data.json
  {
    "starters_by_gamePk": {"12345": {"home_id": 666129, "away_id": 527048,
                                     "home_name": "...", "away_name": "..."}},
    "game_logs": {
      "666129": [
        {"date": "2026-03-30", "ip": 5.2, "er": 2, "h": 4, "bb": 1, "k": 6, "hr": 0},
        ...
      ]
    }
  }

Idempotent — re-running won't re-fetch dates/pitchers we already have.
Friendly to MLB API: ~150ms sleep between calls. Full backfill of 56
dates + ~300 unique starters takes ~2 minutes.

Usage:
  python3 scripts/backfill_pitcher_data.py
  python3 scripts/backfill_pitcher_data.py --force   # ignore cache, refetch
"""
from __future__ import annotations

import argparse
import glob
import json
import sys
import time
import urllib.request
from pathlib import Path
from typing import Optional

CACHE_PATH = "data/pitcher_data.json"
RESULTS_GLOB = "data/results/*.json"
HEADERS = {"User-Agent": "bet-the-farm-hub backtest (chendo1933@github.com)"}
TIMEOUT = 20
PAUSE  = 0.15   # seconds between API calls


def _load_cache() -> dict:
    p = Path(CACHE_PATH)
    if not p.exists():
        return {"starters_by_gamePk": {}, "game_logs": {}, "dates_fetched": []}
    try:
        return json.loads(p.read_text())
    except Exception:
        return {"starters_by_gamePk": {}, "game_logs": {}, "dates_fetched": []}


def _save_cache(cache: dict) -> None:
    Path(CACHE_PATH).parent.mkdir(parents=True, exist_ok=True)
    Path(CACHE_PATH).write_text(json.dumps(cache, indent=2))


def _http_get(url: str) -> Optional[dict]:
    """Single HTTP GET with retry-on-429."""
    for attempt in range(3):
        try:
            req = urllib.request.Request(url, headers=HEADERS)
            with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt < 2:
                time.sleep(2.0 * (attempt + 1))
                continue
            print(f"  ⚠ HTTP {e.code} on {url[:80]}")
            return None
        except Exception as e:
            print(f"  ⚠ {type(e).__name__} on {url[:80]}: {e}")
            return None
    return None


def collect_dates_from_results() -> list[str]:
    """Pull every ET date present in data/results/*.json."""
    files = sorted(glob.glob(RESULTS_GLOB))
    dates = []
    for f in files:
        if "/index.json" in f: continue
        d = Path(f).stem  # YYYY-MM-DD
        if len(d) == 10 and d[4] == '-' and d[7] == '-':
            dates.append(d)
    return dates


def fetch_starters_for_date(date: str) -> dict:
    """Returns {gamePk: {home_id, away_id, home_name, away_name}} for date."""
    url = (f"https://statsapi.mlb.com/api/v1/schedule"
           f"?date={date}&sportId=1&hydrate=probablePitcher")
    data = _http_get(url)
    if not data:
        return {}
    out = {}
    for d in data.get("dates", []):
        for g in d.get("games", []):
            pk = g.get("gamePk")
            if pk is None: continue
            home = g.get("teams", {}).get("home", {})
            away = g.get("teams", {}).get("away", {})
            hp = home.get("probablePitcher") or {}
            ap = away.get("probablePitcher") or {}
            out[str(pk)] = {
                "home_id":   hp.get("id"),
                "home_name": hp.get("fullName"),
                "away_id":   ap.get("id"),
                "away_name": ap.get("fullName"),
                "home_team": home.get("team", {}).get("name"),
                "away_team": away.get("team", {}).get("name"),
            }
    return out


def fetch_game_log(pitcher_id: int) -> list[dict]:
    """Returns chronological list of per-start stats for the 2026 season."""
    url = (f"https://statsapi.mlb.com/api/v1/people/{pitcher_id}/stats"
           f"?stats=gameLog&group=pitching&season=2026&sportId=1")
    data = _http_get(url)
    if not data:
        return []
    # API returns empty `stats: []` for pitchers with no 2026 appearances
    # (rehab guys, releases, etc.). Guard the index access.
    stats_arr = data.get("stats") or []
    if not stats_arr:
        return []
    splits = stats_arr[0].get("splits", [])
    out = []
    for s in splits:
        stat = s.get("stat", {})
        # Skip relief appearances — we only care about starts. The API has
        # a `gamesStarted` field; for starters it's 1, for relievers it's 0.
        if stat.get("gamesStarted", 0) != 1:
            continue
        ip_str = stat.get("inningsPitched", "0")
        try:
            # "5.2" means 5 and 2/3 innings — convert to outs / 3
            whole, frac = (ip_str.split(".") + ["0"])[:2]
            ip = int(whole) + int(frac) / 3
        except Exception:
            ip = 0
        out.append({
            "date":  s.get("date"),
            "ip":    ip,
            "er":    int(stat.get("earnedRuns", 0) or 0),
            "h":     int(stat.get("hits", 0) or 0),
            "bb":    int(stat.get("baseOnBalls", 0) or 0),
            "k":     int(stat.get("strikeOuts", 0) or 0),
            "hr":    int(stat.get("homeRuns", 0) or 0),
            "bf":    int(stat.get("battersFaced", 0) or 0),
        })
    out.sort(key=lambda x: x["date"] or "")
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--force", action="store_true",
                    help="Ignore cache, refetch everything")
    args = ap.parse_args()

    cache = {"starters_by_gamePk": {}, "game_logs": {}, "dates_fetched": []} if args.force else _load_cache()

    # ── Pass 1: starters per date ──────────────────────────────────────
    dates = collect_dates_from_results()
    print(f"Pass 1: starters for {len(dates)} dates "
          f"({dates[0]} → {dates[-1]})")
    fetched = set(cache.get("dates_fetched", []))
    for i, d in enumerate(dates):
        if d in fetched and not args.force:
            continue
        starters = fetch_starters_for_date(d)
        cache["starters_by_gamePk"].update({k: v for k, v in starters.items()})
        cache.setdefault("dates_fetched", []).append(d)
        print(f"  {d}: +{len(starters)} games")
        time.sleep(PAUSE)
        if (i + 1) % 20 == 0:
            _save_cache(cache)   # checkpoint
    _save_cache(cache)

    # ── Pass 2: game logs per unique pitcher ───────────────────────────
    pitcher_ids = set()
    for entry in cache["starters_by_gamePk"].values():
        if entry.get("home_id"): pitcher_ids.add(int(entry["home_id"]))
        if entry.get("away_id"): pitcher_ids.add(int(entry["away_id"]))
    print(f"\nPass 2: game logs for {len(pitcher_ids)} unique starters")
    already_have = set(cache.get("game_logs", {}).keys())
    todo = [pid for pid in pitcher_ids if str(pid) not in already_have or args.force]
    print(f"  ({len(already_have)} cached, {len(todo)} to fetch)")
    for i, pid in enumerate(sorted(todo)):
        starts = fetch_game_log(pid)
        cache["game_logs"][str(pid)] = starts
        if (i + 1) % 25 == 0:
            print(f"  fetched {i+1}/{len(todo)} pitchers")
            _save_cache(cache)
        time.sleep(PAUSE)
    _save_cache(cache)

    # ── Stats ──────────────────────────────────────────────────────────
    total_games = len(cache["starters_by_gamePk"])
    pitchers_with_data = sum(1 for v in cache["game_logs"].values() if v)
    print(f"\n✓ Done. Cache → {CACHE_PATH}")
    print(f"  Games with starter info: {total_games}")
    print(f"  Pitchers with game logs: {pitchers_with_data}/{len(pitcher_ids)}")


if __name__ == "__main__":
    main()
