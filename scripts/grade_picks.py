#!/usr/bin/env python3
"""
Bet The Farm Hub — Nightly pick grader
Reads yesterday's logged picks (data/picks/YYYY-MM-DD.json) and
yesterday's game results (data/results/YYYY-MM-DD.json), determines
whether each pick covered, and updates data/performance.json with
running hit rates by confidence tier.

Run after log_results.py (~03:30 UTC) via grade-picks.yml workflow.

Grading logic:
  Spread: margin = home_score - away_score + spread
    margin > 0  → home covers
    margin < 0  → away covers
    margin == 0 → push
  ML: winner = team with higher score
  O/U: skipped (we don't log the total line)
"""

import json
import os
import sys
from datetime import datetime, timezone, timedelta

PICKS_DIR   = "data/picks"
RESULTS_DIR = "data/results"
PERF_FILE   = "data/performance.json"


def load_json(path: str) -> dict | None:
    try:
        with open(path) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def normalize(name: str) -> str:
    """Lower-case, strip common suffixes for fuzzy team matching."""
    return name.lower().strip()


def find_result(pick: dict, results_by_sport: dict) -> dict | None:
    """Find the game result matching a pick. Returns result dict or None."""
    sport = pick.get("sport", "").lower()
    home  = normalize(pick.get("home", ""))
    away  = normalize(pick.get("away", ""))

    for game in results_by_sport.get(sport, []):
        gh = normalize(game.get("home", ""))
        ga = normalize(game.get("away", ""))
        # Match if both team names appear (substring ok for mascot variations)
        if (home in gh or gh in home) and (away in ga or ga in away):
            return game
    return None


def grade_spread(pick: dict, result: dict) -> str:
    """
    Returns 'win', 'loss', or 'push'.
    spread = home team's line (negative = home favored).
    margin = home_score - away_score
    """
    spread    = pick.get("spread")
    ats_pick  = pick.get("atsPick")   # 'home' or 'away'
    home_sc   = result.get("home_score", 0)
    away_sc   = result.get("away_score", 0)

    if spread is None or ats_pick is None:
        return "ungraded"

    margin = (home_sc - away_sc) + spread   # > 0 means home covered

    if abs(margin) < 0.01:                  # push
        return "push"
    if ats_pick == "home":
        return "win" if margin > 0 else "loss"
    else:   # away
        return "win" if margin < 0 else "loss"


def grade_ml(pick: dict, result: dict) -> str:
    """For moneyline picks: did the picked team win outright?"""
    ats_pick = pick.get("atsPick")
    home_sc  = result.get("home_score", 0)
    away_sc  = result.get("away_score", 0)
    if home_sc == away_sc:
        return "push"
    if ats_pick == "home":
        return "win" if home_sc > away_sc else "loss"
    else:
        return "win" if away_sc > home_sc else "loss"


def load_performance() -> dict:
    existing = load_json(PERF_FILE)
    if existing and "records" in existing:
        return existing
    return {
        "updated": "",
        "records": {
            tier: {"wins": 0, "losses": 0, "pushes": 0}
            for tier in ("elite", "strong", "good", "lean")
        },
        "by_sport": {},
        "graded_dates": [],
    }


def main():
    now_utc  = datetime.now(timezone.utc)
    target   = now_utc - timedelta(days=1)
    date_key = target.strftime("%Y-%m-%d")

    print(f"[grade_picks] Grading picks for {date_key}")

    picks_path   = os.path.join(PICKS_DIR,   f"{date_key}.json")
    results_path = os.path.join(RESULTS_DIR, f"{date_key}.json")

    picks_data   = load_json(picks_path)
    results_data = load_json(results_path)

    if not picks_data:
        print(f"  · No picks file found for {date_key} — skipping")
        sys.exit(0)
    if not results_data:
        print(f"  · No results file found for {date_key} — skipping")
        sys.exit(0)

    picks   = picks_data.get("picks", [])
    sports  = results_data.get("sports", {})

    if not picks:
        print(f"  · No Elite/Strong picks were logged for {date_key}")
        sys.exit(0)

    perf = load_performance()

    # Skip if already graded
    if date_key in perf.get("graded_dates", []):
        print(f"  · {date_key} already graded — skipping")
        sys.exit(0)

    graded = 0
    for pick in picks:
        tier      = pick.get("tier", "lean")
        bet_type  = pick.get("betType", "spread")
        result    = find_result(pick, sports)

        if not result:
            print(f"  ? {pick['sport']} {pick['pickLabel']} — no matching result found")
            continue

        outcome = (grade_ml(pick, result)     if bet_type == "ml"
                   else grade_spread(pick, result) if bet_type == "spread"
                   else "ungraded")

        if outcome == "ungraded":
            print(f"  ? {pick['sport']} {pick['pickLabel']} — could not grade ({bet_type})")
            continue

        rec = perf["records"].setdefault(
            tier, {"wins": 0, "losses": 0, "pushes": 0}
        )
        if outcome == "win":
            rec["wins"]   += 1
        elif outcome == "loss":
            rec["losses"] += 1
        else:
            rec["pushes"] += 1

        # Track by sport too
        sport_key = pick["sport"].lower()
        sp_tiers  = perf["by_sport"].setdefault(sport_key, {})
        sp_rec    = sp_tiers.setdefault(tier, {"wins": 0, "losses": 0, "pushes": 0})
        if outcome == "win":
            sp_rec["wins"]   += 1
        elif outcome == "loss":
            sp_rec["losses"] += 1
        else:
            sp_rec["pushes"] += 1

        icon = "✅" if outcome == "win" else "❌" if outcome == "loss" else "🔁"
        print(f"  {icon} [{tier.upper():6}] {pick['sport']} {pick['pickLabel']} "
              f"({result['home']} {result['home_score']} – {result['away']} {result['away_score']}) "
              f"→ {outcome.upper()}")
        graded += 1

    if graded == 0:
        print(f"  ⚠  No picks could be graded for {date_key}")
        sys.exit(0)

    perf["updated"] = date_key
    perf.setdefault("graded_dates", []).append(date_key)
    perf["graded_dates"].sort(reverse=True)

    # Print summary
    print(f"\n── Performance Summary ─────────────────────────────────────────")
    for tier in ("elite", "strong", "good"):
        rec = perf["records"].get(tier, {"wins": 0, "losses": 0, "pushes": 0})
        total = rec["wins"] + rec["losses"]
        pct   = f"{rec['wins']/total*100:.1f}%" if total > 0 else "—"
        print(f"  {tier.upper():8} {rec['wins']}-{rec['losses']}"
              + (f" ({rec['pushes']}P)" if rec["pushes"] else "")
              + f"  →  {pct}")

    os.makedirs(os.path.dirname(PERF_FILE), exist_ok=True)
    with open(PERF_FILE, "w") as f:
        json.dump(perf, f, indent=2)

    print(f"\n✅ {graded} pick(s) graded → {PERF_FILE}")
    sys.exit(0)


if __name__ == "__main__":
    main()
