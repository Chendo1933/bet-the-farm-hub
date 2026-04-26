#!/usr/bin/env python3
"""
Bet The Farm Hub — Pick file comparison

Compares two picks files for the same date (typically primary at 11:50 AM ET
vs backup-confirm at 12:30 PM ET) and prints a diff summary. Used by the
log-picks-verify.yml workflow to surface drift between the locked slate and
the late-morning re-check.

Why this matters:
  - Detects line movement that flipped picks (Over → Under, fav → dog)
  - Detects lineup news that re-scored picks
  - Detects total slate count changes (cancellations, additions)
  - Surfaces large score drift on individual picks
  - If primary file is missing/corrupt, this run still produces a slate

Exits 0 on success regardless of drift; intent is informational, not
blocking. Drift signal is for human review (and future Slack alert).

Usage:
  python scripts/compare_picks_files.py PRIMARY_PATH BACKUP_PATH

Example:
  python scripts/compare_picks_files.py data/picks/2026-04-26.json \
                                        data/picks/2026-04-26-confirm.json
"""

import json
import os
import sys


def load_picks(path: str):
    if not os.path.exists(path):
        return None
    try:
        with open(path) as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return None


def pick_key(p: dict) -> tuple:
    """Stable key for matching a pick across two files."""
    return (
        p.get("sport", ""),
        p.get("home", ""),
        p.get("away", ""),
        p.get("betType", ""),
        p.get("pickedTeam") or p.get("atsPick") or "",
    )


def main():
    if len(sys.argv) < 3:
        print("usage: compare_picks_files.py PRIMARY BACKUP")
        sys.exit(2)
    primary_path, backup_path = sys.argv[1], sys.argv[2]
    primary = load_picks(primary_path)
    backup  = load_picks(backup_path)

    print("=" * 72)
    print(f"PRIMARY: {primary_path}")
    print(f"BACKUP : {backup_path}")
    print("=" * 72)

    if primary is None and backup is None:
        print("⚠  Neither file readable — pipeline may be down")
        sys.exit(0)
    if primary is None:
        print("⚠  Primary missing — backup will serve as the slate when grading runs")
        sys.exit(0)
    if backup is None:
        print("⚠  Backup missing — primary stands as-is")
        sys.exit(0)

    p_picks = {pick_key(p): p for p in primary.get("picks", [])}
    b_picks = {pick_key(p): p for p in backup.get("picks", [])}

    print(f"\nPrimary: {len(p_picks)} picks · logged {primary.get('logged','?')}")
    print(f"Backup : {len(b_picks)} picks · logged {backup.get('logged','?')}")

    only_primary = set(p_picks) - set(b_picks)
    only_backup  = set(b_picks) - set(p_picks)
    in_both      = set(p_picks) & set(b_picks)

    if only_backup:
        print(f"\n📥 Picks added in backup ({len(only_backup)}):")
        for k in sorted(only_backup):
            p = b_picks[k]
            print(f"   + [{p.get('tier','?'):6}] {p.get('sport','?'):>3} {p.get('pickLabel','?')}"
                  f" ({p.get('away','?')} @ {p.get('home','?')})  score={p.get('score100','?')}")

    if only_primary:
        print(f"\n📤 Picks dropped from primary ({len(only_primary)}):")
        for k in sorted(only_primary):
            p = p_picks[k]
            print(f"   - [{p.get('tier','?'):6}] {p.get('sport','?'):>3} {p.get('pickLabel','?')}"
                  f" ({p.get('away','?')} @ {p.get('home','?')})  score={p.get('score100','?')}")

    # Score drift on shared picks
    drift_pairs = []
    tier_changes = []
    for k in in_both:
        ps, bs = p_picks[k].get("score100"), b_picks[k].get("score100")
        if ps is not None and bs is not None and ps != bs:
            drift_pairs.append((k, ps, bs))
        pt, bt = p_picks[k].get("tier"), b_picks[k].get("tier")
        if pt and bt and pt != bt:
            tier_changes.append((k, pt, bt))

    if drift_pairs:
        drift_pairs.sort(key=lambda x: -abs(x[1] - x[2]))
        big = [d for d in drift_pairs if abs(d[1] - d[2]) >= 3]
        if big:
            print(f"\n📊 Significant score drift (≥3pt) on {len(big)} pick(s):")
            for k, ps, bs in big[:10]:
                p = p_picks[k]
                arrow = "↑" if bs > ps else "↓"
                print(f"   {arrow} {p.get('sport','?'):>3} {p.get('pickLabel','?'):<35}"
                      f"  primary={ps}  backup={bs}  Δ{bs-ps:+d}")

    if tier_changes:
        print(f"\n🔄 Tier changes ({len(tier_changes)}):")
        for k, pt, bt in tier_changes[:10]:
            p = p_picks[k]
            print(f"   {pt} → {bt}  · {p.get('sport','?'):>3} {p.get('pickLabel','?')}"
                  f" ({p.get('away','?')} @ {p.get('home','?')})")

    # Tier count summary
    def tiers(picks_dict):
        out = {"elite": 0, "strong": 0, "good": 0, "lean": 0}
        for p in picks_dict.values():
            t = p.get("tier", "lean")
            if t in out: out[t] += 1
        return out
    pt_counts = tiers(p_picks)
    bt_counts = tiers(b_picks)
    if pt_counts != bt_counts:
        print(f"\n📈 Tier-count drift:")
        for t in ("elite", "strong", "good", "lean"):
            if pt_counts[t] != bt_counts[t]:
                print(f"   {t.upper():>6}: primary {pt_counts[t]} → backup {bt_counts[t]}"
                      f"  ({bt_counts[t] - pt_counts[t]:+d})")
    else:
        print(f"\n✓ Tier counts unchanged: {dict(pt_counts)}")

    drift_pct = 0
    if p_picks:
        drift_pct = len(only_primary | only_backup) / len(p_picks) * 100
    print(f"\nSlate drift: {drift_pct:.1f}% of primary picks differ in backup")
    if drift_pct >= 25:
        print(f"⚠  Drift ≥25% — significant change since primary log; review recommended")

    sys.exit(0)


if __name__ == "__main__":
    main()
