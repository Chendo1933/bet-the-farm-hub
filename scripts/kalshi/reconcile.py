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
ALT_TOTAL_PERF_OUT = Path("data/kalshi_alt_total_perf.json")  # MLB alt-total paper


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


def _f5_outcome_for_pick(pick: dict, file_date: str) -> tuple[str | None, dict | None]:
    """
    Grade an f5_ou paper pick against the actual F5 score in
    data/results/{date}.json (written by scripts/fetch_f5_scores.py).

    Returns (outcome, score_info):
      outcome ∈ {'win','loss','push',None}
      score_info contains f5_total/f5_home/f5_away for audit trail.

    Returns (None, None) when:
      - The day's results file isn't present yet (game not finished)
      - The specific game isn't found in the file
      - f5_total isn't populated (fetch_f5_scores hasn't run for that day)
      - f5_complete is False (game shortened — biased sample, skip)
    """
    home = pick.get("home", ""); away = pick.get("away", "")
    line = pick.get("total")
    side = pick.get("pickedTeam", "").lower()  # 'over' or 'under'
    if not home or not away or line is None or side not in ("over","under"):
        return (None, None)

    results_path = Path(f"data/results/{file_date}.json")
    if not results_path.exists():
        return (None, None)
    try:
        data = json.loads(results_path.read_text())
    except Exception:
        return (None, None)
    mlb = data.get("sports", {}).get("mlb", [])
    if not isinstance(mlb, list):
        return (None, None)

    for g in mlb:
        g_home = g.get("home_db") or g.get("home")
        g_away = g.get("away_db") or g.get("away")
        if g_home != home or g_away != away:
            continue
        if not g.get("f5_complete"):
            return (None, None)   # rain-shortened, walkoff in 5th, etc. — skip
        f5_total = g.get("f5_total")
        if f5_total is None:
            return (None, None)

        # Pick line is "{line}.5 F5" per the pick label — so f5_total > line
        # means Over wins. Exact-half lines (e.g. 4.5) never push.
        actual_over = f5_total > line
        if side == "over":
            outcome = "win" if actual_over else "loss"
        else:
            outcome = "win" if not actual_over else "loss"
        return (outcome, {
            "f5_total": f5_total,
            "f5_home":  g.get("f5_home"),
            "f5_away":  g.get("f5_away"),
            "line":     line,
        })
    return (None, None)


def _spread_outcome_for_pick(pick: dict, spread_meta: dict, file_date: str) -> tuple[str | None, dict | None]:
    """
    Grade a spread paper pick against the actual final score and the
    Kalshi line we'd have bet (spread_meta.spread_line_bet), NOT the
    consensus line — because the Kalshi bet is what we're validating.

    yes_side semantics from find_market_for_spread_pick:
      'YES' = favorite, market is "{picked} wins by over {line}"
              → win if picked margin > line
      'NO'  = underdog, market is "{opponent} wins by over {line}"
              → win if opponent margin <= line (picked covered the +line)

    Returns (outcome, info). (None, None) when the game isn't final yet
    or isn't found in the results file.
    """
    home = pick.get("home", ""); away = pick.get("away", "")
    ats  = pick.get("atsPick")
    line = spread_meta.get("spread_line_bet")
    side = spread_meta.get("yes_side")
    if not home or not away or ats not in ("home","away") or line is None or side not in ("YES","NO"):
        return (None, None)

    results_path = Path(f"data/results/{file_date}.json")
    if not results_path.exists():
        return (None, None)
    try:
        data = json.loads(results_path.read_text())
    except Exception:
        return (None, None)
    sport_key = (pick.get("sport") or "").lower()
    games = data.get("sports", {}).get(sport_key, [])
    for g in games:
        g_home = g.get("home_db") or g.get("home")
        g_away = g.get("away_db") or g.get("away")
        if g_home != home or g_away != away:
            continue
        hs = g.get("home_score"); as_ = g.get("away_score")
        if hs is None or as_ is None:
            return (None, None)
        picked   = home if ats == "home" else away
        picked_score = hs if ats == "home" else as_
        opp_score    = as_ if ats == "home" else hs
        picked_margin = picked_score - opp_score   # positive = picked won by this
        if side == "YES":
            # Favorite: "picked wins by over line"
            won = picked_margin > line
        else:
            # Underdog NO: "opponent wins by over line" must be FALSE →
            # opponent's margin (−picked_margin) must be ≤ line
            won = (-picked_margin) <= line
        outcome = "win" if won else "loss"
        return (outcome, {"picked_margin": picked_margin, "line": line, "side": side})
    return (None, None)


