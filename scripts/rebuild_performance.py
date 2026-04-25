#!/usr/bin/env python3
"""
Bet The Farm Hub — One-shot full rebuild of performance.json + pick_history.json

Why this exists:
  Auditing on 2026-04-18 found 199 picks in pick_history.json but 257 picks counted
  in performance.json tier W/L counters. The gap traces to pick_history being
  truncated at some point (4/11's entries are missing entirely) while performance.json
  kept accumulating. ROI tracking was also added partway through, so only 39 of 199
  picks had units captured.

What it does:
  1. Backs up current performance.json + pick_history.json with .bak.YYYYMMDD-HHMMSS suffix
  2. Replays every data/picks/*.json against the matching data/results/*.json using
     the same grading logic as grade_picks.py
  3. Writes fresh, fully consistent performance.json + pick_history.json

After this, pick_history.json IS the audit trail and performance.json is purely
an aggregation derived from it. Single source of truth.

Run: python scripts/rebuild_performance.py [--dry-run]
"""

from __future__ import annotations   # defer type-hint eval (works on py 3.9)

import json
import os
import sys
import glob
import shutil
from datetime import datetime, timezone

# ── Inlined grading logic ──────────────────────────────────────────────────
# Originally we imported from grade_picks, but that module uses 3.10+ union
# type hints (`dict | None`) and this script must also run on Python 3.9 (the
# default macOS install). The logic below is a verbatim copy — keep it in sync
# with grade_picks.py if either changes.

CONF_BANDS = ["80+", "75-79", "71-74", "68-70", "65-67", "62-64", "59-61", "56-58", "50-55"]
ALL_TIERS  = ("elite", "strong", "good", "lean")
_BET_TYPES = ("spread", "ml", "ou")
_MARGINS_DEFAULT = {"total": 0.0, "count": 0, "brier_sum": 0.0, "brier_n": 0}
_ROI_DEFAULT     = {"units_won": 0.0, "roi_count": 0}


def _empty_wlp():
    return {"w": 0, "l": 0, "p": 0}


def _empty_bet_type_dict():
    return {bt: {**_empty_wlp(), **dict(_MARGINS_DEFAULT), **dict(_ROI_DEFAULT)}
            for bt in _BET_TYPES}


def normalize(name):
    return (name or "").lower().strip()


def find_result(pick, results_by_sport):
    sport = pick.get("sport", "").lower()
    home  = normalize(pick.get("home", ""))
    away  = normalize(pick.get("away", ""))
    if not home or not away:
        return None
    for game in results_by_sport.get(sport, []):
        gh = normalize(game.get("home", ""))
        ga = normalize(game.get("away", ""))
        if (home in gh or gh in home) and (away in ga or ga in away):
            return game
    return None


def grade_spread(pick, result):
    spread   = pick.get("spread")
    ats_pick = pick.get("atsPick")
    home_sc  = result.get("home_score", 0)
    away_sc  = result.get("away_score", 0)
    if spread is None or ats_pick is None:
        return ("ungraded", None)
    margin = (home_sc - away_sc) + spread
    if abs(margin) < 0.01:
        return ("push", 0.0)
    if ats_pick == "home":
        return ("win" if margin > 0 else "loss", abs(margin))
    return ("win" if margin < 0 else "loss", abs(margin))


def grade_ml(pick, result):
    ats_pick = pick.get("atsPick")
    home_sc  = result.get("home_score", 0)
    away_sc  = result.get("away_score", 0)
    pt_margin = abs(home_sc - away_sc)
    if home_sc == away_sc:
        return ("push", 0.0)
    if ats_pick == "home":
        return ("win" if home_sc > away_sc else "loss", pt_margin)
    return ("win" if away_sc > home_sc else "loss", pt_margin)


def grade_ou(pick, result):
    total  = pick.get("total")
    picked = (pick.get("pickedTeam") or "").lower()
    home_sc = result.get("home_score", 0)
    away_sc = result.get("away_score", 0)
    if total is None or picked not in ("over", "under"):
        return ("ungraded", None)
    actual_total = home_sc + away_sc
    margin = actual_total - total
    if abs(margin) < 0.01:
        return ("push", 0.0)
    if picked == "over":
        return ("win" if margin > 0 else "loss", abs(margin))
    return ("win" if margin < 0 else "loss", abs(margin))


