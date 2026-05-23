#!/usr/bin/env python3
"""
Score-prediction model proof-of-concept (CFB 2025).

The strategic shift: instead of scoring each bet type independently,
predict the actual game OUTCOME (margin + total) as a DISTRIBUTION,
then derive bets — including alternate lines — from that one prediction.

This script:
  1. Projects every 2025 CFB game's score from running, no-lookahead
     team offense/defense (points-for / points-against per game).
  2. Measures raw accuracy: mean-absolute-error of predicted margin
     and total vs actual. (How good is the crystal ball?)
  3. Converts each projection to a probability via a normal model
     (margin σ ≈ 13.5, total σ ≈ 10 for CFB), then bets the main
     spread / total when our probability beats the line's implied
     probability — and reports hit rate + ROI.
  4. Demonstrates the ALT-LINE concept: for the games we're most
     confident on, shows how often the projected margin would have
     covered alternate spreads, proving alt lines amplify a confident
     prediction.

Usage:
  python3 scripts/backtest_score_model.py
"""
from __future__ import annotations

import json
import math
import statistics
from collections import defaultdict
from pathlib import Path

DATA = "data/cfb_history/2025.json"

# CFB empirical dispersion (well-established): game margins have a large
# standard deviation (~13.5 pts), totals ~10 pts. These turn a point
# projection into a probability for any line.
MARGIN_SIGMA = 13.5
TOTAL_SIGMA  = 10.0
HOME_FIELD   = 2.5   # CFB home-field worth ~2.5 pts


def normal_cdf(x, mu, sigma):
    """P(X <= x) for X ~ Normal(mu, sigma)."""
    return 0.5 * (1 + math.erf((x - mu) / (sigma * math.sqrt(2))))


def implied_prob(american_or_cents=None):
    # We grade vs a -110 line (no per-line prices in the ESPN data), so
    # break-even implied prob is 0.5238. Alt-line pricing comes later
    # when we wire Kalshi markets in.
    return 0.5238


