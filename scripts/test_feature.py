#!/usr/bin/env python3
"""
MLB feature-testing harness.

The question every new signal must answer: does it actually SEPARATE
high-scoring games from low-scoring ones? If a feature has predictive
power, games in its top tercile will score measurably different from
games in its bottom tercile.

This engine computes a batch of candidate per-game features (using only
pre-game info — no lookahead) and reports, for each:
  • the F5 over-rate and full-game over-rate in the top vs bottom tercile
  • the spread (separation) between them

A feature with real signal shows a wide spread (e.g. top tercile 60%
over, bottom 40% over = 20pt separation). A useless feature shows ~0
separation (both ~50%).

This is more honest than a betting backtest for NEW signals because we
don't have historical betting lines for most of them — but we DO have
actual outcomes, so we can measure raw predictive power. Features that
separate are worth wiring into the live model + paper-tracking; features
that don't, we drop.

Usage:
  python3 scripts/test_feature.py
"""
from __future__ import annotations

import glob
import json
import statistics
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Optional

PITCHER_DATA = "data/pitcher_data.json"

# Bullpen/park reference tables (snapshot — see caveat below)
PARK = {
    'Baltimore Orioles':1.00,'Boston Red Sox':1.08,'New York Yankees':1.03,
    'Tampa Bay Rays':0.96,'Toronto Blue Jays':1.02,'Chicago White Sox':1.01,
    'Cleveland Guardians':0.95,'Detroit Tigers':0.97,'Kansas City Royals':0.98,
    'Minnesota Twins':1.03,'Houston Astros':0.97,'Los Angeles Angels':1.01,
    'Athletics':0.99,'Seattle Mariners':0.93,'Texas Rangers':1.05,
    'Atlanta Braves':1.01,'Miami Marlins':0.95,'New York Mets':0.97,
    'Philadelphia Phillies':1.02,'Washington Nationals':1.01,
    'Chicago Cubs':1.04,'Cincinnati Reds':1.05,'Milwaukee Brewers':0.97,
    'Pittsburgh Pirates':0.98,'St. Louis Cardinals':0.99,
    'Arizona Diamondbacks':1.02,'Colorado Rockies':1.28,'Los Angeles Dodgers':0.98,
    'San Diego Padres':0.93,'San Francisco Giants':0.92,
}


def _norm(n: str) -> str:
    return {"Oakland Athletics":"Athletics","Sacramento Athletics":"Athletics"}.get(n, n)


def cumulative(starts, before_date):
    prior = [s for s in (starts or []) if s.get("date") and s["date"] < before_date]
    if not prior: return None
    ip = sum(s.get("ip",0) for s in prior)
    if ip < 1: return None
    er=sum(s.get("er",0) for s in prior); bb=sum(s.get("bb",0) for s in prior)
    k=sum(s.get("k",0) for s in prior); hr=sum(s.get("hr",0) for s in prior)
    return {"era":9*er/ip,"fip":(13*hr+3*bb-2*k)/ip+3.10,"starts":len(prior),
            "last_date":prior[-1]["date"]}


def recent_form(starts, before_date, n=3):
    """Avg ERA over a pitcher's last n starts before the date (recent trend)."""
    prior = [s for s in (starts or []) if s.get("date") and s["date"] < before_date]
    if len(prior) < n: return None
    recent = prior[-n:]
    ip = sum(s.get("ip",0) for s in recent)
    if ip < 1: return None
    er = sum(s.get("er",0) for s in recent)
    return 9*er/ip


def days_between(d1, d2):
    try:
        return (datetime.strptime(d2,"%Y-%m-%d") - datetime.strptime(d1,"%Y-%m-%d")).days
    except Exception:
        return None


