#!/usr/bin/env python3
"""
MLB moneyline CALIBRATION audit.

The question: when the hub says a team is a `score100 = 65` pick, does that
team actually win ~65% of the time — and more importantly, is backing it
+EV at the price we'd pay? The live gate bets MLB ML at calibrated score
≥ 65, so we need to know that threshold is well-placed.

Method (no lookahead needed — these are already-logged historical picks):
  1. Join every logged MLB ML pick (data/picks/*.json) to its final score
     (data/results/*.json) by date + teams.
  2. Bucket by score100. Per bucket compute:
       • actual win %        — did the picked team win?
       • market-implied %    — from the odds in the pick label (with vig)
       • edge                — win% − implied% (are we beating the price?)
       • ROI                 — flat 1u per pick at the pick's American odds
  3. Two reads:
       • MONOTONICITY — does win% / ROI climb with score? (signal vs noise)
       • THRESHOLD    — what does the cal≥65 gate actually select, and is a
                        different cutoff better?

Usage:
  python3 scripts/analyze_calibration_mlb.py
"""
from __future__ import annotations

import glob
import json
import re
from collections import defaultdict
from pathlib import Path

GATE = 65   # current live min_calibrated_score for MLB ML


def _norm(n: str) -> str:
    return {"Oakland Athletics": "Athletics",
            "Sacramento Athletics": "Athletics"}.get(n, n)


def parse_odds(label: str | None) -> int | None:
    m = re.search(r"\(([+-]\d+)\)", label or "")
    return int(m.group(1)) if m else None


def american_profit(ml: int, stake: float = 1.0) -> float:
    return stake * 100 / (-ml) if ml < 0 else stake * ml / 100


def implied(ml: int) -> float:
    return (-ml) / ((-ml) + 100) if ml < 0 else 100 / (ml + 100)


def score_bucket(s: int) -> str:
    if s < 50:  return "<50"
    if s < 55:  return "50-54"
    if s < 60:  return "55-59"
    if s < 65:  return "60-64"
    if s < 70:  return "65-69"
    if s < 75:  return "70-74"
    if s < 80:  return "75-79"
    return "80+"


BUCKET_ORDER = ["<50", "50-54", "55-59", "60-64", "65-69", "70-74", "75-79", "80+"]


def build_results_index() -> dict:
    """date -> {(home,away): (home_score, away_score)} for MLB."""
    idx: dict[str, dict] = {}
    for f in glob.glob("data/results/*.json"):
        if "/index.json" in f:
            continue
        d = json.loads(Path(f).read_text())
        date = d.get("date", Path(f).stem)
        games = {}
        for g in d.get("sports", {}).get("mlb", []):
            if not isinstance(g, dict):
                continue
            h = _norm(g.get("home_db") or g.get("home") or "")
            a = _norm(g.get("away_db") or g.get("away") or "")
            hs = g.get("home_score"); as_ = g.get("away_score")
            if hs is None or as_ is None:
                continue
            games[(h, a)] = (hs, as_)
        idx[date] = games
    return idx


