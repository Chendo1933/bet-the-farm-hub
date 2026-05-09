#!/usr/bin/env python3
"""
Kalshi Phase 2 — daily dry-run order simulator.

Reads today's picks file, runs the pick→market mapper, fetches current
Kalshi prices, computes hypothetical Kelly stakes, and writes the
"would-have-placed" orders to data/kalshi_dryrun/{ET-today}.json.

NO ORDERS ARE PLACED. This is purely a paper-trading simulation that
lets us measure Kalshi's effective ROI before risking real money.

Run:
  python3 scripts/kalshi/dry_run.py                    # today
  python3 scripts/kalshi/dry_run.py --date 2026-05-09  # specific date
  python3 scripts/kalshi/dry_run.py --picks-file PATH  # explicit file

Config: data/kalshi_config.json (bankroll, kelly_fraction, max_stake,
        max_daily_exposure, min_calibrated_score, etc.)

Output schema (data/kalshi_dryrun/{date}.json):
  {
    "date": "2026-05-09",
    "logged": "ISO timestamp",
    "config_snapshot": { ... },
    "orders": [
      {
        "pick": { ...from picks file... },
        "market_ticker": "KXMLBGAME-...",
        "yes_side": "YES",
        "yes_ask_cents": 60,
        "model_prob": 0.65,
        "stake_dollars": 9.60,
        "contracts": 16,
        "edge_pct": 0.05,
        "would_place": true,
        "skip_reason": null     # or 'no_market' / 'no_ask' / 'no_edge' / etc.
      },
      ...
    ],
    "summary": {
      "picks_total": int,
      "picks_eligible_after_score_floor": int,
      "orders_would_place": int,
      "total_stake_dollars": float,
      "skipped_by_reason": { ... }
    }
  }
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from kalshi.client import KalshiClient
from kalshi.pick_mapper import find_market_for_ml_pick
from kalshi.stake import kelly_stake_dollars

CONFIG_PATH = "data/kalshi_config.json"
DRYRUN_DIR  = "data/kalshi_dryrun"


def _load_config() -> dict:
    if not Path(CONFIG_PATH).exists():
        sys.exit(f"Config not found at {CONFIG_PATH}")
    raw = json.loads(Path(CONFIG_PATH).read_text())
    # Strip _doc keys (they're documentation only)
    return {k: v for k, v in raw.items() if not k.startswith("_")
            and not k.endswith("_doc")}


def _config_snapshot(cfg: dict) -> dict:
    """Just the values that affect simulation outcomes — for replay/audit."""
    keep = (
        "environment", "min_calibrated_score", "supported_bet_types",
        "max_stake_per_pick_dollars", "max_daily_exposure_dollars",
        "kelly_fraction", "bankroll_dollars",
        "skip_if_yes_ask_above_cents",
    )
    return {k: cfg.get(k) for k in keep}


def _model_prob_from_pick(pick: dict) -> float | None:
    """Calibrated score / 100 → model's win probability."""
    s = pick.get("score100")
    if s is None: return None
    return max(0.01, min(0.99, s / 100.0))


