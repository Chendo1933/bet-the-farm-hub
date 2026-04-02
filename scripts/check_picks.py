#!/usr/bin/env python3
"""
Bet The Farm Hub — Pick anomaly detector

Scans today's logged picks for suspicious scoring patterns that suggest
something went wrong with the hub's confidence calculation. Designed to
catch problems like the "MLB O/U all-99" bug before you notice it manually.

Usage:
    cd "Bet The Farm"
    python scripts/check_picks.py               # checks today
    python scripts/check_picks.py 2026-04-01    # checks a specific date

Exit codes:
    0 — no anomalies detected
    1 — anomalies found (check stdout for details)
    2 — picks file not found for the given date

Rules checked:
    1. Suspiciously high cluster — if ≥60% of picks for any (sport, betType)
       combo all score ≥ 95, flag it. Healthy picks should have spread.
    2. Score-99 count — any individual score100=99 (the cap) is flagged.
       A handful of 99s is possible; every O/U pick being 99 is a bug.
    3. All same tier — if all picks for a sport are the same tier, flag it.
       Real games have variance; uniform tiers suggest the signal is stuck.
    4. Zero picks — if log ran but generated 0 Elite/Strong picks, note it
       (informational, not necessarily a bug, but worth knowing).
    5. Picks with no live matchup — atsPick=null or away="" means the pick
       was an ATS-trend pick with no game context. These are filtered out
       by log_picks.py now, but flag them if any slipped through.
"""

import json
import os
import sys
from collections import defaultdict
from datetime import datetime, timezone

PICKS_DIR = "data/picks"

# Thresholds — tune these if you get too many false positives
HIGH_SCORE_THRESHOLD  = 95   # score100 value considered suspiciously high
HIGH_SCORE_CLUSTER_PCT = 0.60 # fraction of a group that must be high to flag
MIN_CLUSTER_SIZE       = 3   # minimum group size before the cluster rule fires


def load_picks(date_key: str) -> dict | None:
    path = os.path.join(PICKS_DIR, f"{date_key}.json")
    if not os.path.exists(path):
        return None
    try:
        with open(path) as f:
            return json.load(f)
    except (json.JSONDecodeError, IOError) as e:
        print(f"❌ Could not load {path}: {e}")
        return None


def check_picks(date_key: str) -> list[dict]:
    """
    Returns a list of anomaly dicts:
        { "severity": "warn"|"error", "rule": str, "detail": str }
    Empty list = all clear.
    """
    data = load_picks(date_key)
    if data is None:
        return [{"severity": "error", "rule": "file_missing",
                 "detail": f"No picks file for {date_key}"}]

    picks = data.get("picks", [])
    all_picks_count = data.get("all_picks_count", len(picks))
    has_live_odds   = data.get("has_live_odds", False)
    anomalies = []

    # ── Rule 0: No picks at all ────────────────────────────────────────────
    if all_picks_count == 0:
        anomalies.append({
            "severity": "warn",
            "rule": "zero_picks",
            "detail": f"Hub generated 0 picks total. "
                      f"{'Live odds were loaded.' if has_live_odds else 'No live odds (API key issue?).'}"
        })
    elif len(picks) == 0:
        anomalies.append({
            "severity": "warn",
            "rule": "zero_tracked",
            "detail": f"Hub generated {all_picks_count} picks but 0 Elite/Strong with game context. "
                      f"Possibly all picks were ATS-trend only (no live matchup)."
        })

    if not picks:
        return anomalies

    # ── Rule 1: Picks with no live matchup (should be filtered, but double-check) ──
    no_matchup = [p for p in picks if not p.get("away", "").strip() or p.get("atsPick") is None]
    if no_matchup:
        anomalies.append({
            "severity": "warn",
            "rule": "no_matchup_picks",
            "detail": f"{len(no_matchup)} pick(s) slipped through with no live matchup "
                      f"(atsPick=null or empty away): "
                      + ", ".join(p.get("pickLabel","?") for p in no_matchup[:3])
        })

    # ── Rule 2: Individual score100=99 (capped) ────────────────────────────
    capped = [p for p in picks if p.get("score100") == 99]
    if capped:
        labels = [f"{p['sport']} {p.get('pickLabel','?')} ({p.get('betType','?')})"
                  for p in capped]
        severity = "error" if len(capped) >= 3 else "warn"
        anomalies.append({
            "severity": severity,
            "rule": "score_capped_99",
            "detail": f"{len(capped)} pick(s) hit score100=99 (the maximum cap). "
                      f"{'Many 99s suggest the extremizer is saturating — check scoring logic.' if len(capped) >= 3 else 'A few 99s are plausible.'} "
                      f"Affected: {'; '.join(labels[:5])}"
        })

    # ── Rule 3: Suspicious cluster (≥60% of a sport+betType group all ≥ 95) ──
    groups: dict[tuple, list] = defaultdict(list)
    for p in picks:
        key = (p.get("sport","?"), p.get("betType","?"))
        groups[key].append(p)

    for (sport, bet_type), group in groups.items():
        if len(group) < MIN_CLUSTER_SIZE:
            continue
        high = [p for p in group if p.get("score100", 0) >= HIGH_SCORE_THRESHOLD]
        ratio = len(high) / len(group)
        if ratio >= HIGH_SCORE_CLUSTER_PCT:
            scores = sorted([p.get("score100",0) for p in group], reverse=True)
            anomalies.append({
                "severity": "error",
                "rule": "high_score_cluster",
                "detail": (
                    f"{sport.upper()} {bet_type}: {len(high)}/{len(group)} picks "
                    f"scored ≥{HIGH_SCORE_THRESHOLD} ({ratio*100:.0f}%). "
                    f"This pattern (all {sport} {bet_type} picks near 99) is the "
                    f"'MLB O/U all-99' bug signature. "
                    f"Scores: {scores}"
                )
            })

    # ── Rule 4: All picks for a sport land in the same tier ───────────────
    sport_tiers: dict[str, list] = defaultdict(list)
    for p in picks:
        sport_tiers[p.get("sport","?")].append(p.get("tier","?"))

    for sport, tiers in sport_tiers.items():
        if len(tiers) >= 4:
            unique_tiers = set(tiers)
            if len(unique_tiers) == 1:
                anomalies.append({
                    "severity": "warn",
                    "rule": "uniform_tiers",
                    "detail": (
                        f"{sport.upper()}: all {len(tiers)} picks are tier "
                        f"'{tiers[0]}'. Real picks should have variance across tiers."
                    )
                })

    return anomalies


