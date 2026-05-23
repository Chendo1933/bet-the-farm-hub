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

    MODELS = ["always_over", "always_under", "pitcher_signal", "pitcher_extended"]
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

            # Compute pitcher signal
            ids = by_teams.get((home, away))
            sig = sig_ext = None
            if ids:
                h = cumulative(logs.get(str(ids[0]), []), date)
                a = cumulative(logs.get(str(ids[1]), []), date)
                if h and a:
                    sig = pitcher_signal(h, a, extended=False)
                    sig_ext = pitcher_signal(h, a, extended=True)

            picks = {
                "always_over": "over",
                "always_under": "under",
                "pitcher_signal": sig,
                "pitcher_extended": sig_ext,
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