def _today_et_date() -> str:
    return datetime.now(ZoneInfo("America/New_York")).strftime("%Y-%m-%d")


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--date", help="ET date YYYY-MM-DD (defaults to today)")
    ap.add_argument("--picks-file", help="Override picks file path")
    args = ap.parse_args()

    cfg = _load_config()
    date_key = args.date or _today_et_date()
    picks_path = Path(args.picks_file) if args.picks_file else Path(f"data/picks/{date_key}.json")

    if not picks_path.exists():
        sys.exit(f"Picks file not found: {picks_path}")

    print(f"Kalshi dry-run · {date_key} · environment={cfg.get('environment','demo')}")
    print(f"  Reading {picks_path}")
    data = json.loads(picks_path.read_text())
    all_picks = data.get("picks", [])
    print(f"  {len(all_picks)} pick(s) in file")

    # Filter to supported bet types (ML only in Phase 2) AND score floor.
    supported = set(cfg.get("supported_bet_types") or ["ml"])
    min_score = int(cfg.get("min_calibrated_score") or 0)
    eligible = [p for p in all_picks
                if p.get("betType") in supported
                and (p.get("score100") or 0) >= min_score]
    print(f"  {len(eligible)} eligible after score≥{min_score} + bet-type filter")

    # Map each eligible pick to a Kalshi market (live API call).
    client = KalshiClient(environment=cfg.get("environment"))
    orders = []
    skipped: dict = {}
    daily_exposure = 0.0
    daily_cap = float(cfg.get("max_daily_exposure_dollars") or 0)

    for pick in eligible:
        mapping = find_market_for_ml_pick(client, pick)
        order = {
            "pick": pick,
            "market_ticker": mapping.get("market_ticker"),
            "market_title":  mapping.get("market_title"),
            "yes_side":      mapping.get("yes_side"),
            "yes_ask_cents": mapping.get("current_yes_ask_cents"),
            "model_prob":    _model_prob_from_pick(pick),
            "would_place":   False,
            "skip_reason":   None,
        }

        # Map status checks — only 'matched' status means we can size a stake.
        if mapping.get("status") != "matched":
            order["skip_reason"] = mapping["status"]
            order["map_reason"] = mapping.get("reason")
            orders.append(order)
            skipped[order["skip_reason"]] = skipped.get(order["skip_reason"], 0) + 1
            continue

        # Direction: we always want YES on the picked team's market. If our
        # mapper found the team on the NO side instead, we'd buy NO at
        # (100 - yes_ask) cents. For now Phase 2 only supports YES placement;
        # NO-side picks are skipped (very rare per current mapper behavior).
        if order["yes_side"] != "YES":
            order["skip_reason"] = "no_side_unsupported"
            orders.append(order)
            skipped["no_side_unsupported"] = skipped.get("no_side_unsupported", 0) + 1
            continue

        sized = kelly_stake_dollars(
            bankroll_dollars            = float(cfg.get("bankroll_dollars") or 0),
            kelly_fraction              = float(cfg.get("kelly_fraction") or 0.25),
            model_prob                  = order["model_prob"],
            yes_ask_cents               = order["yes_ask_cents"],
            max_stake_dollars           = float(cfg.get("max_stake_per_pick_dollars") or 0),
            skip_if_yes_ask_above_cents = cfg.get("skip_if_yes_ask_above_cents"),
        )
        order["stake_dollars"]       = sized["stake_dollars"]
        order["contracts"]           = sized["contracts"]
        order["edge_pct"]            = sized["edge_pct"]
        order["kelly_fraction_used"] = sized["kelly_fraction_used"]

        if sized["skip_reason"]:
            order["skip_reason"] = sized["skip_reason"]
            skipped[sized["skip_reason"]] = skipped.get(sized["skip_reason"], 0) + 1
            orders.append(order)
            continue

        # Daily exposure cap — once cumulative stakes hit the cap, the rest
        # of the slate is skipped to defend against runaway placement.
        if daily_cap and (daily_exposure + sized["stake_dollars"]) > daily_cap:
            order["skip_reason"] = "daily_cap_exceeded"
            skipped["daily_cap_exceeded"] = skipped.get("daily_cap_exceeded", 0) + 1
            orders.append(order)
            continue

        order["would_place"] = True
        daily_exposure += sized["stake_dollars"]
        orders.append(order)

    placed = sum(1 for o in orders if o["would_place"])
    total_stake = round(sum(o.get("stake_dollars") or 0 for o in orders if o["would_place"]), 2)

    summary = {
        "picks_total": len(all_picks),
        "picks_eligible_after_filter": len(eligible),
        "orders_would_place": placed,
        "total_stake_dollars": total_stake,
        "remaining_daily_capacity": round(max(0, daily_cap - total_stake), 2),
        "skipped_by_reason": skipped,
    }
    out = {
        "date": date_key,
        "logged": datetime.now().isoformat(),
        "config_snapshot": _config_snapshot(cfg),
        "orders": orders,
        "summary": summary,
    }

    Path(DRYRUN_DIR).mkdir(parents=True, exist_ok=True)
    out_path = Path(DRYRUN_DIR) / f"{date_key}.json"
    out_path.write_text(json.dumps(out, indent=2, default=str))

    # Console summary
    print(f"\n── Dry-run summary ──────────────────────────────────────")
    print(f"  Picks: {summary['picks_total']} total, "
          f"{summary['picks_eligible_after_filter']} eligible, "
          f"{summary['orders_would_place']} would place")
    print(f"  Total stake: ${total_stake:.2f}  ·  daily cap remaining: "
          f"${summary['remaining_daily_capacity']:.2f}")
    if skipped:
        print(f"  Skipped breakdown: {skipped}")
    if placed:
        print(f"\n── Would-place orders ──────────────────────────────────")
        for o in orders:
            if not o["would_place"]: continue
            p = o["pick"]
            print(f"  ${o['stake_dollars']:>5.2f} on {o['market_ticker']:35} "
                  f"({o['contracts']:>2} contracts @ {o['yes_ask_cents']}¢) "
                  f"· model {o['model_prob']*100:.0f}% vs ask {o['yes_ask_cents']}¢ "
                  f"= edge +{o['edge_pct']*100:.1f}%")
            print(f"        pick: {p.get('sport','?')} {p.get('pickLabel','?')}")

    print(f"\n✅ Saved: {out_path}")


if __name__ == "__main__":
    main()