def _alt_total_outcome_for_pick(pick: dict, file_date: str) -> tuple[str | None, dict | None]:
    """Grade an MLB alt-total paper pick against the actual final total.
    pick has home/away/line/side ('over'/'under'). Returns (outcome, info) or
    (None, None) if the game isn't final yet / not found."""
    home = pick.get("home", ""); away = pick.get("away", "")
    line = pick.get("line"); side = (pick.get("side") or "").lower()
    if not home or not away or line is None or side not in ("over", "under"):
        return (None, None)
    rp = Path(f"data/results/{file_date}.json")
    if not rp.exists():
        return (None, None)
    try:
        data = json.loads(rp.read_text())
    except Exception:
        return (None, None)
    for g in data.get("sports", {}).get("mlb", []):
        if not isinstance(g, dict):
            continue
        if (g.get("home_db") or g.get("home")) != home or (g.get("away_db") or g.get("away")) != away:
            continue
        hs = g.get("home_score"); as_ = g.get("away_score")
        if hs is None or as_ is None:
            return (None, None)
        actual = hs + as_
        over = actual > line   # lines are .5 → no push
        outcome = "win" if (over if side == "over" else not over) else "loss"
        return (outcome, {"actual_total": actual, "line": line, "side": side})
    return (None, None)


def _reconcile_paper_orders_one_file(path: Path, hist_idx: dict, write_back: bool = True) -> dict | None:
    """
    Reconcile the paper-track orders (paper_orders[]) from a dry-run file
    against actual outcomes. Returns aggregate stats for the day, or None
    when the file has no paper_orders array (older snapshots — silently
    skipped).

    Bet types handled here:
      ou        → graded against full-game total via pick_history.json
      f5_ou     → graded against F5 total via data/results/{date}.json
      spread    → graded against final score vs the Kalshi line we bet
      alt_total → graded against final total at the REAL Kalshi price (kept in
                  a SEPARATE sub-aggregate so its variable-price PnL doesn't
                  distort the flat-110 O/U numbers)

    Math: ou/f5_ou/spread graded at standard -110 juice (win +$1.00 / loss
    -$1.10). alt_total graded at its actual Kalshi contract price.
    """
    data = json.loads(path.read_text())
    file_date = data.get("date", path.stem)
    paper_orders = data.get("paper_orders")
    if not paper_orders:
        return None

    annotated = []
    wins = losses = pushes = ungraded = 0
    STAKE_PER_PICK   = 1.10
    PROFIT_PER_WIN   = 1.00
    # alt_total kept separate (variable Kalshi price, not flat -110)
    alt = {"placed": 0, "wins": 0, "losses": 0, "ungraded": 0,
           "cost": 0.0, "pnl": 0.0}

    for o in paper_orders:
        pick = o.get("pick", {})
        bet_type = pick.get("betType", "")
        ann = dict(o)

        # ── Alt-total grading (real Kalshi price, separate aggregate) ──
        if bet_type == "alt_total":
            outcome, info = _alt_total_outcome_for_pick(pick, file_date)
            if info:
                ann["alt_grade_info"] = info
            price = float((o.get("alt_meta") or {}).get("kalshi_price") or 0.0)
            ann["alt"] = True
            if outcome is None or price <= 0:
                ann["outcome"] = None
                ann["reconcile_status"] = "ungraded_no_score"
                ann["pnl_dollars"] = 0.0
                alt["ungraded"] += 1
                annotated.append(ann)
                continue
            ann["outcome"] = outcome
            # 1 contract: cost = price, payout = $1 if win. pnl = 1-price / -price.
            pnl = round((1.0 - price) if outcome == "win" else -price, 4)
            ann["pnl_dollars"] = pnl
            ann["reconcile_status"] = "graded"
            alt["placed"] += 1
            alt["cost"] += price
            alt["pnl"] += pnl
            alt["wins" if outcome == "win" else "losses"] += 1
            annotated.append(ann)
            continue

        # ── F5 grading path ────────────────────────────────────────────
        if bet_type == "f5_ou":
            outcome, score_info = _f5_outcome_for_pick(pick, file_date)
            if score_info:
                ann["f5_score_info"] = score_info
            if outcome is None:
                ann["outcome"] = None
                ann["reconcile_status"] = "ungraded_no_f5_data"
                ann["pnl_dollars"] = 0.0
                ungraded += 1
                annotated.append(ann)
                continue
            ann["outcome"] = outcome
            if outcome == "win":
                ann["pnl_dollars"] = PROFIT_PER_WIN; ann["reconcile_status"] = "graded"; wins += 1
            elif outcome == "loss":
                ann["pnl_dollars"] = -STAKE_PER_PICK; ann["reconcile_status"] = "graded"; losses += 1
            else:
                ann["pnl_dollars"] = 0.0; ann["reconcile_status"] = "graded"; pushes += 1
            annotated.append(ann)
            continue

        # ── Spread grading path ───────────────────────────────────────
        if bet_type == "spread":
            sm = o.get("spread_meta") or {}
            outcome, info = _spread_outcome_for_pick(pick, sm, file_date)
            if info:
                ann["spread_grade_info"] = info
            if outcome is None:
                ann["outcome"] = None
                ann["reconcile_status"] = "ungraded_no_score"
                ann["pnl_dollars"] = 0.0
                ungraded += 1
                annotated.append(ann)
                continue
            ann["outcome"] = outcome
            if outcome == "win":
                ann["pnl_dollars"] = PROFIT_PER_WIN; ann["reconcile_status"] = "graded"; wins += 1
            else:
                ann["pnl_dollars"] = -STAKE_PER_PICK; ann["reconcile_status"] = "graded"; losses += 1
            annotated.append(ann)
            continue

        # ── Full-game O/U grading path (existing logic) ───────────────
        key = (file_date, pick.get("sport", ""), pick.get("home", ""),
               pick.get("away", ""), bet_type, pick.get("pickLabel", ""))
        hist = hist_idx.get(key)
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
    # Flat -110 pnl excludes alt_total (its variable-price pnl is tracked apart).
    pnl = sum(a.get("pnl_dollars") or 0 for a in annotated if not a.get("alt"))
    daily = {
        "date": file_date,
        "placed": placed,
        "wins": wins, "losses": losses, "pushes": pushes, "ungraded": ungraded,
        "total_stake_dollars": round(stake, 2),
        "total_pnl_dollars": round(pnl, 2),
        "roi_pct": round((pnl / stake * 100) if stake else 0, 2),
    }
    if alt["placed"] or alt["ungraded"]:
        graded = alt["wins"] + alt["losses"]
        daily["alt_total"] = {
            "placed": alt["placed"], "wins": alt["wins"], "losses": alt["losses"],
            "ungraded": alt["ungraded"],
            "cost_dollars": round(alt["cost"], 2),
            "pnl_dollars": round(alt["pnl"], 2),
            "roi_pct": round((alt["pnl"] / alt["cost"] * 100) if alt["cost"] else 0, 2),
            "win_pct": round((100 * alt["wins"] / graded) if graded else 0, 1),
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

    # ── ALT-TOTAL paper track (MLB, real Kalshi price, separate aggregate) ──
    alt_daily = [d["alt_total"] | {"date": d["date"]} for d in paper_daily if d.get("alt_total")]
    if alt_daily:
        a_w = sum(d["wins"] for d in alt_daily); a_l = sum(d["losses"] for d in alt_daily)
        a_cost = sum(d["cost_dollars"] for d in alt_daily)
        a_pnl = sum(d["pnl_dollars"] for d in alt_daily)
        a_graded = a_w + a_l
        alt_overall = {
            "days": len(alt_daily), "wins": a_w, "losses": a_l,
            "graded": a_graded,
            "ungraded": sum(d["ungraded"] for d in alt_daily),
            "total_cost_dollars": round(a_cost, 2),
            "total_pnl_dollars": round(a_pnl, 2),
            "roi_pct": round((a_pnl / a_cost * 100) if a_cost else 0, 2),
            "win_pct": round((100 * a_w / a_graded) if a_graded else 0, 1),
            "track": "paper_alt_total", "daily": alt_daily,
        }
        ALT_TOTAL_PERF_OUT.write_text(json.dumps(alt_overall, indent=2, default=str))
        print(f"\n── Alt-total paper aggregate (MLB, real price) ────────────")
        if a_graded:
            print(f"  Record: {a_w}-{a_l} ({alt_overall['win_pct']:.1f}%) · "
                  f"PnL ${a_pnl:+.2f} · ROI {alt_overall['roi_pct']:+.1f}% over {a_graded} graded")
        print(f"  ✅ Wrote {ALT_TOTAL_PERF_OUT}")

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
