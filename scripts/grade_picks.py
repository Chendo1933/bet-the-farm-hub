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

performance.json schema (matches hub performance panel):
  {
    "last_updated": "YYYY-MM-DD",
    "tiers": {
      "elite":  { "w": 0, "l": 0, "p": 0 },
      "strong": { "w": 0, "l": 0, "p": 0 }
    },
    "by_sport": { "nba": { "elite": { "w": 0, "l": 0, "p": 0 } } },
    "graded_dates": ["2026-03-22", ...]
  }
"""

import json
import os
import sys
from datetime import datetime, timezone, timedelta

# Schema validator — catches key-name mismatches before they hit disk
try:
    from schemas import validate as schema_validate, SchemaError
except ImportError:
    # Running outside scripts/ directory — define a no-op so the script still works
    def schema_validate(name, data, **kw): return []
    class SchemaError(Exception): pass

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

    if not home or not away:
        return None   # ungradeable pick with no game context

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


CONF_BANDS = ["95-99", "90-94", "85-89", "80-84", "75-79", "70-74"]

def conf_band(score100) -> str | None:
    """Map a score100 value to a 5-point confidence band label, or None if out of range."""
    if score100 is None:
        return None
    s = int(score100)
    if s >= 95: return "95-99"
    if s >= 90: return "90-94"
    if s >= 85: return "85-89"
    if s >= 80: return "80-84"
    if s >= 75: return "75-79"
    if s >= 70: return "70-74"
    return None


def load_performance() -> dict:
    """
    Load existing performance.json, or return a fresh default.
    Uses the hub-compatible schema: tiers / w / l / p / last_updated.
    Also maintains by_conf for confidence-band hit-rate breakdown.
    """
    existing = load_json(PERF_FILE)
    if existing and "tiers" in existing:
        for tier in ("elite", "strong"):
            existing["tiers"].setdefault(tier, {"w": 0, "l": 0, "p": 0})
        existing.setdefault("by_sport", {})
        existing.setdefault("graded_dates", [])
        # Backfill by_conf for files written before this field existed
        existing.setdefault("by_conf", {band: {"w": 0, "l": 0, "p": 0} for band in CONF_BANDS})
        for band in CONF_BANDS:
            existing["by_conf"].setdefault(band, {"w": 0, "l": 0, "p": 0})
        return existing
    return {
        "last_updated": "",
        "tiers": {
            "elite":  {"w": 0, "l": 0, "p": 0},
            "strong": {"w": 0, "l": 0, "p": 0},
        },
        "by_sport": {},
        "by_conf":  {band: {"w": 0, "l": 0, "p": 0} for band in CONF_BANDS},
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
        tier      = pick.get("tier", "lean").lower()
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

        # Track by tier
        if tier in ("elite", "strong"):
            rec = perf["tiers"][tier]
            if outcome == "win":   rec["w"] += 1
            elif outcome == "loss": rec["l"] += 1
            else:                  rec["p"] += 1

        # Track by sport
        sport_key = pick["sport"].lower()
        sp_tiers  = perf["by_sport"].setdefault(sport_key, {})
        sp_rec    = sp_tiers.setdefault(tier, {"w": 0, "l": 0, "p": 0})
        if outcome == "win":   sp_rec["w"] += 1
        elif outcome == "loss": sp_rec["l"] += 1
        else:                  sp_rec["p"] += 1

        # Track by confidence band — the key question: do 95s hit more than 87s?
        band = conf_band(pick.get("score100"))
        if band:
            br = perf["by_conf"].setdefault(band, {"w": 0, "l": 0, "p": 0})
            if outcome == "win":   br["w"] += 1
            elif outcome == "loss": br["l"] += 1
            else:                  br["p"] += 1

        icon = "✅" if outcome == "win" else "❌" if outcome == "loss" else "🔁"
        band_str = f" [{band}]" if band else ""
        print(f"  {icon} [{tier.upper():6}]{band_str} {pick['sport']} {pick['pickLabel']} "
              f"({result['home']} {result['home_score']} – {result['away']} {result['away_score']}) "
              f"→ {outcome.upper()}")
        graded += 1

    if graded == 0:
        print(f"  ⚠  No picks could be graded for {date_key}")
        sys.exit(0)

    perf["last_updated"] = date_key
    perf.setdefault("graded_dates", []).append(date_key)
    perf["graded_dates"].sort(reverse=True)

    # Print summary
    print(f"\n── Performance by Tier ──────────────────────────────────────────")
    for tier in ("elite", "strong"):
        rec   = perf["tiers"].get(tier, {"w": 0, "l": 0, "p": 0})
        total = rec["w"] + rec["l"]
        pct   = f"{rec['w']/total*100:.1f}%" if total > 0 else "—"
        push_str = f" ({rec['p']}P)" if rec["p"] else ""
        print(f"  {tier.upper():8} {rec['w']}-{rec['l']}{push_str}  →  {pct}")

    print(f"\n── Performance by Confidence Band ───────────────────────────────")
    for band in CONF_BANDS:
        rec   = perf["by_conf"].get(band, {"w": 0, "l": 0, "p": 0})
        total = rec["w"] + rec["l"]
        if total == 0:
            continue
        pct      = f"{rec['w']/total*100:.1f}%"
        push_str = f" ({rec['p']}P)" if rec["p"] else ""
        bar      = "█" * rec["w"] + "░" * rec["l"]
        print(f"  {band}  {rec['w']}-{rec['l']}{push_str}  →  {pct}  {bar}")

    # Validate schema before writing — crash loudly rather than silently corrupt
    try:
        schema_validate("performance", perf)
    except SchemaError as e:
        print(f"\n🚨 SCHEMA ERROR — refusing to write malformed data:\n{e}")
        sys.exit(1)

    os.makedirs(os.path.dirname(PERF_FILE), exist_ok=True)
    with open(PERF_FILE, "w") as f:
        json.dump(perf, f, indent=2)

    print(f"\n✅ {graded} pick(s) graded → {PERF_FILE}")
    sys.exit(0)


if __name__ == "__main__":
    main()