def main():
    # Accept optional date argument, otherwise use today
    if len(sys.argv) > 1:
        date_key = sys.argv[1]
    else:
        date_key = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    print(f"\n[check_picks] Scanning picks for {date_key}")
    print("─" * 56)

    # First check if file exists
    path = os.path.join(PICKS_DIR, f"{date_key}.json")
    if not os.path.exists(path):
        print(f"  ⚠  No picks file found at {path}")
        print("  Run log_picks.py first, or provide a valid date.\n")
        sys.exit(2)

    # Load and print summary
    data = load_picks(date_key)
    if data:
        all_count = data.get("all_picks_count", "?")
        tracked   = len(data.get("picks", []))
        has_odds  = data.get("has_live_odds", False)
        print(f"  Picks file: {all_count} total generated · {tracked} Elite/Strong tracked")
        print(f"  Live odds: {'✓' if has_odds else '✗ (no Odds API key)'}")
        print()

        # Show all picks
        for p in data.get("picks", []):
            score = p.get("score100", "?")
            flag  = " ⚠" if score == 99 else ""
            print(f"  [{p.get('tier','?').upper():6}] {p.get('sport','?').upper():3} "
                  f"{p.get('betType','?'):6} | {str(score):>3}% | "
                  f"{p.get('pickLabel','?')}{flag}")
        print()

    anomalies = check_picks(date_key)

    if not anomalies:
        print("  ✅ No anomalies detected. Picks look healthy.\n")
        sys.exit(0)

    errors   = [a for a in anomalies if a["severity"] == "error"]
    warnings = [a for a in anomalies if a["severity"] == "warn"]

    if errors:
        print(f"  🚨 {len(errors)} ERROR(S) — scoring logic may be broken:\n")
        for a in errors:
            print(f"  ❌ [{a['rule']}]")
            print(f"     {a['detail']}\n")

    if warnings:
        print(f"  ⚠  {len(warnings)} WARNING(S) — worth investigating:\n")
        for a in warnings:
            print(f"  ⚠  [{a['rule']}]")
            print(f"     {a['detail']}\n")

    print("─" * 56)
    if errors:
        print(f"  Found {len(errors)} error(s). Investigate before trusting these picks.\n")
        sys.exit(1)
    else:
        print(f"  Found {len(warnings)} warning(s) only. Picks may still be usable.\n")
        sys.exit(0)


if __name__ == "__main__":
    main()
