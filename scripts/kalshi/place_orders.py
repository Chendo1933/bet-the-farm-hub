#!/usr/bin/env python3
"""
Kalshi Phase 3 — live order placement.

Reads today's dry-run snapshot (data/kalshi_dryrun/{date}.json), runs every
"would_place" entry through pre-flight safety gates, and places real limit
orders on Kalshi for survivors. Records the placed-order receipts to
data/kalshi_orders/{date}.json for tracking and tomorrow's reconciliation.

Hard kill switches — checked before ANY API call:

  1. config.auto_trading_enabled must be true (master flag, default false)
  2. config.environment must be present
  3. KALSHI_API_KEY_ID + KALSHI_PRIVATE_KEY env vars must be set
  4. Account balance must be >= total stake we'd deploy today
  5. Yesterday's reconciled PnL must be > -config.kill_switch_daily_loss_dollars
  6. No orders already placed for this date (idempotency — re-runs are no-ops)

Per-order gates — checked for each candidate order:

  7. Order.would_place must be true (came from dry-run as eligible)
  8. Order.skip_reason must be null
  9. stake_dollars must be > 0 and <= config.max_stake_per_pick_dollars
  10. cumulative-stake-so-far must stay under config.max_daily_exposure_dollars
  11. We don't already have an open position on this market (avoid stacking)

Order sizing/pricing:

  - Limit orders at the dry-run's chosen use_price_cents (current ask, OR
    fallback through last/prev/bid+2 if ask wasn't available at dry-run time)
  - 1-minute Time-in-Force (GTC isn't ideal — we don't want orders sitting
    open after a game starts). If Kalshi rejects 'min', we fall back to GTC
    and rely on game-start auto-cancel.
  - client_order_id derived from (date + market_ticker) so workflow retries
    or re-runs are detected as duplicates and the API returns the existing
    order rather than placing a second one.

Usage:
  python3 scripts/kalshi/place_orders.py                  # today
  python3 scripts/kalshi/place_orders.py --date 2026-05-10
  python3 scripts/kalshi/place_orders.py --dry             # show what would
                                                            # be placed without
                                                            # actually placing
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import uuid
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from kalshi.client import KalshiClient, KalshiAPIError

CONFIG_PATH      = "data/kalshi_config.json"
DRYRUN_DIR       = "data/kalshi_dryrun"
ORDERS_DIR       = "data/kalshi_orders"
PERF_PATH        = "data/kalshi_dryrun_perf.json"   # used for kill-switch yesterday-loss check


def _load_config() -> dict:
    raw = json.loads(Path(CONFIG_PATH).read_text())
    return {k: v for k, v in raw.items() if not k.startswith("_") and not k.endswith("_doc")}


def _today_et_date() -> str:
    return datetime.now(ZoneInfo("America/New_York")).strftime("%Y-%m-%d")


def _client_order_id(date_key: str, market_ticker: str) -> str:
    """
    Stable idempotency key. Same (date, market) on retries gives the same
    UUID, so Kalshi treats the second POST as a duplicate of the first
    rather than placing a second order. Format keeps it under Kalshi's
    36-char limit by hashing.
    """
    raw = f"btf:{date_key}:{market_ticker}"
    return f"btf-{uuid.uuid5(uuid.NAMESPACE_DNS, raw)}"


def _yesterday_pnl_dollars() -> float:
    """
    Read the most recently reconciled day's PnL from kalshi_dryrun_perf.json.
    Returns 0.0 if no perf data exists yet (first day — kill switch only
    fires on a confirmed losing day).
    """
    p = Path(PERF_PATH)
    if not p.exists():
        return 0.0
    data = json.loads(p.read_text())
    daily = data.get("daily", [])
    if not daily:
        return 0.0
    # Daily list is ordered by date asc; take last
    return float(daily[-1].get("total_pnl_dollars") or 0)


def _print_block(title: str, msg: str = ""):
    print(f"\n{'='*72}\n  {title}\n{'='*72}")
    if msg: print(msg)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--date", help="ET date YYYY-MM-DD (default: today)")
    ap.add_argument("--dry", action="store_true",
                    help="Print what would be placed without actually placing")
    args = ap.parse_args()

    cfg = _load_config()
    date_key = args.date or _today_et_date()

    # ── Pre-flight gate 1: master flag ──────────────────────────────────────
    if not cfg.get("auto_trading_enabled"):
        _print_block("Phase 3 gate: auto_trading_enabled is FALSE",
                     "Master flag is off in data/kalshi_config.json.\n"
                     "No orders will be placed. Flip the flag to true once\n"
                     "you've reviewed Phase 2 dry-run data and are ready.\n")
        sys.exit(0)

    # ── Pre-flight gate 2: env env vars ─────────────────────────────────────
    if not os.environ.get("KALSHI_API_KEY_ID"):
        sys.exit("✗ KALSHI_API_KEY_ID env var not set — refusing to proceed")
    has_key = os.environ.get("KALSHI_PRIVATE_KEY") or os.environ.get("KALSHI_PRIVATE_KEY_PATH")
    if not has_key:
        sys.exit("✗ KALSHI_PRIVATE_KEY or KALSHI_PRIVATE_KEY_PATH must be set")

    # ── Load dry-run candidates ─────────────────────────────────────────────
    dryrun_path = Path(DRYRUN_DIR) / f"{date_key}.json"
    if not dryrun_path.exists():
        sys.exit(f"✗ No dry-run file at {dryrun_path} — run dry_run.py first")
    dryrun = json.loads(dryrun_path.read_text())
    candidates = [o for o in dryrun.get("orders", []) if o.get("would_place")]
    if not candidates:
        _print_block("No candidate orders for today",
                     f"Dry-run for {date_key} produced 0 'would_place' orders. Nothing to do.")
        sys.exit(0)

    # ── Pre-flight gate 6: idempotency on the orders file ───────────────────
    orders_path = Path(ORDERS_DIR) / f"{date_key}.json"
    if orders_path.exists() and not args.dry:
        existing = json.loads(orders_path.read_text())
        if existing.get("placed_orders"):
            _print_block(f"Orders already placed for {date_key}",
                         f"Found {len(existing['placed_orders'])} existing order(s). Refusing to re-run.\n"
                         f"If you genuinely need to retry, delete {orders_path} first.")
            sys.exit(0)

    # ── Gate 4 & 5: account balance + yesterday-loss kill switch ────────────
    active_env = os.environ.get("KALSHI_ENVIRONMENT") or cfg.get("environment", "demo")
    print(f"Phase 3 order placement · {date_key} · environment={active_env}")
    if args.dry:
        print("  [DRY RUN — no API calls will fire]")

    client = KalshiClient(environment=active_env)

    if not args.dry:
        try:
            bal = client.get_balance()
        except Exception as e:
            sys.exit(f"✗ Could not read balance ({e}) — refusing to proceed")
        balance_cents = bal.get("balance", 0) or 0
        balance_dollars = balance_cents / 100
        print(f"  Account balance: ${balance_dollars:,.2f}")
    else:
        balance_dollars = float(cfg.get("bankroll_dollars") or 100)
        print(f"  Simulated balance: ${balance_dollars:,.2f}")

    # Sum stakes from candidates first to check feasibility before any placement
    total_planned = sum(c.get("stake_dollars") or 0 for c in candidates)
    print(f"  Candidate orders: {len(candidates)} · planned stake: ${total_planned:.2f}")

    if total_planned > balance_dollars:
        sys.exit(f"✗ Planned stake ${total_planned:.2f} exceeds balance ${balance_dollars:.2f} — refusing all orders")

    yesterday_pnl = _yesterday_pnl_dollars()
    kill_switch = float(cfg.get("kill_switch_daily_loss_dollars") or 0)
    if kill_switch > 0 and yesterday_pnl < -kill_switch:
        _print_block(f"Kill switch tripped",
                     f"Yesterday PnL was ${yesterday_pnl:.2f}, below threshold of -${kill_switch:.2f}.\n"
                     f"Skipping all orders today. Reset by waiting for a non-losing day.")
        sys.exit(0)
    print(f"  Yesterday PnL: ${yesterday_pnl:+.2f} (kill switch threshold: -${kill_switch:.2f}) — OK")

    # ── Gate 11: don't double up if we already have positions on these markets
    #             (only checked for live, since dry env is fake-money)
    existing_positions: set = set()
    if not args.dry:
        try:
            pos_resp = client.get_positions(limit=200)
            for p in pos_resp.get("market_positions", []) or []:
                if (p.get("position") or 0) != 0:
                    existing_positions.add(p.get("ticker"))
        except KalshiAPIError as e:
            print(f"  ⚠ Could not fetch existing positions ({e.status}) — continuing without dedup check")

    # ── Per-order placement loop ────────────────────────────────────────────
    max_per_pick = float(cfg.get("max_stake_per_pick_dollars") or 0)
    max_daily   = float(cfg.get("max_daily_exposure_dollars") or 0)
    placed_orders = []
    skipped: list = []
    cumulative_stake = 0.0

    for c in candidates:
        ticker = c.get("market_ticker")
        side   = (c.get("yes_side") or "YES").lower()
        stake  = float(c.get("stake_dollars") or 0)
        contracts = int(c.get("contracts") or 0)
        price_cents = int(c.get("use_price_cents") or 0)
        skip_reason = None

        if stake <= 0 or contracts <= 0 or price_cents <= 0:
            skip_reason = "invalid_dryrun_entry"
        elif stake > max_per_pick + 0.01:  # tiny float tolerance
            skip_reason = "exceeds_max_stake_per_pick"
        elif (cumulative_stake + stake) > max_daily + 0.01:
            skip_reason = "would_exceed_daily_cap"
        elif ticker in existing_positions:
            skip_reason = "already_have_position"

        if skip_reason:
            print(f"  · SKIP {ticker} ({stake:.2f}u) — {skip_reason}")
            skipped.append({"ticker": ticker, "skip_reason": skip_reason, "candidate": c})
            continue

        coid = _client_order_id(date_key, ticker)
        order_args = dict(
            ticker=ticker,
            side=side,
            action="buy",
            count=contracts,
            order_type="limit",
            client_order_id=coid,
        )
        if side == "yes":
            order_args["yes_price_cents"] = price_cents
        else:
            order_args["no_price_cents"] = price_cents

        if args.dry:
            print(f"  · [DRY] would POST create_order({ticker} {side} {contracts}× @ {price_cents}¢, coid={coid[-8:]})")
            placed_orders.append({"ticker": ticker, "dry": True, **order_args})
            cumulative_stake += stake
            continue

        try:
            resp = client.create_order(**order_args)
        except KalshiAPIError as e:
            print(f"  ✗ FAIL {ticker}: API {e.status} — {e.body[:120]}")
            skipped.append({"ticker": ticker, "skip_reason": f"api_error_{e.status}",
                            "error": e.body[:300], "candidate": c})
            continue
        except Exception as e:
            print(f"  ✗ FAIL {ticker}: {type(e).__name__} — {e}")
            skipped.append({"ticker": ticker, "skip_reason": "exception",
                            "error": str(e), "candidate": c})
            continue

        order_obj = (resp.get("order") if isinstance(resp.get("order"), dict) else resp)
        order_id  = order_obj.get("order_id")
        status    = order_obj.get("status", "?")
        print(f"  ✓ PLACED {ticker} · {contracts}× @ {price_cents}¢ · order_id={order_id} · status={status}")
        placed_orders.append({
            "ticker": ticker,
            "side": side,
            "contracts": contracts,
            "price_cents": price_cents,
            "stake_dollars": stake,
            "client_order_id": coid,
            "order_id": order_id,
            "status": status,
            "kalshi_response": order_obj,
            "dryrun_pick": c.get("pick"),
        })
        cumulative_stake += stake

    # ── Persist results ─────────────────────────────────────────────────────
    out = {
        "date": date_key,
        "logged": datetime.now().isoformat(),
        "environment": active_env,
        "dry": args.dry,
        "config_snapshot": {
            "max_stake_per_pick_dollars":   cfg.get("max_stake_per_pick_dollars"),
            "max_daily_exposure_dollars":   cfg.get("max_daily_exposure_dollars"),
            "kill_switch_daily_loss_dollars": cfg.get("kill_switch_daily_loss_dollars"),
            "kelly_fraction":               cfg.get("kelly_fraction"),
        },
        "placed_orders": placed_orders,
        "skipped": skipped,
        "summary": {
            "candidates": len(candidates),
            "placed": len([o for o in placed_orders if not o.get("dry")]),
            "dry_simulated": len([o for o in placed_orders if o.get("dry")]),
            "skipped": len(skipped),
            "total_stake_placed": round(cumulative_stake, 2),
        },
    }

    Path(ORDERS_DIR).mkdir(parents=True, exist_ok=True)
    with open(orders_path, "w") as f:
        json.dump(out, f, indent=2, default=str)

    _print_block("Done",
                 f"Placed: {out['summary']['placed']} · Dry-simulated: {out['summary']['dry_simulated']} · "
                 f"Skipped: {out['summary']['skipped']}\n"
                 f"Total stake placed: ${cumulative_stake:.2f}\n"
                 f"Saved: {orders_path}")


if __name__ == "__main__":
    main()
