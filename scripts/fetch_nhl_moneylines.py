#!/usr/bin/env python3
"""
Fetch historical NHL moneylines from ESPN for every game in our results
archive, so we can test underdog-ML value (favorite-longshot bias + model
edge) on hockey — where the favorite-longshot bias is actually documented,
unlike MLB. Our results files store ESPN game_ids; ESPN's core odds endpoint
returns home/away moneyline per game.

Output: data/nhl_moneylines.json
  { "401873383": {"home_ml": -218, "away_ml": 180, "date": "2026-05-21",
                  "home": "Carolina Hurricanes", "away": "Montreal Canadiens"}, ... }

Resumable — skips game_ids already cached.

Usage:
  python3 scripts/fetch_nhl_moneylines.py
"""
from __future__ import annotations

import glob
import json
import time
import urllib.request
import urllib.error
from pathlib import Path
from typing import Optional

OUT = "data/nhl_moneylines.json"
HEADERS = {"User-Agent": "bet-the-farm-hub nhl-ml-fetch (chendo1933@github.com)"}
TIMEOUT = 15
PAUSE = 0.10


def _get(url: str) -> Optional[dict]:
    for attempt in range(3):
        try:
            req = urllib.request.Request(url, headers=HEADERS)
            with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt < 2:
                time.sleep(2.0*(attempt+1)); continue
            return None
        except Exception:
            return None
    return None


def fetch_ml(gid: str) -> tuple[Optional[float], Optional[float]]:
    url = (f"https://sports.core.api.espn.com/v2/sports/hockey/leagues/nhl/"
           f"events/{gid}/competitions/{gid}/odds")
    d = _get(url)
    if not d: return (None, None)
    for o in d.get("items", []):
        hml = o.get("homeTeamOdds", {}).get("moneyLine")
        aml = o.get("awayTeamOdds", {}).get("moneyLine")
        if hml is not None and aml is not None:
            return (hml, aml)
    return (None, None)


def main():
    cache = {}
    if Path(OUT).exists():
        try: cache = json.loads(Path(OUT).read_text())
        except Exception: pass

    games = []
    for f in sorted(glob.glob("data/results/*.json")):
        if "/index.json" in f: continue
        d = json.loads(Path(f).read_text())
        date = d.get("date", Path(f).stem)
        for g in d.get("sports", {}).get("nhl", []):
            if not isinstance(g, dict): continue
            gid = g.get("game_id")
            if gid:
                games.append((str(gid), date, g.get("home_db") or g.get("home"),
                              g.get("away_db") or g.get("away")))

    todo = [g for g in games if g[0] not in cache]
    print(f"NHL moneylines: {len(games)} games, {len(cache)} cached, {len(todo)} to fetch")
    fetched = 0
    for i, (gid, date, home, away) in enumerate(todo):
        hml, aml = fetch_ml(gid)
        if hml is not None:
            cache[gid] = {"home_ml": hml, "away_ml": aml, "date": date,
                          "home": home, "away": away}
            fetched += 1
        time.sleep(PAUSE)
        if (i+1) % 100 == 0:
            print(f"  {i+1}/{len(todo)} ...")
            Path(OUT).parent.mkdir(parents=True, exist_ok=True)
            Path(OUT).write_text(json.dumps(cache))
    Path(OUT).parent.mkdir(parents=True, exist_ok=True)
    Path(OUT).write_text(json.dumps(cache, indent=2))
    print(f"\n✓ {fetched} new moneylines fetched · {len(cache)} total → {OUT}")


if __name__ == "__main__":
    main()