def main():
    results = build_results_index()

    buckets = defaultdict(lambda: {"n": 0, "w": 0, "units": 0.0, "imp": 0.0})
    matched = unmatched = no_odds = 0

    for f in sorted(glob.glob("data/picks/*.json")):
        date = Path(f).stem
        d = json.loads(Path(f).read_text())
        games = results.get(date, {})
        for p in d.get("picks", []):
            if p.get("sport") != "MLB" or p.get("betType") != "ml":
                continue
            score = p.get("score100")
            if score is None:
                continue
            odds = parse_odds(p.get("pickLabel"))
            if odds is None:
                no_odds += 1
                continue
            home = _norm(p.get("home", "")); away = _norm(p.get("away", ""))
            side = p.get("atsPick")
            res = games.get((home, away))
            if not res:
                unmatched += 1
                continue
            matched += 1
            hs, as_ = res
            if hs == as_:
                continue
            home_won = hs > as_
            pick_won = home_won if side == "home" else (not home_won)
            b = score_bucket(score)
            bk = buckets[b]
            bk["n"] += 1
            bk["imp"] += implied(odds)
            if pick_won:
                bk["w"] += 1; bk["units"] += american_profit(odds)
            else:
                bk["units"] -= 1.0

    print("MLB moneyline calibration audit")
    print(f"  picks matched to a final score: {matched}  "
          f"(unmatched {unmatched}, no-odds {no_odds})\n")

    print("═══ Win% & ROI by calibrated score bucket ═══")
    print(f"  {'score':<7} {'n':>4} {'win%':>6} {'impl%':>6} {'edge':>6} {'ROI':>8}")
    tot = {"n": 0, "w": 0, "units": 0.0}
    gate = {"n": 0, "w": 0, "units": 0.0}
    for b in BUCKET_ORDER:
        bk = buckets.get(b)
        if not bk or bk["n"] < 8:
            if bk:
                print(f"  {b:<7} {bk['n']:>4}   (n<8, skipped)")
            continue
        n = bk["n"]; win = 100 * bk["w"] / n
        impl = 100 * bk["imp"] / n
        roi = 100 * bk["units"] / n
        print(f"  {b:<7} {n:>4} {win:>5.1f}% {impl:>5.1f}% {win-impl:>+5.1f} {roi:>+7.1f}%")
        tot["n"] += n; tot["w"] += bk["w"]; tot["units"] += bk["units"]
        if b in ("65-69", "70-74", "75-79", "80+"):
            gate["n"] += n; gate["w"] += bk["w"]; gate["units"] += bk["units"]

    if tot["n"]:
        print(f"\n  ALL picks: {tot['n']} · win {100*tot['w']/tot['n']:.1f}% · "
              f"ROI {100*tot['units']/tot['n']:+.1f}%")
    if gate["n"]:
        print(f"  cal≥{GATE} (what we BET): {gate['n']} · "
              f"win {100*gate['w']/gate['n']:.1f}% · ROI {100*gate['units']/gate['n']:+.1f}%")

    # Threshold sweep — ROI if we required score ≥ T
    print("\n═══ Threshold sweep: ROI if we only bet score ≥ T ═══")
    print(f"  {'cutoff':<7} {'n':>4} {'win%':>6} {'ROI':>8}")
    raw = []
    for f in sorted(glob.glob("data/picks/*.json")):
        date = Path(f).stem
        d = json.loads(Path(f).read_text())
        games = results.get(date, {})
        for p in d.get("picks", []):
            if p.get("sport") != "MLB" or p.get("betType") != "ml":
                continue
            score = p.get("score100"); odds = parse_odds(p.get("pickLabel"))
            if score is None or odds is None:
                continue
            res = games.get((_norm(p.get("home", "")), _norm(p.get("away", ""))))
            if not res or res[0] == res[1]:
                continue
            home_won = res[0] > res[1]
            won = home_won if p.get("atsPick") == "home" else (not home_won)
            raw.append((score, odds, won))
    for T in (55, 60, 62, 65, 68, 70, 72, 75):
        sub = [r for r in raw if r[0] >= T]
        if len(sub) < 10:
            continue
        n = len(sub); w = sum(1 for r in sub if r[2])
        units = sum(american_profit(r[1]) if r[2] else -1.0 for r in sub)
        print(f"  ≥{T:<6} {n:>4} {100*w/n:>5.1f}% {100*units/n:>+7.1f}%")

    print("\nReads: (1) if win%/ROI rise with score → the score has real signal;")
    print("(2) edge>0 in the bet buckets → we're beating the price; (3) the")
    print("sweep shows whether 65 is the right cutoff or we're leaving money /")
    print("betting too loose.")


if __name__ == "__main__":
    main()
