#!/usr/bin/env python3
"""
Test NHL underdog moneyline value — the hockey counterpart to the MLB
underdog study. NHL is where the FAVORITE-LONGSHOT BIAS is actually
documented (heavy public money on chalk pushes dog prices up), so unlike
MLB we expect (A) to show real signal.

Two distinct things to test:

  A. FAVORITE-LONGSHOT BIAS (market structure, no model needed):
     Are underdogs undervalued AS A CLASS? Bucket every game by the
     underdog's price and compute ROI of blindly backing the dog.
     If small dogs (+100..+150) show positive ROI, the public's
     favorite-bias is exploitable on its own.

  B. MODEL-EDGE on dogs (goals-based win model):
     A simple no-lookahead model gives P(team wins) from running
     goals-for / goals-against and the margin distribution. The market
     gives an implied P from the de-vigged moneyline. When our P(dog) >
     market-implied P(dog), is backing the dog +EV? You don't need the
     dog to WIN — just to be UNDERVALUED.

Inputs: data/nhl_moneylines.json + data/results/*.json.

Usage:
  python3 scripts/analyze_underdog_nhl.py
"""
from __future__ import annotations

import glob
import json
import math
import statistics
from collections import defaultdict
from pathlib import Path

MARGIN_SIGMA = 2.3   # NHL goal-margin std dev (~2.3 goals)
HOME_ICE = 0.20      # home advantage in goals


def american_to_prob(ml: float) -> float:
    """Implied win prob from an American moneyline (with vig)."""
    if ml < 0: return (-ml) / ((-ml) + 100)
    return 100 / (ml + 100)


def american_profit(ml: float, stake: float = 1.0) -> float:
    """Profit on a 1-unit WIN at this moneyline."""
    if ml < 0: return stake * 100 / (-ml)
    return stake * ml / 100


def normal_cdf(x, mu, sigma):
    return 0.5*(1+math.erf((x-mu)/(sigma*math.sqrt(2))))


def main():
    mlp = Path("data/nhl_moneylines.json")
    if not mlp.exists():
        print("Run fetch_nhl_moneylines.py first"); return
    ml_by_gid = json.loads(mlp.read_text())

    gf = defaultdict(list); ga = defaultdict(list)   # running goals for / against

    # A. favorite-longshot buckets
    dog_buckets = defaultdict(lambda: {"n": 0, "wins": 0, "units": 0.0})
    # B. model-edge on dogs
    edge_buckets = defaultdict(lambda: {"n": 0, "wins": 0, "units": 0.0})

    for f in sorted(glob.glob("data/results/*.json")):
        if "/index.json" in f: continue
        d = json.loads(Path(f).read_text()); date = d.get("date", Path(f).stem)
        for g in d.get("sports", {}).get("nhl", []):
            if not isinstance(g, dict): continue
            gid = str(g.get("game_id") or "")
            home = g.get("home_db") or g.get("home") or ""
            away = g.get("away_db") or g.get("away") or ""
            hs = g.get("home_score"); as_ = g.get("away_score")
            mlrec = ml_by_gid.get(gid)
            if hs is None or as_ is None or not mlrec:
                if hs is not None:
                    gf[home].append(hs); ga[home].append(as_)
                    gf[away].append(as_); ga[away].append(hs)
                continue
            home_ml = mlrec["home_ml"]; away_ml = mlrec["away_ml"]
            home_won = hs > as_

            # ── A. favorite-longshot: back whichever side is the dog (+odds) ──
            if home_ml > 0:   dog_side, dog_ml, dog_won = "home", home_ml, home_won
            elif away_ml > 0: dog_side, dog_ml, dog_won = "away", away_ml, (not home_won)
            else:             dog_side = None
            if dog_side and hs != as_:
                b = ("+100-120" if dog_ml <= 120 else "+120-150" if dog_ml <= 150
                     else "+150-200" if dog_ml <= 200 else "+200+")
                bk = dog_buckets[b]; bk["n"] += 1
                if dog_won: bk["wins"] += 1; bk["units"] += american_profit(dog_ml)
                else: bk["units"] -= 1.0

            # ── B. model edge: our P(dog) vs market implied P(dog) ──
            if len(gf[home]) >= 5 and len(gf[away]) >= 5 and dog_side and hs != as_:
                ho = statistics.mean(gf[home]); hd = statistics.mean(ga[home])
                ao = statistics.mean(gf[away]); ad = statistics.mean(ga[away])
                proj_home = (ho + ad) / 2 + HOME_ICE
                proj_away = (ao + hd) / 2 - HOME_ICE
                proj_margin = proj_home - proj_away
                our_p_home = normal_cdf(0, -proj_margin, MARGIN_SIGMA)  # P(home win)
                our_p_dog = our_p_home if dog_side == "home" else (1 - our_p_home)
                imp_home = american_to_prob(home_ml); imp_away = american_to_prob(away_ml)
                vig = imp_home + imp_away
                mkt_p_dog = (imp_home / vig) if dog_side == "home" else (imp_away / vig)
                edge = our_p_dog - mkt_p_dog
                if edge >= 0.03:
                    eb = "3-6%" if edge <= 0.06 else "6-10%" if edge <= 0.10 else "10%+"
                    bk = edge_buckets[eb]; bk["n"] += 1
                    if dog_won: bk["wins"] += 1; bk["units"] += american_profit(dog_ml)
                    else: bk["units"] -= 1.0

            gf[home].append(hs); ga[home].append(as_)
            gf[away].append(as_); ga[away].append(hs)

    print(f"NHL underdog ML analysis · {len(ml_by_gid)} games with moneylines\n")
    print("═══ A. Favorite-longshot bias: blindly back every underdog ═══")
    print(f"  {'dog price':<12} {'n':>4} {'win%':>7} {'ROI':>8}")
    tot_n = tot_u = 0
    for b in ["+100-120", "+120-150", "+150-200", "+200+"]:
        bk = dog_buckets.get(b)
        if bk and bk["n"] >= 10:
            roi = 100*bk["units"]/bk["n"]
            print(f"  {b:<12} {bk['n']:>4} {100*bk['wins']/bk['n']:>6.1f}% {roi:>+7.1f}%")
            tot_n += bk["n"]; tot_u += bk["units"]
    if tot_n: print(f"  {'ALL dogs':<12} {tot_n:>4} {'':>7} {100*tot_u/tot_n:>+7.1f}%")

    print("\n═══ B. Model-edge dogs: bet only when OUR P(dog) > market ═══")
    print(f"  {'edge':<8} {'n':>4} {'win%':>7} {'ROI':>8}")
    eb_n = eb_u = 0
    for b in ["3-6%", "6-10%", "10%+"]:
        bk = edge_buckets.get(b)
        if bk and bk["n"] >= 10:
            roi = 100*bk["units"]/bk["n"]
            print(f"  {b:<8} {bk['n']:>4} {100*bk['wins']/bk['n']:>6.1f}% {roi:>+7.1f}%")
            eb_n += bk["n"]; eb_u += bk["units"]
    if eb_n: print(f"  {'ALL':<8} {eb_n:>4} {'':>7} {100*eb_u/eb_n:>+7.1f}%")

    print("\nKey: positive ROI in (A) = the market itself underprices dogs")
    print("(favorite-longshot bias) — exploitable without any model. Positive")
    print("ROI in (B) beating (A) = our model ADDS value selecting which dogs.")


if __name__ == "__main__":
    main()
