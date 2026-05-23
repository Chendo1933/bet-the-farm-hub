#!/usr/bin/env python3
"""
Test MLB underdog moneyline value — the question behind "we've never
predicted an underdog to win."

Two distinct things to test:

  A. FAVORITE-LONGSHOT BIAS (market structure, no model needed):
     Are underdogs undervalued AS A CLASS? Bucket every game by the
     underdog's price and compute ROI of blindly backing the dog.
     If small dogs (+100..+140) show positive ROI, the public's
     favorite-bias is exploitable on its own.

  B. MODEL-EDGE on dogs (combine score-first + probability):
     Our score model gives P(team wins) from the margin distribution.
     The market gives an implied P from the de-vigged moneyline.
     When our P(dog) > market-implied P(dog), is backing the dog +EV?
     This is the alt-line / value idea applied to ML — you don't need
     the dog to WIN, just to be UNDERVALUED.

Inputs: data/mlb_moneylines.json + data/results/*.json + pitcher data.

Usage:
  python3 scripts/analyze_underdog_ml.py
"""
from __future__ import annotations

import glob
import json
import math
import statistics
from collections import defaultdict
from pathlib import Path
from typing import Optional

MARGIN_SIGMA = 4.0   # MLB run-margin std dev (~4 runs)


def american_to_prob(ml: float) -> float:
    """Implied win prob from an American moneyline (with vig)."""
    if ml < 0: return (-ml) / ((-ml) + 100)
    return 100 / (ml + 100)


def american_profit(ml: float, stake: float = 1.0) -> float:
    """Profit on a 1-unit WIN at this moneyline."""
    if ml < 0: return stake * 100 / (-ml)
    return stake * ml / 100


def _norm(n): return {"Oakland Athletics":"Athletics","Sacramento Athletics":"Athletics"}.get(n,n)


def starter_fip(logs, pid, before):
    pr=[s for s in logs.get(str(pid),[]) if s.get("date") and s["date"]<before]
    if not pr: return None
    ip=sum(s.get("ip",0) for s in pr)
    if ip<1: return None
    return ((13*sum(s.get("hr",0)for s in pr)+3*sum(s.get("bb",0)for s in pr)
             -2*sum(s.get("k",0)for s in pr))/ip)+3.10


PARK={'Colorado Rockies':1.28,'Boston Red Sox':1.08,'Cincinnati Reds':1.05,'Chicago Cubs':1.04,
      'Texas Rangers':1.05,'San Diego Padres':0.93,'San Francisco Giants':0.92,'Seattle Mariners':0.93}


def normal_cdf(x, mu, sigma):
    return 0.5*(1+math.erf((x-mu)/(sigma*math.sqrt(2))))


