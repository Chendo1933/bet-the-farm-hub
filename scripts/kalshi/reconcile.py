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

DRYRUN_DIR  = Path("data/kalshi_dryrun")
ORDERS_DIR  = Path("data/kalshi_orders")    # Phase 3 live order receipts
HISTORY     = Path("data/pick_history.json")
PERF_OUT    = Path("data/kalshi_dryrun_perf.json")
LIVE_PERF_OUT = Path("data/kalshi_live_perf.json")  # Phase 3 real-money PnL


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


def _reconcile_paper_orders_one_file(path: Path, hist_idx: dict, write_back: bool = True) -> dict | None:
    """
    Reconcile the paper-track orders (paper_orders[]) from a dry-run file
    against actual game outcomes. Returns aggregate stats for the day,
    or None when the file has no paper_orders array (older snapshots
    pre-2026-05-21 — silently skipped).

    Math: paper bets are graded as if we'd bet $1.10 at -110 juice per
    pick (standard sportsbook total / O/U price). Win = +$1.00 profit,
    loss = -$1.10 stake. This makes paper stats directly comparable
    against the break-even 52.4% hit rate we need.

    No Kalshi market lookup happens here — paper picks are pure
    "would we win or lose if we bet at -110" simulations. That's all
    we need to know whether the O/U model is ready for promotion to
    live betting.
    """
    data = json.loads(path.read_text())
    file_date = data.get("date", path.stem)
    paper_orders = data.get("paper_orders")
    if not paper_orders:
        return None   # nothing to reconcile (older file or no O/U picks today)

    annotated = []
    wins = losses = pushes = ungraded = 0
    # Standard -110 sportsbook math for paper grading
    STAKE_PER_PICK   = 1.10
    PROFIT_PER_WIN   = 1.00

    for o in paper_orders:
        pick = o.get("pick", {})
        key = (file_date, pick.get("sport", ""), pick.get("home", ""),
               pick.get("away", ""), pick.get("betType", ""),
               pick.get("pickLabel", ""))
        hist = hist_idx.get(key)
        ann = dict(o)
        if not hist:
            ann["outcome"] = None
            ann["reconcile_status"] = "no_history_match"
            ann["pnl_dollars"] = 0.0
            ungraded += 1
            annotated.append(ann)
            continue
        outcome = hist.get("outcome")
        ann["outcome"] = outcome
        ann["home_score"] = hist.get("homeScore")
        ann["away_score"] = hist.get("awayScore")
        if outcome == "win":
            ann["pnl_dollars"] = PROFIT_PER_WIN
            ann["reconcile_status"] = "graded"
            wins += 1
        elif outcome == "loss":
            ann["pnl_dollars"] = -STAKE_PER_PICK
            ann["reconcile_status"] = "graded"
            losses += 1
        elif outcome == "push":
            ann["pnl_dollars"] = 0.0
            ann["reconcile_status"] = "graded"
            pushes += 1
        else:
            ann["pnl_dollars"] = 0.0
            ann["reconcile_status"] = "ungraded"
            ungraded += 1
        annotated.append(ann)

    placed = wins + losses + pushes + ungraded
    stake = placed * STAKE_PER_PICK
    pnl = sum(a.get("pnl_dollars") or 0 for a in annotated)
    daily = {
        "date": file_date,
        "placed": placed,
        "wins": wins, "losses": losses, "pushes": pushes, "ungraded": ungraded,
        "total_stake_dollars": round(stake, 2),
        "total_pnl_dollars": round(pnl, 2),
        "roi_pct": round((pnl / stake * 100) if stake else 0, 2),
    }

    if write_back:
        data["paper_orders"] = annotated
        data["paper_reconcile"] = daily
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

    # ── PAPER TRACK: grade paper_orders[] (O/U simulated) ─────────────────
    # As of 2026-05-21 the paper track is dedicated to O/U validation.
    # ML used to be reconciled here too but it's now tracked through the
    # live-orders pipeline (no double-counting). Days without paper_orders
    # (older snapshots or no O/U picks) are skipped silently.
    print(f"\n── Paper track (O/U) ──────────────────────────────────────")
    paper_daily = []
    for f in files:
        daily = _reconcile_paper_orders_one_file(f, hist_idx)
        if daily is None: continue
        paper_daily.append(daily)
        n = daily["placed"]
        if n:
            graded = daily["wins"] + daily["losses"] + daily["pushes"]
            ungraded_str = f", {daily['ungraded']} ungraded" if daily["ungraded"] else ""
            print(f"  {daily['date']}: {daily['wins']}-{daily['losses']}"
                  f"{('-'+str(daily['pushes'])) if daily['pushes'] else ''} of {graded}{ungraded_str} · "
                  f"PnL ${daily['total_pnl_dollars']:+.2f} ({daily['roi_pct']:+.1f}%)")
    if not paper_daily:
        print(f"  No paper_orders yet — start producing some by leaving "
              f"paper_supported_bet_types: [\"ou\"] in kalshi_config.json")

    p_stake = sum(d["total_stake_dollars"] for d in paper_daily)
    p_pnl   = sum(d["total_pnl_dollars"]   for d in paper_daily)
    p_placed = sum(d["placed"] for d in paper_daily)
    p_wins  = sum(d["wins"] for d in paper_daily)
    p_losses = sum(d["losses"] for d in paper_daily)
    paper_overall = {
        "days": len(paper_daily),
        "total_orders_placed": p_placed,
        "wins": p_wins, "losses": p_losses,
        "total_stake_dollars": round(p_stake, 2),
        "total_pnl_dollars": round(p_pnl, 2),
        "roi_pct": round((p_pnl / p_stake * 100) if p_stake else 0, 2),
        "win_pct": round((p_wins / (p_wins + p_losses) * 100) if (p_wins + p_losses) else 0, 1),
        "track": "paper_ou",
        "daily": paper_daily,
    }
    PERF_OUT.write_text(json.dumps(paper_overall, indent=2, default=str))
    print(f"\n── Paper aggregate (O/U simulated) ────────────────────────")
    if p_wins + p_losses:
        print(f"  Record: {p_wins}-{p_losses} ({paper_overall['win_pct']:.1f}%) over {p_placed} graded paper picks")
        print(f"  PnL: ${p_pnl:+.2f}    ROI: {paper_overall['roi_pct']:+.1f}%  (need ≥0% to enable live)")
    print(f"  ✅ Wrote {PERF_OUT}")

    # Still update the per-day reconcile fields on the legacy orders[]
    # array (so the hub panel can show ML simulated outcomes as historical
    # context), but DON'T aggregate those into paper-perf anymore — that
    # would double-count with live-perf.
    for f in files:
        _reconcile_one_file(f, hist_idx, write_back=True)

    # ── LIVE TRACK: real ML placements → live-perf ─────────────────────────
    if ORDERS_DIR.exists():
        live_files = sorted(ORDERS_DIR.glob("*.json"))
        if live_files:
            _reconcile_live_orders(live_files, hist_idx)


