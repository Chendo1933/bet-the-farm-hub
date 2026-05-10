#!/usr/bin/env python3
"""
Phase 3 single-order test script.

Use this to verify end-to-end order placement on Kalshi without going
through the dry-run pipeline. Useful for:
  - Confirming Trade-permission auth works
  - Smoke-testing order placement before relying on the auto-pipeline
  - Placing a one-off bet on a market the auto-pipeline wouldn't pick

NOTE: This bypasses the score-threshold and supported-bet-type gates. It
still respects:
  - auto_trading_enabled master flag (hard gate)
  - max_stake_per_pick_dollars cap
  - max_daily_exposure_dollars cap (counts against today's running total)
  - bankroll balance check

Usage:
  python3 scripts/kalshi/test_order.py \\
      --ticker KXNHLGAME-26MAY10TBLDET-DET \\
      --side yes \\
      --contracts 2 \\
      --price-cents 50

  # Dry mode — show what would happen without actually placing:
  python3 scripts/kalshi/test_order.py --ticker ... --side yes --contracts 2 --dry

Required env vars (same as Phase 3 trade workflow):
  KALSHI_API_KEY_ID    — Trade-permission key UUID
  KALSHI_PRIVATE_KEY_PATH  — path to the matching .pem
  KALSHI_ENVIRONMENT   — 'live' or 'demo'

The order is recorded into data/kalshi_orders/{date}.json the same way
auto-placed orders are, so reconcile.py will pick up its outcome
tomorrow morning. Marked with "test": true for traceability.
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

CONFIG_PATH = "data/kalshi_config.json"
ORDERS_DIR  = "data/kalshi_orders"


def _load_config() -> dict:
    raw = json.loads(Path(CONFIG_PATH).read_text())
    return {k: v for k, v in raw.items() if not k.startswith("_") and not k.endswith("_doc")}


def _today_et_date() -> str:
    return datetime.now(ZoneInfo("America/New_York")).strftime("%Y-%m-%d")


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--ticker",   required=True, help="Kalshi market ticker (e.g., KXMLBGAME-26MAY10...)")
    ap.add_argument("--side",     default="yes",  choices=("yes","no"),
                    help="Side of the binary contract (default: yes)")
    ap.add_argument("--contracts", type=int, default=1,
                    help="Number of contracts to buy (default: 1)")
    ap.add_argument("--price-cents", type=int, default=None,
                    help="Limit price in cents. Default: fetch current ask.")
    ap.add_argument("--dry", action="store_true",
                    help="Show what would happen without actually placing an order")
    args = ap.parse_args()

    cfg = _load_config()
    date_key = _today_et_date()
    active_env = os.environ.get("KALSHI_ENVIRONMENT") or cfg.get("environment", "demo")

    print(f"\n{'='*72}")
    print(f"  Phase 3 test order · {date_key} · environment={active_env}")
    print(f"{'='*72}\n")

    # ── Hard gates (same as place_orders.py) ────────────────────────────────
    if not cfg.get("auto_trading_enabled"):
        sys.exit("✗ auto_trading_enabled is FALSE in config — refusing to place")

    if not os.environ.get("KALSHI_API_KEY_ID"):
        sys.exit("✗ KALSHI_API_KEY_ID env var not set")
    if not (os.environ.get("KALSHI_PRIVATE_KEY") or os.environ.get("KALSHI_PRIVATE_KEY_PATH")):
        sys.exit("✗ KALSHI_PRIVATE_KEY or KALSHI_PRIVATE_KEY_PATH must be set")

    client = KalshiClient(environment=active_env)

    # ── Fetch market to verify it exists + grab current ask if not provided
    print(f"Fetching market {args.ticker}…")
    try:
        m = client.get_market(args.ticker).get("market", {})
    except KalshiAPIError as e:
        sys.exit(f"✗ Could not fetch market ({e.status}): {e.body[:200]}")
    if not m:
        sys.exit(f"✗ Market {args.ticker} returned empty response")

    title = m.get("title", "?")
    yes_sub = m.get("yes_sub_title") or m.get("yes_subtitle") or "(unknown)"
    status = m.get("status", "?")
    print(f"  ✓ Market: {title}")
    print(f"  ✓ YES side resolves to: {yes_sub}")
    print(f"  ✓ Market status: {status}")

    if status not in ("active", "open"):
        print(f"  ⚠ Market status is '{status}' — order may be rejected")

    # Resolve price — caller can override; otherwise use current ask for the side.
    def _read_price(market, side: str):
        """Read ask price for the given side (yes or no), trying both new + old fields."""
        if side == "yes":
            v = market.get("yes_ask_dollars")
            if v not in (None, ""):
                try: return round(float(v) * 100)
                except: pass
            return market.get("yes_ask")
        else:
            v = market.get("no_ask_dollars")
            if v not in (None, ""):
                try: return round(float(v) * 100)
                except: pass
            return market.get("no_ask")

    use_price = args.price_cents
    if use_price is None:
        use_price = _read_price(m, args.side)
        if use_price is None:
            sys.exit(f"✗ Market has no current ask for {args.side} side and no --price-cents given")
        print(f"  ✓ Using current {args.side.upper()} ask: {use_price}¢")
    else:
        print(f"  ✓ Using user-specified limit price: {use_price}¢")

    if not (1 <= use_price <= 99):
        sys.exit(f"✗ Price {use_price}¢ outside valid 1..99 range")

    # ── Stake math + caps ────────────────────────────────────────────────────
    stake_dollars = round(args.contracts * (use_price / 100), 2)
    print(f"  ✓ Stake: {args.contracts}× contracts @ {use_price}¢ = ${stake_dollars:.2f}")

    max_per_pick = float(cfg.get("max_stake_per_pick_dollars") or 0)
    if stake_dollars > max_per_pick + 0.01:
        sys.exit(f"✗ Stake ${stake_dollars:.2f} exceeds max_stake_per_pick_dollars ${max_per_pick:.2f}")

    # Check today's existing exposure (cumulative cap)
    orders_path = Path(ORDERS_DIR) / f"{date_key}.json"
    today_existing = 0.0
    if orders_path.exists():
        existing = json.loads(orders_path.read_text())
        for o in existing.get("placed_orders", []) or []:
            if not o.get("dry"):
                today_existing += float(o.get("stake_dollars") or 0)
    max_daily = float(cfg.get("max_daily_exposure_dollars") or 0)
    if today_existing + stake_dollars > max_daily + 0.01:
        sys.exit(f"✗ Combined daily exposure (${today_existing+stake_dollars:.2f}) exceeds cap ${max_daily:.2f}")
    print(f"  ✓ Today's existing exposure: ${today_existing:.2f} · cap: ${max_daily:.2f}")

    # Balance check (skip in dry mode since balance fetch isn't critical there)
    if not args.dry:
        try:
            bal = client.get_balance()
            balance_cents = bal.get("balance", 0) or 0
            balance_dollars = balance_cents / 100
            print(f"  ✓ Account balance: ${balance_dollars:.2f}")
            if balance_dollars < stake_dollars:
                sys.exit(f"✗ Balance ${balance_dollars:.2f} < stake ${stake_dollars:.2f}")
        except Exception as e:
            sys.exit(f"✗ Could not read balance ({e})")

    # ── Place (or simulate) the order ───────────────────────────────────────
    coid = f"btf-test-{date_key}-{uuid.uuid4().hex[:8]}"
    print(f"\n  Order plan:")
    print(f"    ticker:   {args.ticker}")
    print(f"    side:     {args.side}")
    print(f"    action:   buy")
    print(f"    count:    {args.contracts}")
    print(f"    price:    {use_price}¢ (limit)")
    print(f"    stake:    ${stake_dollars:.2f}")
    print(f"    coid:     {coid}")

    if args.dry:
        print(f"\n[DRY] Would POST to /portfolio/orders. Skipping actual placement.\n")
        return

    print(f"\n→ Placing order…")
    try:
        order_args = dict(
            ticker=args.ticker, side=args.side, action="buy",
            count=args.contracts, order_type="limit",
            client_order_id=coid,
        )
        if args.side == "yes":
            order_args["yes_price_cents"] = use_price
        else:
            order_args["no_price_cents"] = use_price
        resp = client.create_order(**order_args)
    except KalshiAPIError as e:
        sys.exit(f"✗ Order rejected by Kalshi ({e.status}): {e.body[:300]}")
    except Exception as e:
        sys.exit(f"✗ Order failed: {type(e).__name__}: {e}")

    order_obj = resp.get("order") if isinstance(resp.get("order"), dict) else resp
    order_id = order_obj.get("order_id")
    status = order_obj.get("status", "?")
    print(f"  ✓ ORDER PLACED")
    print(f"    order_id: {order_id}")
    print(f"    status:   {status}")

    # ── Record into the same orders file structure as auto-placement ───────
    new_order = {
        "ticker": args.ticker,
        "side": args.side,
        "contracts": args.contracts,
        "price_cents": use_price,
        "stake_dollars": stake_dollars,
        "client_order_id": coid,
        "order_id": order_id,
        "status": status,
        "test": True,
        "kalshi_response": order_obj,
        "market_title": title,
    }

    Path(ORDERS_DIR).mkdir(parents=True, exist_ok=True)
    if orders_path.exists():
        out = json.loads(orders_path.read_text())
    else:
        out = {
            "date": date_key,
            "logged": datetime.now().isoformat(),
            "environment": active_env,
            "dry": False,
            "placed_orders": [],
            "skipped": [],
            "summary": {},
        }
    out["placed_orders"].append(new_order)
    out["last_test_order_at"] = datetime.now().isoformat()
    orders_path.write_text(json.dumps(out, indent=2, default=str))

    print(f"\n  ✓ Recorded receipt → {orders_path}")
    print(f"\n{'='*72}")
    print(f"  Now go to https://kalshi.com/portfolio to verify the order is open.")
    print(f"  After the underlying game settles, run scripts/kalshi/reconcile.py")
    print(f"  (or wait for the nightly auto-reconcile) to compute PnL.")
    print(f"{'='*72}\n")


if __name__ == "__main__":
    main()
