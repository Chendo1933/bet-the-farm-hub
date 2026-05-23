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

import argparse
import glob
import json
import os
import re
import urllib.request
from collections import defaultdict
from pathlib import Path

GATE = 65          # current live min_calibrated_score for MLB ML
SHADOW_BAND = (60, 64)   # the +EV-but-unbet band we're paper-validating forward
# Promotion gate: enough OUT-OF-SAMPLE sample AND still clearly profitable.
PROMOTE_MIN_N   = 60
PROMOTE_MIN_ROI = 5.0    # percent
# Forward-validation cutoff. The 60-64 edge was DISCOVERED on picks through
# 2026-05-22, so promoting on those is circular (in-sample). The watch verdict
# only counts picks ON/AFTER this date — genuine out-of-sample confirmation.
VALIDATION_START = "2026-05-23"


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


def collect_graded(results: dict, since: str | None = None) -> tuple[list[tuple[int, int, bool]], dict]:
    """Return ([(score, odds, won), ...], stats) for every MLB ML pick we can
    join to a final score. Single source of truth for both the table and the
    forward-watch notification. `since` (YYYY-MM-DD) restricts to picks on/after
    that date — used for out-of-sample forward validation."""
    graded: list[tuple[int, int, bool]] = []
    stats = {"matched": 0, "unmatched": 0, "no_odds": 0}
    for f in sorted(glob.glob("data/picks/*.json")):
        date = Path(f).stem
        if since and date[:10] < since:
            continue
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
                stats["no_odds"] += 1
                continue
            res = games.get((_norm(p.get("home", "")), _norm(p.get("away", ""))))
            if not res:
                stats["unmatched"] += 1
                continue
            stats["matched"] += 1
            hs, as_ = res
            if hs == as_:
                continue
            home_won = hs > as_
            won = home_won if p.get("atsPick") == "home" else (not home_won)
            graded.append((score, odds, won))
    return graded, stats


def band_stats(graded: list[tuple[int, int, bool]], lo: int, hi: int) -> dict:
    """Win%/ROI for picks with lo <= score <= hi (hi=None → no upper bound)."""
    sub = [g for g in graded if g[0] >= lo and (hi is None or g[0] <= hi)]
    n = len(sub)
    if n == 0:
        return {"n": 0, "win_pct": 0.0, "roi_pct": 0.0}
    w = sum(1 for g in sub if g[2])
    units = sum(american_profit(g[1]) if g[2] else -1.0 for g in sub)
    return {"n": n, "win_pct": 100 * w / n, "roi_pct": 100 * units / n}


def _send_ntfy(title: str, body: str) -> bool:
    """Push a one-line watch summary. Title stays ASCII (HTTP header is
    latin-1); emoji ride in the Tags header. No-op if WEBHOOK_URL unset."""
    webhook = os.environ.get("WEBHOOK_URL", "").strip()
    if not webhook:
        print("WEBHOOK_URL not set — watch summary printed, not pushed")
        return False
    if "ntfy.sh" in webhook or "/ntfy/" in webhook:
        req = urllib.request.Request(
            webhook, data=body.encode("utf-8"),
            headers={"Title": title, "Priority": "3", "Tags": "bar_chart,microscope"},
            method="POST")
    else:
        payload = json.dumps({"content": f"{title}\n{body}",
                              "text": f"{title}\n{body}"}).encode("utf-8")
        req = urllib.request.Request(
            webhook, data=payload,
            headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=10) as r:
        return 200 <= r.status < 300


def watch(results: dict) -> None:
    """Forward-validation check on the 60-64 shadow band — the hands-off way
    to decide if the live gate should drop from 65. Prints + pushes one line."""
    # Out-of-sample only: picks on/after VALIDATION_START. This is the honest
    # forward test — the in-sample 91 picks that revealed the edge don't count.
    graded, stats = collect_graded(results, since=VALIDATION_START)
    lo, hi = SHADOW_BAND
    shadow = band_stats(graded, lo, hi)
    live = band_stats(graded, GATE, None)

    ready = shadow["n"] >= PROMOTE_MIN_N and shadow["roi_pct"] >= PROMOTE_MIN_ROI
    if shadow["n"] < PROMOTE_MIN_N:
        verdict = (f"accumulating ({shadow['n']}/{PROMOTE_MIN_N} picks since "
                   f"{VALIDATION_START}) — keep paper-validating")
        tag = "ACCUMULATING"
    elif ready:
        verdict = (f"READY to promote — {shadow['n']} picks, "
                   f"ROI {shadow['roi_pct']:+.1f}% (>= {PROMOTE_MIN_ROI:+.0f}%)")
        tag = "READY"
    else:
        verdict = (f"FAILED forward test — {shadow['n']} picks but ROI "
                   f"{shadow['roi_pct']:+.1f}% (< {PROMOTE_MIN_ROI:+.0f}%); keep gate at {GATE}")
        tag = "FAILED"

    body = (f"MLB ML gate watch · {stats['matched']} graded picks\n"
            f"shadow {lo}-{hi}: n={shadow['n']} win {shadow['win_pct']:.1f}% "
            f"ROI {shadow['roi_pct']:+.1f}%\n"
            f"live >={GATE}: n={live['n']} win {live['win_pct']:.1f}% "
            f"ROI {live['roi_pct']:+.1f}%\n"
            f"verdict: {verdict}")
    print(body)
    _send_ntfy(f"BTF cal-watch: 60-64 band {tag}", body)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--notify", action="store_true",
                    help="Forward-watch mode: push the 60-64 shadow-band record "
                         "+ promote verdict to WEBHOOK_URL (for the weekly cron).")
    args = ap.parse_args()

    results = build_results_index()

    if args.notify:
        watch(results)
        return

    graded, gstats = collect_graded(results)
    buckets = defaultdict(lambda: {"n": 0, "w": 0, "units": 0.0, "imp": 0.0})
    for score, odds, won in graded:
        b = score_bucket(score)
        bk = buckets[b]
        bk["n"] += 1
        bk["imp"] += implied(odds)
        if won:
            bk["w"] += 1; bk["units"] += american_profit(odds)
        else:
            bk["units"] -= 1.0

    print("MLB moneyline calibration audit")
    print(f"  picks matched to a final score: {gstats['matched']}  "
          f"(unmatched {gstats['unmatched']}, no-odds {gstats['no_odds']})\n")

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

    # Threshold sweep — ROI if we required score ≥ T (reuses `graded`)
    print("\n═══ Threshold sweep: ROI if we only bet score ≥ T ═══")
    print(f"  {'cutoff':<7} {'n':>4} {'win%':>6} {'ROI':>8}")
    for T in (55, 60, 62, 65, 68, 70, 72, 75):
        st = band_stats(graded, T, None)
        if st["n"] < 10:
            continue
        print(f"  ≥{T:<6} {st['n']:>4} {st['win_pct']:>5.1f}% {st['roi_pct']:>+7.1f}%")

    print("\nReads: (1) if win%/ROI rise with score → the score has real signal;")
    print("(2) edge>0 in the bet buckets → we're beating the price; (3) the")
    print("sweep shows whether 65 is the right cutoff or we're leaving money /")
    print("betting too loose.")


if __name__ == "__main__":
    main()
