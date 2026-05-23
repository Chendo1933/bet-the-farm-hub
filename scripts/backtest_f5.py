#!/usr/bin/env python3
"""
MLB First-5-Innings (F5) Over/Under backtest.

This is the CLEANEST backtest we can run because both inputs are
genuine, no-lookahead historical data:
  • Pitcher stats: data/pitcher_data.json game logs, summed only over
    starts STRICTLY BEFORE each game's date (no future info).
  • F5 outcomes: f5_total field in data/results/*.json, backfilled
    from MLB Stats API linescores (innings 1-5 actual runs).

It replays the exact pitcher-tier signal the live F5 paper generator
uses (scripts/kalshi/dry_run.py:_generate_f5_paper_orders) and grades
it against the actual F5 result vs a 4.5 line — which the data shows
is a true coin-flip line (50.5% of games go over 4.5 F5 runs).

Models compared:
  always_over / always_under  — sanity baselines
  pitcher_signal              — both aces → Under, both weak → Over
  pitcher_extended            — also fires on both-above-avg / both-below-avg
                                (looser; more picks, possibly noisier)

Usage:
  python3 scripts/backtest_f5.py
  python3 scripts/backtest_f5.py --line 4.5
  python3 scripts/backtest_f5.py --csv f5_results.csv
"""
from __future__ import annotations

import argparse
import csv
import glob
import json
from pathlib import Path
from typing import Optional

PITCHER_DATA = "data/pitcher_data.json"


def units_for(outcome: str) -> float:
    if outcome == "win":  return 1.00 / 1.10
    if outcome == "loss": return -1.00
    return 0.0


def load_pitcher_data() -> dict:
    try:
        return json.loads(Path(PITCHER_DATA).read_text())
    except FileNotFoundError:
        return {"starters_by_gamePk": {}, "game_logs": {}}


def _norm(name: str) -> str:
    fixes = {"Oakland Athletics": "Athletics", "Sacramento Athletics": "Athletics"}
    return fixes.get(name, name)


def cumulative(starts: list, before_date: str) -> Optional[dict]:
    prior = [s for s in (starts or []) if s.get("date") and s["date"] < before_date]
    if not prior: return None
    ip = sum(s.get("ip", 0) for s in prior)
    if ip < 1: return None
    er = sum(s.get("er", 0) for s in prior)
    bb = sum(s.get("bb", 0) for s in prior)
    k  = sum(s.get("k", 0) for s in prior)
    hr = sum(s.get("hr", 0) for s in prior)
    return {
        "era": (9*er)/ip,
        "fip": ((13*hr + 3*bb - 2*k)/ip) + 3.10,
        "starts": len(prior),
        "last_date": prior[-1]["date"],
    }


def tier(v: float) -> str:
    if v <= 3.25: return "elite"
    if v <= 4.00: return "quality"
    if v <= 4.75: return "average"
    return "weak"


def pitcher_signal(h_stats, a_stats, extended=False) -> Optional[str]:
    """Returns 'over', 'under', or None. `extended` loosens the gate to
    also fire on both-above-avg / both-below-avg matchups."""
    use_fip = h_stats["starts"] >= 3 and a_stats["starts"] >= 3
    hv = h_stats["fip"] if use_fip else h_stats["era"]
    av = a_stats["fip"] if use_fip else a_stats["era"]
    ht, at_ = tier(hv), tier(av)
    both_good = ht in ("elite","quality") and at_ in ("elite","quality")
    both_weak = ht == "weak" and at_ == "weak"
    if both_good: return "under"
    if both_weak: return "over"
    if extended:
        # Looser: both ≤4.00 (good-ish) → under, both ≥4.00 → over
        if hv <= 4.00 and av <= 4.00: return "under"
        if hv >= 4.00 and av >= 4.00: return "over"
    return None


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


def _days_between(d1, d2):
    from datetime import datetime
    try:
        return (datetime.strptime(d2,"%Y-%m-%d") - datetime.strptime(d1,"%Y-%m-%d")).days
    except Exception:
        return None


def _recent_era(starts, before_date, n=3):
    prior = [s for s in (starts or []) if s.get("date") and s["date"] < before_date]
    if len(prior) < n: return None
    rec = prior[-n:]
    ip = sum(s.get("ip",0) for s in rec)
    if ip < 1: return None
    return 9*sum(s.get("er",0) for s in rec)/ip


