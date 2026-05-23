#!/usr/bin/env python3
"""
MLB score-prediction model + backtest (the score-first architecture,
MLB sandbox).

Predicts each game's run total per team from:
  • team offense   — running runs-scored per game (chronological)
  • opp pitching   — blend of the opposing STARTER's FIP (the ~5.5 innings
                     they throw) and the opposing team's runs-allowed per
                     game (the bullpen + defense behind them)
  • park factor    — stadium run environment

Then derives margin + total and tests betting vs the lines we stored in
data/results/*.json. Unlike CFB, MLB is pitcher-dominated, so the
starter is the centerpiece of the projection.

Outputs:
  1. Accuracy: MAE of predicted total + margin vs actual.
  2. Total betting: over/under vs the line, bucketed by projection edge
     (does a bigger disagreement with the line → better hit rate?).
  3. The high-conviction subset — where alt-line betting would amplify.

All stats are computed pre-game (no lookahead): team RS/RA accumulate
chronologically, starter FIP is summed only over starts before the date.

Usage:
  python3 scripts/backtest_score_model_mlb.py
"""
from __future__ import annotations

import glob
import json
import statistics
from collections import defaultdict
from pathlib import Path
from typing import Optional

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

# Weight of the starter vs the rest of the staff in run-prevention.
# Starters throw ~5.5 of 9 innings → ~0.6 of the game.
STARTER_WEIGHT = 0.60


def _norm(n): return {"Oakland Athletics":"Athletics","Sacramento Athletics":"Athletics"}.get(n,n)


def starter_fip(logs, pid, before_date):
    starts = logs.get(str(pid), [])
    prior = [s for s in starts if s.get("date") and s["date"] < before_date]
    if not prior: return None
    ip = sum(s.get("ip",0) for s in prior)
    if ip < 1: return None
    bb=sum(s.get("bb",0) for s in prior); k=sum(s.get("k",0) for s in prior)
    hr=sum(s.get("hr",0) for s in prior)
    return ((13*hr + 3*bb - 2*k)/ip) + 3.10


def main():
    pdata = json.loads(Path("data/pitcher_data.json").read_text())
    logs = pdata.get("game_logs", {})
    by_teams = {}
    for pk,e in pdata.get("starters_by_gamePk",{}).items():
        if all([e.get("home_team"),e.get("away_team"),e.get("home_id"),e.get("away_id")]):
            by_teams[(_norm(e["home_team"]),_norm(e["away_team"]))]=(e["home_id"],e["away_id"])

    # Running team offense (RS/g) + defense (RA/g)
    rs = defaultdict(list); ra = defaultdict(list)

    total_errs=[]; margin_errs=[]
    ou_w=ou_l=0
    ou_conf=defaultdict(lambda:{"w":0,"l":0})
    ml_w=ml_l=0

    # League average runs/team/game — updated as we go for the FIP blend scale
    all_runs=[]

    files=sorted(glob.glob("data/results/*.json"))
    for f in files:
        if "/index.json" in f: continue
        d=json.loads(Path(f).read_text()); date=d.get("date",Path(f).stem)
        for g in d.get("sports",{}).get("mlb",[]):
            home=_norm(g.get("home_db") or g.get("home") or "")
            away=_norm(g.get("away_db") or g.get("away") or "")
            hs=g.get("home_score"); as_=g.get("away_score")
            line=g.get("total"); spread=g.get("spread")
            if hs is None or as_ is None or not home or not away: continue

            league_avg = statistics.mean(all_runs) if len(all_runs)>=50 else 4.3

            ho=rs[home]; hd=ra[home]; ao=rs[away]; ad=ra[away]
            proj_ok = len(ho)>=5 and len(ao)>=5
            if proj_ok:
                home_off=statistics.mean(ho); home_ra=statistics.mean(hd)
                away_off=statistics.mean(ao); away_ra=statistics.mean(ad)
                pf=PARK.get(home,1.0)

                # Opposing pitching = starter FIP (weighted) + team RA (rest)
                ids=by_teams.get((home,away))
                h_fip=a_fip=None
                if ids:
                    h_fip=starter_fip(logs,ids[0],date)
                    a_fip=starter_fip(logs,ids[1],date)
                # Away pitching faced by home offense:
                if a_fip is not None:
                    away_pitch = STARTER_WEIGHT*a_fip + (1-STARTER_WEIGHT)*away_ra
                else:
                    away_pitch = away_ra
                if h_fip is not None:
                    home_pitch = STARTER_WEIGHT*h_fip + (1-STARTER_WEIGHT)*home_ra
                else:
                    home_pitch = home_ra

                # Projected runs: blend team offense with opponent's pitching,
                # scaled by park. Home-field worth ~0.15 run in MLB.
                proj_home = ((home_off + away_pitch)/2)*pf + 0.15
                proj_away = ((away_off + home_pitch)/2)*pf - 0.15
                proj_total = proj_home + proj_away
                proj_margin = proj_home - proj_away

                actual_total=hs+as_; actual_margin=hs-as_
                total_errs.append(abs(proj_total-actual_total))
                margin_errs.append(abs(proj_margin-actual_margin))

                # ── Total betting vs the line ──
                if line is not None:
                    edge=proj_total-line
                    if abs(edge)>=0.5:
                        pick_over=edge>0
                        if actual_total!=line:
                            won=(actual_total>line)==pick_over
                            ou_w+=won; ou_l+=(not won)
                            cb="0.5-1" if abs(edge)<=1 else "1-2" if abs(edge)<=2 else "2-3" if abs(edge)<=3 else "3+"
                            ou_conf[cb]["w" if won else "l"]+=1

                # ── ML from projected margin ──
                pick_home = proj_margin > 0
                if actual_margin != 0:
                    won=(actual_margin>0)==pick_home
                    ml_w+=won; ml_l+=(not won)

            rs[home].append(hs); ra[home].append(as_)
            rs[away].append(as_); ra[away].append(hs)
            all_runs.append(hs); all_runs.append(as_)

    n=len(total_errs)
    print(f"MLB score-prediction backtest · {n} projectable games\n")
    print("═══ Accuracy (lower MAE = better) ═══")
    print(f"  Total MAE:  {statistics.mean(total_errs):.2f} runs")
    print(f"  Margin MAE: {statistics.mean(margin_errs):.2f} runs")
    print(f"  (MLB run total σ ≈ 4.5; a naive 'always 8.5' MAEs ~3.0)")

    print("\n═══ Total (O/U) betting from projection vs line ═══")
    if ou_w+ou_l:
        print(f"  Overall: {ou_w}-{ou_l} = {100*ou_w/(ou_w+ou_l):.1f}% (break-even 52.4%)")
    print("  By projection edge (runs vs the line):")
    for cb in ["0.5-1","1-2","2-3","3+"]:
        bk=ou_conf.get(cb)
        if bk and (bk["w"]+bk["l"])>=15:
            t=bk["w"]+bk["l"]
            print(f"    {cb:<6} runs: {bk['w']}-{bk['l']} = {100*bk['w']/t:.1f}% (n={t})")

    print("\n═══ Moneyline from projected margin ═══")
    if ml_w+ml_l:
        print(f"  Straight-up winner predicted: {ml_w}-{ml_l} = {100*ml_w/(ml_w+ml_l):.1f}%")

    print("\nIf total hit% climbs with projection edge, the score model has")
    print("real signal — and Kalshi's alt-total ladders (F5 + full game)")
    print("let us bet the best-priced line on high-conviction games.")


if __name__ == "__main__":
    main()