def conf_band(score100):
    if score100 is None: return None
    s = int(score100)
    if s >= 80: return "80+"
    if s >= 75: return "75-79"
    if s >= 71: return "71-74"
    if s >= 68: return "68-70"
    if s >= 65: return "65-67"
    if s >= 62: return "62-64"
    if s >= 59: return "59-61"
    if s >= 56: return "56-58"
    if s >= 50: return "50-55"
    return None


def american_odds_to_units(odds, outcome):
    if outcome == "push":  return 0.0
    if outcome not in ("win", "loss"): return 0.0
    price = -110 if odds is None else float(odds)
    if outcome == "loss": return -1.0
    return price/100.0 if price > 0 else 100.0/abs(price)

PICKS_DIR    = "data/picks"
RESULTS_DIR  = "data/results"
PERF_FILE    = "data/performance.json"
HISTORY_FILE = "data/pick_history.json"


def fresh_perf() -> dict:
    """Build an empty performance.json structure with every accumulator initialized."""
    return {
        "last_updated": "",
        "tiers":             {t: _empty_wlp() for t in ALL_TIERS},
        "by_sport":          {},
        "by_conf":           {b: _empty_wlp() for b in CONF_BANDS},
        "by_bet_type":       _empty_bet_type_dict(),
        "by_sport_bet_type": {},
        "margins":           dict(_MARGINS_DEFAULT),
        "margins_by_tier":   {t: dict(_MARGINS_DEFAULT) for t in ALL_TIERS},
        "margins_by_conf":   {b: dict(_MARGINS_DEFAULT) for b in CONF_BANDS},
        "margins_by_sport":  {},
        "roi":               dict(_ROI_DEFAULT),
        "roi_by_tier":       {t: dict(_ROI_DEFAULT) for t in ALL_TIERS},
        "graded_dates":      [],
    }


def grade_one_pick(pick: dict, result: dict, date_key: str) -> tuple[dict | None, dict | None]:
    """
    Grade one pick. Returns (history_entry, accumulator_delta) or (None, None) if ungradeable.
    accumulator_delta is a dict of fields to apply to performance.json.
    """
    bet_type = pick.get("betType", "spread")
    if bet_type == "ml":
        outcome, margin = grade_ml(pick, result)
    elif bet_type == "spread":
        outcome, margin = grade_spread(pick, result)
    elif bet_type == "ou":
        outcome, margin = grade_ou(pick, result)
    else:
        return None, None

    if outcome == "ungraded":
        return None, None

    pick_odds = pick.get("odds")
    units = american_odds_to_units(pick_odds, outcome)
    has_odds_or_standard = (pick_odds is not None) or (bet_type in ("spread", "ou"))

    history_entry = {
        "date":      date_key,
        "sport":     pick.get("sport", ""),
        "betType":   bet_type,
        "tier":      pick.get("tier", "lean").lower(),
        "pick":      pick.get("pickLabel", ""),
        "home":      pick.get("home", ""),
        "away":      pick.get("away", ""),
        "spread":    pick.get("spread"),
        "total":     pick.get("total"),
        "score100":  pick.get("score100"),
        "odds":      pick_odds,
        "outcome":   outcome,
        "margin":    round(margin, 1) if margin is not None else None,
        "units":     round(units, 4) if has_odds_or_standard else None,
        "homeScore": result.get("home_score"),
        "awayScore": result.get("away_score"),
    }
    return history_entry, {
        "outcome": outcome,
        "margin":  margin,
        "units":   units,
        "has_roi": has_odds_or_standard,
        "tier":    pick.get("tier", "lean").lower(),
        "sport":   pick["sport"].lower(),
        "betType": bet_type,
        "score100":pick.get("score100"),
    }


