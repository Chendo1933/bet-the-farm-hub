#!/usr/bin/env python3
"""
Fetch the full 2025 College Football season (scores + closing lines) from
ESPN's public APIs so we can mine it for signals BEFORE the 2026 season
starts (~late August 2026).

Two ESPN endpoints, both free:
  • scoreboard: game list + final scores per date
      /apis/site/v2/sports/football/college-football/scoreboard?dates=YYYYMMDD&groups=80
      groups=80 = FBS (I-A) — the teams in our hub.
  • core odds: closing spread + over/under per game
      /v2/sports/football/leagues/college-football/events/{id}/competitions/{id}/odds

Output: data/cfb_history/2025.json
  {
    "season": "2025",
    "fetched": "...",
    "games": [
      {"date":"2025-10-11","home":"Illinois Fighting Illini",
       "away":"Ohio State Buckeyes","home_score":16,"away_score":34,
       "spread":-15.5,"total":51.5,"neutral":false,"conf_game":true},
      ...
    ]
  }

`spread` is the HOME team's line (negative = home favored), matching the
hub's MLB convention. Resumable via the cache — re-runs skip dates and
games already fetched.

Usage:
  python3 scripts/fetch_cfb_history.py
  python3 scripts/fetch_cfb_history.py --start 20250823 --end 20260120
"""
from __future__ import annotations

import argparse
import json
import time
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

OUT = "data/cfb_history/2025.json"
HEADERS = {"User-Agent": "bet-the-farm-hub cfb-history (chendo1933@github.com)"}
TIMEOUT = 20
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


def fetch_odds(gid: str) -> tuple[Optional[float], Optional[float]]:
    """Return (home_spread, total) for a game, or (None, None)."""
    url = (f"https://sports.core.api.espn.com/v2/sports/football/leagues/"
           f"college-football/events/{gid}/competitions/{gid}/odds")
    d = _get(url)
    if not d: return (None, None)
    items = d.get("items", [])
    # Prefer the first provider with a clean spread + total (ESPN BET).
    for o in items:
        spread = o.get("spread")
        ou = o.get("overUnder")
        if spread is not None and ou is not None:
            # ESPN 'spread' is from the favorite's perspective in `details`
            # but the numeric `spread` field is the home spread already.
            try:
                return (float(spread), float(ou))
            except (TypeError, ValueError):
                continue
    return (None, None)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--start", default="20250823", help="YYYYMMDD season start")
    ap.add_argument("--end",   default="20260120", help="YYYYMMDD season end (bowls)")
    args = ap.parse_args()

    # Load/resume cache
    cache = {"season":"2025","games":[],"dates_done":[]}
    if Path(OUT).exists():
        try: cache = json.loads(Path(OUT).read_text())
        except Exception: pass
    done = set(cache.get("dates_done", []))
    # Index existing games by (date,home,away) for dedup
    seen = {(g["date"],g["home"],g["away"]) for g in cache.get("games",[])}

    start = datetime.strptime(args.start, "%Y%m%d")
    end   = datetime.strptime(args.end, "%Y%m%d")
    cur = start
    total_new = 0
    days_with_games = 0

    while cur <= end:
        ymd = cur.strftime("%Y%m%d")
        iso = cur.strftime("%Y-%m-%d")
        if ymd in done:
            cur += timedelta(days=1); continue
        sb = _get(f"https://site.api.espn.com/apis/site/v2/sports/football/"
                  f"college-football/scoreboard?dates={ymd}&groups=80&limit=120")
        time.sleep(PAUSE)
        events = (sb or {}).get("events", [])
        if not events:
            done.add(ymd); cur += timedelta(days=1); continue
        days_with_games += 1
        day_new = 0
        for e in events:
            comp = (e.get("competitions") or [{}])[0]
            if not comp.get("status",{}).get("type",{}).get("completed"):
                continue
            home = away = None; hs = as_ = None
            for c in comp.get("competitors", []):
                nm = c.get("team",{}).get("displayName")
                sc = c.get("score")
                try: sc = int(sc)
                except (TypeError, ValueError): sc = None
                if c.get("homeAway") == "home": home, hs = nm, sc
                else: away, as_ = nm, sc
            if not home or not away or hs is None or as_ is None:
                continue
            if (iso,home,away) in seen:
                continue
            spread, total = fetch_odds(e.get("id"))
            time.sleep(PAUSE)
            cache["games"].append({
                "date": iso, "home": home, "away": away,
                "home_score": hs, "away_score": as_,
                "spread": spread, "total": total,
                "neutral": bool(comp.get("neutralSite")),
                "conf_game": bool(comp.get("conferenceCompetition")),
            })
            seen.add((iso,home,away)); day_new += 1; total_new += 1
        done.add(ymd)
        cache["dates_done"] = sorted(done)
        if day_new:
            print(f"  {iso}: +{day_new} games")
            Path(OUT).parent.mkdir(parents=True, exist_ok=True)
            Path(OUT).write_text(json.dumps(cache, default=str))  # checkpoint
        cur += timedelta(days=1)

    cache["fetched"] = datetime.now().isoformat()
    Path(OUT).parent.mkdir(parents=True, exist_ok=True)
    Path(OUT).write_text(json.dumps(cache, indent=2, default=str))
    games = cache["games"]
    with_lines = sum(1 for g in games if g.get("spread") is not None)
    print(f"\n✓ Done. {len(games)} games total ({total_new} new this run)")
    print(f"  with spread+total: {with_lines} ({100*with_lines/max(1,len(games)):.0f}%)")
    print(f"  → {OUT}")


if __name__ == "__main__":
    main()