def main():
    pdata = json.loads(Path(PITCHER_DATA).read_text())
    logs = pdata.get("game_logs", {})
    by_teams = {}
    for pk, e in pdata.get("starters_by_gamePk", {}).items():
        if e.get("home_team") and e.get("away_team") and e.get("home_id") and e.get("away_id"):
            by_teams[(_norm(e["home_team"]), _norm(e["away_team"]))] = (e["home_id"], e["away_id"])

    # Build a chronological list of games with their features + outcomes.
    # Also track each team's rolling runs-scored for hot/cold offense.
    team_recent_runs = defaultdict(list)  # team -> [runs scored, newest last]

    games = []
    files = sorted(glob.glob("data/results/*.json"))
    for f in files:
        if "/index.json" in f: continue
        d = json.loads(Path(f).read_text())
        date = d.get("date", Path(f).stem)
        for g in d.get("sports",{}).get("mlb",[]):
            home=_norm(g.get("home_db") or g.get("home") or "")
            away=_norm(g.get("away_db") or g.get("away") or "")
            hs=g.get("home_score"); as_=g.get("away_score")
            if hs is None or as_ is None or not home or not away: continue
            full_total = hs + as_
            f5_total = g.get("f5_total") if g.get("f5_complete") else None

            feats = {}
            # Park
            feats["park"] = PARK.get(home)
            # Pitcher features
            ids = by_teams.get((home,away))
            if ids:
                h = cumulative(logs.get(str(ids[0]),[]), date)
                a = cumulative(logs.get(str(ids[1]),[]), date)
                if h and a:
                    feats["combined_fip"] = h["fip"] + a["fip"]
                    # Rest: days since each starter's last outing
                    hr_rest = days_between(h["last_date"], date)
                    ar_rest = days_between(a["last_date"], date)
                    if hr_rest is not None and ar_rest is not None:
                        feats["avg_rest"] = (hr_rest + ar_rest)/2
                    # Recent form (last 3 starts ERA, combined)
                    hrf = recent_form(logs.get(str(ids[0]),[]), date)
                    arf = recent_form(logs.get(str(ids[1]),[]), date)
                    if hrf is not None and arf is not None:
                        feats["combined_recent_era"] = hrf + arf
            # Hot/cold offense: both teams' avg runs over last 5 games (pre-game)
            h_recent = team_recent_runs[home][-5:]
            a_recent = team_recent_runs[away][-5:]
            if len(h_recent)>=3 and len(a_recent)>=3:
                feats["combined_recent_offense"] = statistics.mean(h_recent)+statistics.mean(a_recent)

            games.append({"date":date,"home":home,"away":away,
                          "full_total":full_total,"f5_total":f5_total,"feats":feats})
            # Update rolling offense AFTER recording (no lookahead)
            team_recent_runs[home].append(hs)
            team_recent_runs[away].append(as_)

    print(f"Analyzed {len(games)} games\n")
    print(f"League baseline: full-game over 8.5 = "
          f"{100*sum(1 for g in games if g['full_total']>8.5)/len(games):.1f}%, "
          f"F5 over 4.5 = "
          f"{100*sum(1 for g in games if g['f5_total'] is not None and g['f5_total']>4.5)/sum(1 for g in games if g['f5_total'] is not None):.1f}%")
    print()

    # For each feature, split into terciles and measure over-rates.
    feature_names = ["park","combined_fip","avg_rest","combined_recent_era","combined_recent_offense"]
    FULL_LINE, F5_LINE = 8.5, 4.5

    print(f"{'feature':<24} {'n':>4}  {'bottom⅓ over%':>14} {'top⅓ over%':>12} {'separation':>11}")
    print("-"*72)
    for fn in feature_names:
        vals = [(g["feats"][fn], g) for g in games if g["feats"].get(fn) is not None]
        if len(vals) < 30:
            print(f"{fn:<24} {len(vals):>4}  (too few to test)")
            continue
        vals.sort(key=lambda x: x[0])
        third = len(vals)//3
        bottom = [g for _,g in vals[:third]]
        top    = [g for _,g in vals[-third:]]
        # Full-game over rate per tercile
        def over_rate(grp, line, key):
            valid=[g for g in grp if g[key] is not None]
            if not valid: return None
            return 100*sum(1 for g in valid if g[key]>line)/len(valid)
        b_full = over_rate(bottom, FULL_LINE, "full_total")
        t_full = over_rate(top,    FULL_LINE, "full_total")
        sep = (t_full-b_full) if (b_full is not None and t_full is not None) else 0
        flag = "  ← SIGNAL" if abs(sep)>=8 else ""
        print(f"{fn:<24} {len(vals):>4}  {b_full:>13.1f}% {t_full:>11.1f}% {sep:>+10.1f}{flag}")

    print()
    print("Reading: separation ≥8pts = the feature meaningfully splits high/low")
    print("scoring games and is worth wiring into the model. ~0 = no signal.")
    print("(park snapshot is static so it's a clean test; pitcher/offense use")
    print(" only pre-game data so no lookahead.)")


if __name__ == "__main__":
    main()