def apply_delta(perf: dict, d: dict):
    """Apply one pick's grading delta to every accumulator in perf."""
    outcome  = d["outcome"]
    margin   = d["margin"]
    units    = d["units"]
    tier     = d["tier"]
    sport    = d["sport"]
    bt       = d["betType"]
    band     = conf_band(d["score100"])

    # Tier
    if tier in ALL_TIERS:
        rec = perf["tiers"][tier]
        rec["w" if outcome=="win" else "l" if outcome=="loss" else "p"] += 1

    # By sport (tier breakdown)
    sp_tiers = perf["by_sport"].setdefault(sport, {})
    sp_rec   = sp_tiers.setdefault(tier, _empty_wlp())
    sp_rec["w" if outcome=="win" else "l" if outcome=="loss" else "p"] += 1

    # By conf band
    if band:
        br = perf["by_conf"].setdefault(band, _empty_wlp())
        br["w" if outcome=="win" else "l" if outcome=="loss" else "p"] += 1

    # By bet type W/L/P
    bt_rec = perf["by_bet_type"].setdefault(bt, _empty_wlp())
    bt_rec["w" if outcome=="win" else "l" if outcome=="loss" else "p"] += 1

    # Sport × bet type crosstab
    sp_bt   = perf["by_sport_bet_type"].setdefault(sport, {})
    sbt_rec = sp_bt.setdefault(bt, {**_empty_wlp(), **dict(_MARGINS_DEFAULT), **dict(_ROI_DEFAULT)})
    sbt_rec["w" if outcome=="win" else "l" if outcome=="loss" else "p"] += 1

    # Margins + Brier (skip pushes — no signal)
    if outcome != "push":
        predicted = (d["score100"] or 50) / 100.0
        actual    = 1.0 if outcome == "win" else 0.0
        brier     = (predicted - actual) ** 2

        for bucket in (
            perf["margins"],
            perf["margins_by_tier"].setdefault(tier, dict(_MARGINS_DEFAULT)),
            perf["margins_by_conf"].setdefault(band, dict(_MARGINS_DEFAULT)) if band else None,
            perf["by_bet_type"][bt],
            perf["margins_by_sport"].setdefault(sport, dict(_MARGINS_DEFAULT)),
            sbt_rec,
        ):
            if bucket is None: continue
            # Only apply margin to records that track it (skip plain wlp dicts)
            if "total" in bucket and margin is not None:
                bucket["total"]     += margin
                bucket["count"]     += 1
            if "brier_sum" in bucket:
                bucket["brier_sum"] += brier
                bucket["brier_n"]   += 1

    # ROI tracking
    if d["has_roi"]:
        perf["roi"]["units_won"]                          += units
        perf["roi"]["roi_count"]                          += 1
        rt = perf["roi_by_tier"].setdefault(tier, dict(_ROI_DEFAULT))
        rt["units_won"]                                   += units
        rt["roi_count"]                                   += 1
        perf["by_bet_type"][bt]["units_won"]              += units
        perf["by_bet_type"][bt]["roi_count"]              += 1
        sbt_rec["units_won"]                              += units
        sbt_rec["roi_count"]                              += 1


def round_floats(d):
    """Walk an accumulator tree and round all float values to 4 decimals for clean JSON."""
    if isinstance(d, dict):
        return {k: round_floats(v) for k, v in d.items()}
    if isinstance(d, list):
        return [round_floats(x) for x in d]
    if isinstance(d, float):
        return round(d, 4)
    return d


