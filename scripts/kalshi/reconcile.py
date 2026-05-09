#!/usr/bin/env python3
"""
Kalshi Phase 2 — nightly reconciliation of dry-run orders against actual game results.

For each historical dry-run file, look up the matching pick in pick_history.json,
compute the theoretical PnL (would-have-paid-out minus stake), and accumulate
into data/kalshi_dryrun_perf.json. Also writes per-day reconciled detail back
into the dry-run file so each row has its outcome attached.

Math (Kalshi binary contract):
  Buy YES at cents `a` → contract pays $1.00 if YES resolves true, $0 otherwise.
  Stake S = contracts × (a/100)
  If pick wins (YES resolves true): payout = contracts × $1.00, profit = payout - S
  If pick loses:                    payout = $0,                profit = -S
  Push (rare in MLB ML, won't happen for ML, possible elsewhere) is treated as void → 0 PnL.

Run:
  python3 scripts/kalshi/reconcile.py            # reconcile every dry-run file we have
  python3 scripts/kalshi/reconcile.py --date 2026-05-09  # one specific day

Output: data/kalshi_dryrun_perf.json with running theoretical PnL summary.
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path

DRYRUN_DIR = Path("data/kalshi_dryrun")
HISTORY    = Path("data/pick_history.json")
PERF_OUT   = Path("data/kalshi_dryrun_perf.json")


def _pick_key(p: dict) -> tuple:
    """Stable identity for matching dry-run order back to a pick_history entry."""
    return (
        p.get("date") or p.get("game_date") or "",
        p.get("sport", ""),
        p.get("home", ""),
        p.get("away", ""),
        p.get("betType", ""),
        p.get("pickLabel") or p.get("pick") or "",
    )


def _build_history_index() -> dict:
    """Map pick-key → outcome dict from data/pick_history.json."""
    if not HISTORY.exists():
        return {}
    entries = json.loads(HISTORY.read_text()).get("picks", [])
    out = {}
    for h in entries:
        # pick_history schema differs slightly from picks-file schema
        # (pickLabel becomes 'pick'). Match on either.
        key = (
            h.get("date", ""),
            h.get("sport", ""),
            h.get("home", ""),
            h.get("away", ""),
            h.get("betType", ""),
            h.get("pick", ""),
        )
        out[key] = h
    return out


def _reconcile_order(order: dict, hist_idx: dict) -> dict:
    """Annotate one dry-run order with its outcome + theoretical PnL."""
    # Build the date+matchup key for lookup. Picks-file 'date' is "Apr 17"
    # style; pick_history uses ISO date. We need the file-level date.
    # Caller passes that in via the order's enclosing day file (handled below).
    pick = order.get("pick", {})
    file_date = order.get("_file_date", "")
    key = (
        file_date,
        pick.get("sport", ""),
        pick.get("home", ""),
        pick.get("away", ""),
        pick.get("betType", ""),
        pick.get("pickLabel", ""),
    )
    hist = hist_idx.get(key)
    annotated = dict(order)

    if not hist:
        annotated["outcome"] = None
        annotated["pnl_dollars"] = 0.0
        annotated["payout_dollars"] = 0.0
        annotated["reconcile_status"] = "no_history_match"
        return annotated

    outcome = hist.get("outcome")
    annotated["outcome"] = outcome
    annotated["home_score"] = hist.get("homeScore")
    annotated["away_score"] = hist.get("awayScore")

    if not order.get("would_place"):
        annotated["pnl_dollars"] = 0.0
        annotated["payout_dollars"] = 0.0
        annotated["reconcile_status"] = "skipped_dry_run"
        return annotated

    contracts = order.get("contracts") or 0
    stake = order.get("stake_dollars") or 0.0

    if outcome == "win":
        payout = contracts * 1.00
        pnl = round(payout - stake, 2)
    elif outcome == "loss":
        payout = 0.0
        pnl = round(-stake, 2)
    elif outcome == "push":
        payout = stake   # contract voided / refunded
        pnl = 0.0
    else:  # ungraded / unknown
        payout = 0.0
        pnl = 0.0

    annotated["payout_dollars"] = round(payout, 2)
    annotated["pnl_dollars"] = pnl
    annotated["reconcile_status"] = "graded" if outcome in ("win","loss","push") else "ungraded"
    return annotated


def _reconcile_one_file(path: Path, hist_idx: dict, write_back: bool = True) -> dict:
    """Process one dry-run file. Returns aggregate PnL stats for that day."""
    data = json.loads(path.read_text())
    file_date = data.get("date", path.stem)
    annotated_orders = []
    placed_count = 0
    wins = losses = pushes = ungraded = 0
    total_stake = total_payout = total_pnl = 0.0

    for order in data.get("orders", []):
        order["_file_date"] = file_date
        ann = _reconcile_order(order, hist_idx)
        ann.pop("_file_date", None)
        annotated_orders.append(ann)
        if ann.get("would_place"):
            placed_count += 1
            total_stake += ann.get("stake_dollars") or 0
            total_payout += ann.get("payout_dollars") or 0
            total_pnl += ann.get("pnl_dollars") or 0
            if ann.get("outcome") == "win":   wins += 1
            elif ann.get("outcome") == "loss": losses += 1
            elif ann.get("outcome") == "push": pushes += 1
            else: ungraded += 1

    daily = {
        "date": file_date,
        "placed": placed_count,
        "wins": wins, "losses": losses, "pushes": pushes, "ungraded": ungraded,
        "total_stake_dollars": round(total_stake, 2),
        "total_payout_dollars": round(total_payout, 2),
        "total_pnl_dollars": round(total_pnl, 2),
        "roi_pct": round((total_pnl / total_stake * 100) if total_stake else 0, 2),
    }

    if write_back:
        data["orders"] = annotated_orders
        data["reconcile"] = daily
        path.write_text(json.dumps(data, indent=2, default=str))

    return daily


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--date", help="ET date YYYY-MM-DD; reconcile only that file")
    args = ap.parse_args()

    if not DRYRUN_DIR.exists():
        sys.exit(f"No dry-run dir found at {DRYRUN_DIR} — run dry_run.py first")

    if args.date:
        files = [DRYRUN_DIR / f"{args.date}.json"]
        if not files[0].exists():
            sys.exit(f"No dry-run file for {args.date}")
    else:
        files = sorted(DRYRUN_DIR.glob("*.json"))
        files = [f for f in files if f.stem != "index"]

    hist_idx = _build_history_index()
    print(f"Reconciling {len(files)} dry-run file(s) against {len(hist_idx)} graded picks")

    daily_summaries = []
    for f in files:
        daily = _reconcile_one_file(f, hist_idx)
        daily_summaries.append(daily)
        n = daily["placed"]
        if n == 0:
            print(f"  {daily['date']}: no orders placed")
        else:
            graded_n = daily["wins"] + daily["losses"] + daily["pushes"]
            ungraded_str = f", {daily['ungraded']} ungraded" if daily["ungraded"] else ""
            print(f"  {daily['date']}: {daily['wins']}-{daily['losses']}{('-'+str(daily['pushes'])) if daily['pushes'] else ''} "
                  f"of {graded_n}{ungraded_str} placed · "
                  f"stake ${daily['total_stake_dollars']:.2f} · "
                  f"PnL ${daily['total_pnl_dollars']:+.2f} ({daily['roi_pct']:+.1f}%)")

    # Aggregate across all reconciled days
    total_stake = sum(d["total_stake_dollars"] for d in daily_summaries)
    total_pnl   = sum(d["total_pnl_dollars"]   for d in daily_summaries)
    total_placed = sum(d["placed"] for d in daily_summaries)
    total_wins  = sum(d["wins"] for d in daily_summaries)
    total_losses = sum(d["losses"] for d in daily_summaries)
    overall = {
        "days": len(daily_summaries),
        "total_orders_placed": total_placed,
        "wins": total_wins,
        "losses": total_losses,
        "total_stake_dollars": round(total_stake, 2),
        "total_pnl_dollars": round(total_pnl, 2),
        "roi_pct": round((total_pnl / total_stake * 100) if total_stake else 0, 2),
        "win_pct": round((total_wins / (total_wins + total_losses) * 100) if (total_wins + total_losses) else 0, 1),
        "daily": daily_summaries,
    }

    PERF_OUT.write_text(json.dumps(overall, indent=2, default=str))
    print(f"\n── Aggregate ────────────────────────────────────────────")
    if total_wins + total_losses:
        print(f"  Record: {total_wins}-{total_losses} ({overall['win_pct']:.1f}%) over {total_placed} placed orders")
        print(f"  Stake:  ${total_stake:.2f}    PnL: ${total_pnl:+.2f}    ROI: {overall['roi_pct']:+.1f}%")
    else:
        print(f"  No graded orders yet (run grading first or wait for tomorrow's reconcile)")
    print(f"\n✅ Wrote {PERF_OUT}")


if __name__ == "__main__":
    main()