def projection_signal(h_stats, a_stats, h_log, a_log, home, away, date, line):
    """
    Projection-based F5 model: estimate expected F5 runs from starter
    quality, then tilt for rest + recent form + park, and bet over/under
    vs the line. Returns 'over'/'under'/None.

    expected F5 runs per starter ≈ (ERA × 5/9), i.e. their per-9 rate
    scaled to 5 innings. Summed across both starters = base F5 projection.
    """
    use_fip = h_stats["starts"] >= 3 and a_stats["starts"] >= 3
    hv = h_stats["fip"] if use_fip else h_stats["era"]
    av = a_stats["fip"] if use_fip else a_stats["era"]
    proj = (hv * 5/9) + (av * 5/9)

    # Rest tilt — long rest suppresses, short rest inflates (from feature test:
    # ≤4 days → 54% over, 6.5+ → 42% over). Applied as a small multiplier.
    h_rest = _days_between(h_stats["last_date"], date)
    a_rest = _days_between(a_stats["last_date"], date)
    if h_rest is not None and a_rest is not None:
        avg_rest = (h_rest + a_rest)/2
        if avg_rest >= 6:   proj *= 0.93   # extra-rested = fresher = fewer runs
        elif avg_rest <= 4: proj *= 1.06   # short rest = more runs

    # Recent-form tilt — if both starters are pitching worse than their
    # season line lately, nudge up; better lately, nudge down.
    hrf = _recent_era(h_log, date); arf = _recent_era(a_log, date)
    if hrf is not None and arf is not None:
        recent_avg = (hrf + arf)/2
        season_avg = (h_stats["era"] + a_stats["era"])/2
        if recent_avg - season_avg >= 1.0:   proj *= 1.05   # trending bad
        elif season_avg - recent_avg >= 1.0: proj *= 0.96   # trending good

    # Park tilt
    pf = PARK.get(home)
    if pf is not None:
        proj *= pf

    # Bet only when the projection clears the line by a meaningful margin
    # (avoid coin-flip calls). 0.4 runs ≈ the noise floor for F5.
    if proj >= line + 0.4: return "over"
    if proj <= line - 0.4: return "under"
    return None


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--line", type=float, default=4.5,
                    help="F5 over/under line to grade against (default 4.5 — a coin flip)")
    ap.add_argument("--csv", help="Dump per-game results")
    args = ap.parse_args()

    pdata = load_pitcher_data()
    logs = pdata.get("game_logs", {})
    # Build (home, away) → (h_id, a_id) from starter cache
    by_teams = {}
    for pk, e in pdata.get("starters_by_gamePk", {}).items():
        ht, at_ = e.get("home_team"), e.get("away_team")
        if ht and at_ and e.get("home_id") and e.get("away_id"):
            by_teams[(_norm(ht), _norm(at_))] = (e["home_id"], e["away_id"])

    files = sorted(glob.glob("data/results/*.json"))
    files = [f for f in files if "/index.json" not in f]

    MODELS = ["always_over", "always_under", "pitcher_signal", "pitcher_extended", "projection_v2"]
    tally = {m: {"picks":0,"wins":0,"losses":0,"units":0.0} for m in MODELS}
    csv_rows = []
    graded = 0

    for fpath in files:
        data = json.loads(Path(fpath).read_text())
        date = data.get("date", Path(fpath).stem)
        for g in data.get("sports", {}).get("mlb", []):
            if not g.get("f5_complete"): continue
            f5 = g.get("f5_total")
            if f5 is None: continue
            home = _norm(g.get("home_db") or g.get("home") or "")
            away = _norm(g.get("away_db") or g.get("away") or "")
            graded += 1
            actual_over = f5 > args.line

            # Compute pitcher signals
            ids = by_teams.get((home, away))
            sig = sig_ext = proj_sig = None
            if ids:
                h_log = logs.get(str(ids[0]), []); a_log = logs.get(str(ids[1]), [])
                h = cumulative(h_log, date)
                a = cumulative(a_log, date)
                if h and a:
                    sig = pitcher_signal(h, a, extended=False)
                    sig_ext = pitcher_signal(h, a, extended=True)
                    proj_sig = projection_signal(h, a, h_log, a_log, home, away, date, args.line)

            picks = {
                "always_over": "over",
                "always_under": "under",
                "pitcher_signal": sig,
                "pitcher_extended": sig_ext,
                "projection_v2": proj_sig,
            }
            for m, pick in picks.items():
                if pick is None: continue
                outcome = "win" if (pick=="over")==actual_over else "loss"
                tally[m]["picks"] += 1
                tally[m]["wins" if outcome=="win" else "losses"] += 1
                tally[m]["units"] += units_for(outcome)

            if args.csv and sig:
                csv_rows.append({"date":date,"home":home,"away":away,
                                 "f5_total":f5,"line":args.line,
                                 "pick":sig,"correct":(sig=="over")==actual_over})

    print(f"F5 backtest · line {args.line} · {graded} complete games\n")
    print(f"{'model':<20} {'picks':>6} {'W-L':>9} {'hit%':>8} {'units':>9} {'ROI':>8}")
    print("-"*64)
    for m in MODELS:
        t = tally[m]; n = t["picks"]; wl = t["wins"]+t["losses"]
        hit = 100*t["wins"]/wl if wl else 0
        roi = 100*t["units"]/(n*1.10) if n else 0
        print(f"{m:<20} {n:>6} {t['wins']}-{t['losses']:<5} {hit:>7.1f}% {t['units']:>+8.2f}u {roi:>+7.1f}%")
    print(f"\nBreak-even at -110: 52.38% hit / 0% ROI")

    if args.csv and csv_rows:
        with open(args.csv,"w",newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(csv_rows[0].keys()))
            w.writeheader(); w.writerows(csv_rows)
        print(f"\nCSV → {args.csv}")


if __name__ == "__main__":
    main()