def main():
    dry = "--dry-run" in sys.argv

    # ── 1. Backup ───────────────────────────────────────────────────────────
    ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    if not dry:
        for src in (PERF_FILE, HISTORY_FILE):
            if os.path.exists(src):
                bak = f"{src}.bak.{ts}"
                shutil.copy2(src, bak)
                print(f"  📦 backed up {src} → {bak}")

    # ── 2. Discover pick/result file pairs ──────────────────────────────────
    pick_files = sorted(glob.glob(os.path.join(PICKS_DIR, "*.json")))
    pick_files = [p for p in pick_files if not p.endswith("index.json")]

    pairs = []
    for pf in pick_files:
        date_key = os.path.basename(pf).replace(".json", "")
        rf = os.path.join(RESULTS_DIR, f"{date_key}.json")
        if os.path.exists(rf):
            pairs.append((date_key, pf, rf))
        else:
            print(f"  · {date_key}: no results file (skipping — likely future games)")

    print(f"\nFound {len(pairs)} (picks, results) pairs to grade")

    # ── 3. Replay grading ────────────────────────────────────────────────────
    perf    = fresh_perf()
    history = {"picks": []}

    totals = {"graded": 0, "unmatched": 0, "ungraded": 0, "by_date": {}}

    for date_key, pf, rf in pairs:
        picks_data   = json.load(open(pf))
        results_data = json.load(open(rf))
        picks   = picks_data.get("picks", [])
        sports  = results_data.get("sports", {})

        # Dedupe within the day. The hub had a bug (fixed in commit d3446b6 on 2026-04-19,
        # the TODAY_GAMES 36h→ET-today narrowing) that emitted the same pick twice for some
        # MLB games when the 36h window included a tomorrow-game with same matchup. Pre-fix
        # daily files (4/10–4/15) carry these dupes. Match by (sport, home, away, pickLabel,
        # atsPick) so we keep both sides of a real symmetric pick (e.g. ML vs Spread vs O/U
        # on the same game) but drop exact duplicates.
        seen = set()
        deduped = []
        for p in picks:
            key = (p.get("sport",""), p.get("home",""), p.get("away",""),
                   p.get("pickLabel",""), p.get("atsPick"), p.get("betType"))
            if key in seen:
                continue
            seen.add(key)
            deduped.append(p)
        if len(deduped) != len(picks):
            print(f"  · {date_key}: removed {len(picks)-len(deduped)} duplicate(s)")
        picks = deduped

        date_graded = 0
        for pick in picks:
            result = find_result(pick, sports)
            if not result:
                totals["unmatched"] += 1
                continue
            entry, delta = grade_one_pick(pick, result, date_key)
            if entry is None:
                totals["ungraded"] += 1
                continue
            history["picks"].append(entry)
            apply_delta(perf, delta)
            date_graded += 1
            totals["graded"] += 1

        if date_graded:
            perf["graded_dates"].append(date_key)
        totals["by_date"][date_key] = (date_graded, len(picks))

    perf["graded_dates"].sort(reverse=True)
    perf["last_updated"] = max(perf["graded_dates"]) if perf["graded_dates"] else ""

    # ── 4. Report ───────────────────────────────────────────────────────────
    print(f"\n── Grading replay summary ────────────────────────────────────")
    for d, (g, n) in sorted(totals["by_date"].items()):
        print(f"  {d}  graded {g}/{n}")
    print(f"\n  Total graded: {totals['graded']}")
    print(f"  Unmatched (no result for pick): {totals['unmatched']}")
    print(f"  Ungraded (couldn't grade outcome): {totals['ungraded']}")

    print(f"\n── Tier W/L (rebuilt) ────────────────────────────────────────")
    for t in ALL_TIERS:
        r = perf["tiers"][t]
        n = r["w"] + r["l"]
        if n == 0: continue
        print(f"  {t.upper():>6}  {r['w']}-{r['l']}-{r['p']}  ({r['w']/n*100:.1f}%)")

    print(f"\n── ROI (rebuilt) ─────────────────────────────────────────────")
    r = perf["roi"]
    if r["roi_count"]:
        print(f"  Overall: {r['units_won']:+.2f}u over {r['roi_count']} picks  →  ROI {r['units_won']/r['roi_count']*100:+.1f}%")
    for t in ALL_TIERS:
        rt = perf["roi_by_tier"][t]
        if rt["roi_count"] == 0: continue
        print(f"  {t.upper():>6}  {rt['units_won']:+.2f}u over {rt['roi_count']} picks  →  ROI {rt['units_won']/rt['roi_count']*100:+.1f}%")

    # ── 5. Schema-validate + write ──────────────────────────────────────────
    perf = round_floats(perf)
    history = round_floats(history)

    try:
        from schemas import validate, SchemaError
        validate("performance", perf)
    except (ImportError, SchemaError) as e:
        if isinstance(e, SchemaError):
            print(f"\n🚨 Schema error — refusing to write:\n{e}")
            sys.exit(1)

    if dry:
        print("\n[DRY RUN] no files written")
        return

    with open(HISTORY_FILE, "w") as f:
        json.dump(history, f, indent=2)
    with open(PERF_FILE, "w") as f:
        json.dump(perf, f, indent=2)
    print(f"\n✅ Wrote {HISTORY_FILE} ({len(history['picks'])} entries)")
    print(f"✅ Wrote {PERF_FILE}")


if __name__ == "__main__":
    main()