def main():
    mlp = Path("data/mlb_moneylines.json")
    if not mlp.exists():
        print("Run fetch_mlb_moneylines.py first"); return
    ml_by_gid = json.loads(mlp.read_text())

    pdata = json.loads(Path("data/pitcher_data.json").read_text())
    logs = pdata.get("game_logs", {})
    by_teams={}
    for pk,e in pdata.get("starters_by_gamePk",{}).items():
        if all([e.get("home_team"),e.get("away_team"),e.get("home_id"),e.get("away_id")]):
            by_teams[(_norm(e["home_team"]),_norm(e["away_team"]))]=(e["home_id"],e["away_id"])

    rs=defaultdict(list); ra=defaultdict(list)

    # A. favorite-longshot buckets
    dog_buckets = defaultdict(lambda:{"n":0,"wins":0,"units":0.0})
    # B. model-edge on dogs
    edge_buckets = defaultdict(lambda:{"n":0,"wins":0,"units":0.0})

    for f in sorted(glob.glob("data/results/*.json")):
        if "/index.json" in f: continue
        d=json.loads(Path(f).read_text()); date=d.get("date",Path(f).stem)
        for g in d.get("sports",{}).get("mlb",[]):
            gid=str(g.get("game_id") or "")
            home=_norm(g.get("home_db")or g.get("home")or""); away=_norm(g.get("away_db")or g.get("away")or"")
            hs=g.get("home_score"); as_=g.get("away_score")
            mlrec=ml_by_gid.get(gid)
            if hs is None or as_ is None or not mlrec:
                # still update running stats below
                if hs is not None:
                    rs[home].append(hs); ra[home].append(as_); rs[away].append(as_); ra[away].append(hs)
                continue
            home_ml=mlrec["home_ml"]; away_ml=mlrec["away_ml"]
            home_won = hs > as_

            # ── A. favorite-longshot: back whichever side is the dog (+odds) ──
            if home_ml > 0:   dog_side, dog_ml, dog_won = "home", home_ml, home_won
            elif away_ml > 0: dog_side, dog_ml, dog_won = "away", away_ml, (not home_won)
            else:             dog_side = None
            if dog_side and as_ != hs:
                b = "+100-120" if dog_ml<=120 else "+120-150" if dog_ml<=150 else "+150-200" if dog_ml<=200 else "+200+"
                bk=dog_buckets[b]; bk["n"]+=1
                if dog_won: bk["wins"]+=1; bk["units"]+=american_profit(dog_ml)
                else: bk["units"]-=1.0

            # ── B. model edge: our P(dog) vs market implied P(dog) ──
            if len(rs[home])>=5 and len(rs[away])>=5 and dog_side and as_!=hs:
                ho=statistics.mean(rs[home]); hd=statistics.mean(ra[home])
                ao=statistics.mean(rs[away]); ad=statistics.mean(ra[away])
                pf=PARK.get(home,1.0)
                ids=by_teams.get((home,away)); hfip=starter_fip(logs,ids[0],date) if ids else None
                afip=starter_fip(logs,ids[1],date) if ids else None
                ap=0.6*afip+0.4*ad if afip else ad; hp=0.6*hfip+0.4*hd if hfip else hd
                proj_home=((ho+ap)/2)*pf+0.15; proj_away=((ao+hp)/2)*pf-0.15
                proj_margin=proj_home-proj_away
                our_p_home=normal_cdf(0, -proj_margin, MARGIN_SIGMA)  # P(home margin>0)
                our_p_dog = our_p_home if dog_side=="home" else (1-our_p_home)
                # de-vig market implied for the dog
                imp_home=american_to_prob(home_ml); imp_away=american_to_prob(away_ml)
                vig=imp_home+imp_away
                mkt_p_dog=(imp_home/vig) if dog_side=="home" else (imp_away/vig)
                edge=our_p_dog-mkt_p_dog
                if edge>=0.03:   # we think the dog is ≥3pts more likely than market
                    eb="3-6%" if edge<=0.06 else "6-10%" if edge<=0.10 else "10%+"
                    bk=edge_buckets[eb]; bk["n"]+=1
                    if dog_won: bk["wins"]+=1; bk["units"]+=american_profit(dog_ml)
                    else: bk["units"]-=1.0

            rs[home].append(hs); ra[home].append(as_); rs[away].append(as_); ra[away].append(hs)

    print(f"Underdog ML analysis · {len(ml_by_gid)} games with moneylines\n")
    print("═══ A. Favorite-longshot bias: blindly back every underdog ═══")
    print(f"  {'dog price':<12} {'n':>4} {'win%':>7} {'ROI':>8}")
    tot_n=tot_u=0
    for b in ["+100-120","+120-150","+150-200","+200+"]:
        bk=dog_buckets.get(b)
        if bk and bk["n"]>=15:
            roi=100*bk["units"]/bk["n"]
            print(f"  {b:<12} {bk['n']:>4} {100*bk['wins']/bk['n']:>6.1f}% {roi:>+7.1f}%")
            tot_n+=bk["n"]; tot_u+=bk["units"]
    if tot_n: print(f"  {'ALL dogs':<12} {tot_n:>4} {'':>7} {100*tot_u/tot_n:>+7.1f}%")

    print("\n═══ B. Model-edge dogs: bet only when OUR P(dog) > market ═══")
    print(f"  {'edge':<8} {'n':>4} {'win%':>7} {'ROI':>8}")
    eb_n=eb_u=0
    for b in ["3-6%","6-10%","10%+"]:
        bk=edge_buckets.get(b)
        if bk and bk["n"]>=10:
            roi=100*bk["units"]/bk["n"]
            print(f"  {b:<8} {bk['n']:>4} {100*bk['wins']/bk['n']:>6.1f}% {roi:>+7.1f}%")
            eb_n+=bk["n"]; eb_u+=bk["units"]
    if eb_n: print(f"  {'ALL':<8} {eb_n:>4} {'':>7} {100*eb_u/eb_n:>+7.1f}%")

    print("\nKey: positive ROI in (A) = the market itself underprices dogs")
    print("(favorite-longshot bias) — exploitable without any model. Positive")
    print("ROI in (B) beating (A) = our model ADDS value selecting which dogs.")


if __name__ == "__main__":
    main()