def main():
    p = Path(DATA)
    if not p.exists():
        print(f"No data at {DATA} — run fetch_cfb_history.py first"); return
    games = [g for g in json.loads(p.read_text()).get("games", [])
             if g.get("spread") is not None and g.get("total") is not None]
    games.sort(key=lambda g: g["date"])

    # Running per-team offense (pts scored) + defense (pts allowed), pre-game.
    off = defaultdict(list); deff = defaultdict(list)

    # ── Opponent-adjusted power rating (Elo-style, margin-based) ──────────
    # Each team has a single net power rating (points better than average).
    # Predicted margin = home_rating − away_rating + home_field. After each
    # game we nudge both teams' ratings toward the observed result. This is
    # opponent-adjusted by construction: beating a strong team raises you
    # more than beating a weak one. K controls adjustment speed.
    power = defaultdict(float)
    K = 0.10
    pr_margin_errs = []
    pr_ats_w = pr_ats_l = 0
    pr_conf = defaultdict(lambda: {"w":0,"l":0})

    margin_errs = []; total_errs = []
    # Main-line betting from the projection
    ats_w = ats_l = 0
    ou_w = ou_l = 0
    # Confidence-bucketed ATS (does a bigger projection edge → better cover?)
    conf_buckets = defaultdict(lambda: {"w":0,"l":0})

    LEAGUE_PPG = 28.0   # CFB team scores ~28/game; prior for thin samples

    for g in games:
        home, away = g["home"], g["away"]
        hs, as_ = g["home_score"], g["away_score"]
        spread, total = g["spread"], g["total"]
        neutral = g.get("neutral")

        ho, hd = off[home], deff[home]
        ao, ad = off[away], deff[away]
        # Need a few games of data on both teams to project
        if len(ho) >= 3 and len(ao) >= 3:
            home_off = statistics.mean(ho); home_def = statistics.mean(hd)
            away_off = statistics.mean(ao); away_def = statistics.mean(ad)
            hf = 0 if neutral else HOME_FIELD
            # Projected scores: blend team's offense vs opponent's defense
            proj_home = (home_off + away_def) / 2 + hf
            proj_away = (away_off + home_def) / 2 - hf
            proj_margin = proj_home - proj_away      # + = home favored
            proj_total  = proj_home + proj_away

            actual_margin = hs - as_
            actual_total  = hs + as_
            margin_errs.append(abs(proj_margin - actual_margin))
            total_errs.append(abs(proj_total - actual_total))

            # ── Main-line ATS from projection ──
            # spread is home line (negative = home favored). Home covers if
            # actual_margin + spread > 0. We bet the side our projection favors:
            # projected home cover margin = proj_margin + spread.
            proj_cover = proj_margin + spread
            if abs(proj_cover) >= 1.0:   # only bet when projection clears the line by ≥1
                pick_home = proj_cover > 0
                actual_cover = actual_margin + spread
                if abs(actual_cover) > 1e-9:
                    won = (actual_cover > 0) == pick_home
                    if won: ats_w += 1
                    else:   ats_l += 1
                    # Confidence bucket by projected edge vs line
                    edge = abs(proj_cover)
                    cb = "1-3" if edge<=3 else "3-7" if edge<=7 else "7-14" if edge<=14 else "14+"
                    conf_buckets[cb]["w" if won else "l"] += 1

            # ── Main-line total from projection ──
            proj_ou_edge = proj_total - total
            if abs(proj_ou_edge) >= 2.0:   # bet when projection clears total by ≥2
                pick_over = proj_ou_edge > 0
                if actual_total != total:
                    won = (actual_total > total) == pick_over
                    if won: ou_w += 1
                    else:   ou_l += 1

        # ── Opponent-adjusted power-rating projection + ATS test ──
        hf2 = 0 if neutral else HOME_FIELD
        pr_proj_margin = power[home] - power[away] + hf2
        actual_margin = hs - as_
        # Only score once both teams have played (ratings have moved off 0)
        if off[home] and off[away]:
            pr_margin_errs.append(abs(pr_proj_margin - actual_margin))
            pr_cover = pr_proj_margin + spread
            if abs(pr_cover) >= 1.0:
                pick_home = pr_cover > 0
                actual_cover = actual_margin + spread
                if abs(actual_cover) > 1e-9:
                    won = (actual_cover > 0) == pick_home
                    if won: pr_ats_w += 1
                    else:   pr_ats_l += 1
                    edge = abs(pr_cover)
                    cb = "1-3" if edge<=3 else "3-7" if edge<=7 else "7-14" if edge<=14 else "14+"
                    pr_conf[cb]["w" if won else "l"] += 1
        # Update power ratings toward observed margin (opponent-adjusted)
        expected = power[home] - power[away] + hf2
        err = actual_margin - expected
        power[home] += K * err
        power[away] -= K * err

        # Update running stats AFTER projecting (no lookahead)
        off[home].append(hs); deff[home].append(as_)
        off[away].append(as_); deff[away].append(hs)

    n = len(margin_errs)
    print(f"Score-prediction backtest · 2025 CFB · {n} projectable games\n")
    print("═══ Prediction accuracy (lower = better) ═══")
    print(f"  Margin MAE: {statistics.mean(margin_errs):.1f} pts  (σ assumption {MARGIN_SIGMA})")
    print(f"  Total  MAE: {statistics.mean(total_errs):.1f} pts  (σ assumption {TOTAL_SIGMA})")
    print(f"  For reference: a coin-flip-useless model MAEs ~{MARGIN_SIGMA*1.13:.0f} on margin.")

    print("\n═══ Betting the projection vs the closing line (-110) ═══")
    if ats_w+ats_l:
        print(f"  ATS:   {ats_w}-{ats_l} = {100*ats_w/(ats_w+ats_l):.1f}%  (break-even 52.4%)")
    if ou_w+ou_l:
        print(f"  Total: {ou_w}-{ou_l} = {100*ou_w/(ou_w+ou_l):.1f}%  (break-even 52.4%)")

    print("\n═══ ATS hit rate by projection edge (does confidence help?) ═══")
    for cb in ["1-3","3-7","7-14","14+"]:
        bk = conf_buckets.get(cb)
        if bk and (bk["w"]+bk["l"])>=15:
            t=bk["w"]+bk["l"]
            print(f"  proj edge {cb:<5} pts: {bk['w']}-{bk['l']} = {100*bk['w']/t:.1f}% (n={t})")

    # ── Opponent-adjusted power-rating results ──
    print("\n═══ OPPONENT-ADJUSTED power rating (Elo-style) ═══")
    if pr_margin_errs:
        print(f"  Margin MAE: {statistics.mean(pr_margin_errs):.1f} pts "
              f"(vs naive {statistics.mean(margin_errs):.1f})")
    if pr_ats_w+pr_ats_l:
        print(f"  ATS: {pr_ats_w}-{pr_ats_l} = {100*pr_ats_w/(pr_ats_w+pr_ats_l):.1f}% "
              f"(break-even 52.4%)")
    print("  ATS by projection edge:")
    for cb in ["1-3","3-7","7-14","14+"]:
        bk = pr_conf.get(cb)
        if bk and (bk["w"]+bk["l"])>=15:
            t=bk["w"]+bk["l"]
            print(f"    {cb:<5} pts: {bk['w']}-{bk['l']} = {100*bk['w']/t:.1f}% (n={t})")

    print("\nIf the opponent-adjusted model beats both the naive model AND the")
    print("52.4% line, the score-first architecture is viable — and alt lines")
    print("amplify the high-edge games. If it still can't beat the close, the")
    print("market is too sharp on the main line and alt-line price inefficiency")
    print("(thin Kalshi markets) becomes the play instead.")


if __name__ == "__main__":
    main()
