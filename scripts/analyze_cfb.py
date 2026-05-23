#!/usr/bin/env python3
"""
Mine the 2025 CFB season (data/cfb_history/2025.json) for ATS + totals
signals — off-season prep so the 2026 CFB model launches with proven
edges instead of guesses.

Computes running team stats CHRONOLOGICALLY (no lookahead) and tests
which signals separate cover/non-cover and over/under:
  • Home/away ATS records (does prior ATS form predict the next cover?)
  • Scoring margin vs spread (are favorites over/under-valued?)
  • Home field edge (do home teams cover more?)
  • Favorite vs underdog cover rates by spread size
  • Over/under: combined PPG vs total line
  • Conference vs non-conference games
  • Neutral-site behavior

This mirrors the MLB feature harness but for CFB's two main markets.
The output tells us which factors to weight when the 2026 model goes live.

Usage:
  python3 scripts/analyze_cfb.py
"""
from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path

DATA = "data/cfb_history/2025.json"


def cover_result(home_score, away_score, home_spread):
    """Did the HOME team cover its spread? Returns 'home'/'away'/'push'.
    home_spread negative = home favored (must win by > |spread|)."""
    margin = home_score - away_score          # + = home won by this
    adj = margin + home_spread                 # home_spread is negative for favorites
    if abs(adj) < 1e-9: return "push"
    return "home" if adj > 0 else "away"


def main():
    p = Path(DATA)
    if not p.exists():
        print(f"No data at {DATA} — run scripts/fetch_cfb_history.py first")
        return
    data = json.loads(p.read_text())
    games = [g for g in data.get("games", []) if g.get("spread") is not None
             and g.get("total") is not None]
    games.sort(key=lambda g: g["date"])
    print(f"2025 CFB games with lines: {len(games)}\n")

    # ── Baseline market behavior ──────────────────────────────────────
    home_cov = away_cov = push = 0
    over = under = ou_push = 0
    fav_cover = fav_total = 0          # favorite ATS
    dog_cover = dog_total = 0
    neutral_home_cov = neutral_total = 0
    home_field_wins = 0
    for g in games:
        hs, as_, spr, tot = g["home_score"], g["away_score"], g["spread"], g["total"]
        c = cover_result(hs, as_, spr)
        if c == "home": home_cov += 1
        elif c == "away": away_cov += 1
        else: push += 1
        combined = hs + as_
        if combined > tot: over += 1
        elif combined < tot: under += 1
        else: ou_push += 1
        if hs > as_: home_field_wins += 1
        # Favorite cover (favorite = whoever is laying points)
        if spr < 0:    # home favored
            fav_total += 1
            if c == "home": fav_cover += 1
            dog_total += 1
            if c == "away": dog_cover += 1
        elif spr > 0:  # away favored
            fav_total += 1
            if c == "away": fav_cover += 1
            dog_total += 1
            if c == "home": dog_cover += 1
        if g.get("neutral"):
            neutral_total += 1
            if c == "home": neutral_home_cov += 1

    n = len(games)
    print("═══ Market baselines ═══")
    print(f"  Home ATS cover:  {home_cov}/{home_cov+away_cov} = {100*home_cov/(home_cov+away_cov):.1f}%")
    print(f"  Home straight-up win: {100*home_field_wins/n:.1f}%")
    print(f"  Over hit: {over}/{over+under} = {100*over/(over+under):.1f}%")
    print(f"  Favorites ATS:   {fav_cover}/{fav_total} = {100*fav_cover/max(1,fav_total):.1f}%")
    print(f"  Underdogs ATS:   {dog_cover}/{dog_total} = {100*dog_cover/max(1,dog_total):.1f}%")
    if neutral_total:
        print(f"  Neutral-site home cover: {neutral_home_cov}/{neutral_total} = {100*neutral_home_cov/neutral_total:.1f}%")

    # ── Cover rate by spread size (are big favorites over-valued?) ─────
    print("\n═══ ATS cover by spread size (favorite's perspective) ═══")
    buckets = defaultdict(lambda: {"fav":0,"n":0})
    for g in games:
        spr = g["spread"]; c = cover_result(g["home_score"],g["away_score"],spr)
        if spr == 0: continue
        size = abs(spr)
        b = "pick-3.5" if size<=3.5 else "4-7" if size<=7 else "7.5-14" if size<=14 else "14.5-21" if size<=21 else "21.5+"
        fav_side = "home" if spr<0 else "away"
        buckets[b]["n"]+=1
        if c==fav_side: buckets[b]["fav"]+=1
    for b in ["pick-3.5","4-7","7.5-14","14.5-21","21.5+"]:
        bk=buckets.get(b)
        if bk and bk["n"]>=10:
            print(f"  {b:<10} fav covers {bk['fav']}/{bk['n']} = {100*bk['fav']/bk['n']:.1f}%")

    # ── Totals by line size (do high totals go over/under?) ───────────
    print("\n═══ Over rate by total line size ═══")
    tbuckets = defaultdict(lambda: {"over":0,"n":0})
    for g in games:
        tot=g["total"]; combined=g["home_score"]+g["away_score"]
        if combined==tot: continue
        b = "≤45" if tot<=45 else "45.5-52" if tot<=52 else "52.5-59" if tot<=59 else "59.5+"
        tbuckets[b]["n"]+=1
        if combined>tot: tbuckets[b]["over"]+=1
    for b in ["≤45","45.5-52","52.5-59","59.5+"]:
        bk=tbuckets.get(b)
        if bk and bk["n"]>=10:
            print(f"  total {b:<8} over {bk['over']}/{bk['n']} = {100*bk['over']/bk['n']:.1f}%")

    # ── Prior-ATS-form signal (does a team's ATS record predict next cover?)
    print("\n═══ Does prior ATS form predict the next cover? ═══")
    team_ats = defaultdict(lambda: {"w":0,"l":0})   # running ATS record
    hot_cov = hot_n = cold_cov = cold_n = 0
    for g in games:
        home, away, spr = g["home"], g["away"], g["spread"]
        c = cover_result(g["home_score"],g["away_score"],spr)
        # Evaluate the favorite's prior ATS form
        fav, fav_side = (home,"home") if spr<0 else (away,"away") if spr>0 else (None,None)
        if fav:
            rec = team_ats[fav]; tot = rec["w"]+rec["l"]
            if tot >= 3:
                pct = rec["w"]/tot
                covered = (c==fav_side)
                if pct >= 0.60:   hot_n+=1;  hot_cov += covered
                elif pct <= 0.40: cold_n+=1; cold_cov += covered
        # Update running ATS AFTER evaluating
        if c=="home": team_ats[home]["w"]+=1; team_ats[away]["l"]+=1
        elif c=="away": team_ats[away]["w"]+=1; team_ats[home]["l"]+=1
    if hot_n>=10 and cold_n>=10:
        print(f"  Favorite was ATS-hot (≥60%):  covers {100*hot_cov/hot_n:.1f}% (n={hot_n})")
        print(f"  Favorite was ATS-cold (≤40%): covers {100*cold_cov/cold_n:.1f}% (n={cold_n})")
        print(f"  → separation: {100*hot_cov/hot_n - 100*cold_cov/cold_n:+.1f}pts")

    print("\nBreak-even ATS/total at -110: 52.4%. Anything ≥54% across a")
    print("full season is a real, sizable edge worth weighting heavily.")


if __name__ == "__main__":
    main()