def _reconcile_live_orders(files, hist_idx):
    """
    Reconcile live (real-money) order receipts against graded picks. Same
    win/loss/PnL math as paper, but tied to actual placed orders rather
    than hypothetical ones. Writes data/kalshi_live_perf.json.
    """
    print(f"\n── Live orders reconcile ─────────────────────────────────")
    daily_summaries = []
    total_wins = total_losses = total_placed = 0
    total_stake = total_pnl = 0.0

    for path in files:
        try:
            data = json.loads(path.read_text())
        except Exception as e:
            print(f"  · {path.name}: parse error ({e})")
            continue
        # Skip dry-mode order files (they have dry: true, no real placement)
        if data.get("dry"):
            continue
        file_date = data.get("date", path.stem)
        annotated = []
        wins = losses = ungraded = 0
        stake = payout = pnl = 0.0

        for o in data.get("placed_orders", []):
            if o.get("dry"):  # safety: skip any per-order dry rows
                annotated.append(o); continue

            pick = o.get("dryrun_pick") or {}
            key = (file_date, pick.get("sport",""), pick.get("home",""),
                   pick.get("away",""), pick.get("betType",""), pick.get("pickLabel",""))
            hist = hist_idx.get(key)
            ann = dict(o)
            outcome = (hist or {}).get("outcome")
            ann["outcome"] = outcome
            ann["home_score"] = (hist or {}).get("homeScore")
            ann["away_score"] = (hist or {}).get("awayScore")

            stake_d = float(o.get("stake_dollars") or 0)
            contracts = int(o.get("contracts") or 0)
            if outcome == "win":
                payout_d = contracts * 1.00
                ann["pnl_dollars"] = round(payout_d - stake_d, 2)
                ann["payout_dollars"] = round(payout_d, 2)
                ann["reconcile_status"] = "graded"
                wins += 1; payout += payout_d; pnl += ann["pnl_dollars"]; stake += stake_d
            elif outcome == "loss":
                ann["pnl_dollars"] = round(-stake_d, 2)
                ann["payout_dollars"] = 0.0
                ann["reconcile_status"] = "graded"
                losses += 1; pnl += ann["pnl_dollars"]; stake += stake_d
            elif outcome == "push":
                ann["pnl_dollars"] = 0.0
                ann["payout_dollars"] = round(stake_d, 2)
                ann["reconcile_status"] = "graded"
                stake += stake_d; payout += stake_d
            else:
                ann["pnl_dollars"] = 0.0
                ann["payout_dollars"] = 0.0
                ann["reconcile_status"] = "ungraded" if hist else "no_history_match"
                ungraded += 1

            annotated.append(ann)

        daily = {
            "date": file_date,
            "placed": len([o for o in annotated if not o.get("dry") and o.get("status") not in (None,"canceled","rejected")]),
            "wins": wins, "losses": losses, "ungraded": ungraded,
            "total_stake_dollars": round(stake, 2),
            "total_payout_dollars": round(payout, 2),
            "total_pnl_dollars": round(pnl, 2),
            "roi_pct": round((pnl/stake*100) if stake else 0, 2),
        }
        # Persist annotated back into the per-day file so the hub panel
        # can show outcomes + PnL alongside placed orders.
        data["placed_orders"] = annotated
        data["reconcile"] = daily
        path.write_text(json.dumps(data, indent=2, default=str))
        daily_summaries.append(daily)

        if daily["placed"]:
            print(f"  {daily['date']}: {wins}-{losses}{f' ({ungraded} ungraded)' if ungraded else ''} · "
                  f"stake ${daily['total_stake_dollars']:.2f} · "
                  f"PnL ${daily['total_pnl_dollars']:+.2f} ({daily['roi_pct']:+.1f}%)")
        total_placed += daily["placed"]; total_wins += wins; total_losses += losses
        total_stake += stake; total_pnl += pnl

    overall = {
        "days": len(daily_summaries),
        "total_orders_placed": total_placed,
        "wins": total_wins, "losses": total_losses,
        "total_stake_dollars": round(total_stake, 2),
        "total_pnl_dollars": round(total_pnl, 2),
        "roi_pct": round((total_pnl/total_stake*100) if total_stake else 0, 2),
        "win_pct": round((total_wins/(total_wins+total_losses)*100) if (total_wins+total_losses) else 0, 1),
        "daily": daily_summaries,
    }
    LIVE_PERF_OUT.write_text(json.dumps(overall, indent=2, default=str))
    if total_wins + total_losses:
        print(f"\n  Live record: {total_wins}-{total_losses} ({overall['win_pct']:.1f}%) over {total_placed} orders")
        print(f"  Live stake:  ${total_stake:.2f}    PnL: ${total_pnl:+.2f}    ROI: {overall['roi_pct']:+.1f}%")
    print(f"  ✅ Wrote {LIVE_PERF_OUT}")


if __name__ == "__main__":
    main()
