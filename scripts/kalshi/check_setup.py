#!/usr/bin/env python3
"""
Kalshi setup verification — run this after generating API keys to confirm:
  1. Auth credentials are configured (env vars or files)
  2. Private key parses as valid RSA
  3. We can reach Kalshi (demo or live based on KALSHI_ENVIRONMENT)
  4. The signed request authenticates and returns your account balance
  5. Sports markets are visible (what we'd map picks against)
  6. (Optional) Today's picks file mapping — pass --map-today to dry-test the
     pick→market mapper against data/picks/{ET-today}.json

Run:
  KALSHI_ENVIRONMENT=demo \\
  KALSHI_API_KEY_ID=<uuid> \\
  KALSHI_PRIVATE_KEY_PATH=~/.config/kalshi/demo.pem \\
  python scripts/kalshi/check_setup.py [--map-today]

Exit codes:
  0  all checks passed
  1  configuration / connectivity / mapping failure
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

# Allow running this script directly: `python scripts/kalshi/check_setup.py`
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from kalshi.client import KalshiClient, KalshiAPIError
from kalshi import auth as _auth
from kalshi.pick_mapper import map_picks, SPORT_TO_SERIES


def step(name: str):
    print(f"\n── {name} " + "─" * (60 - len(name)))


def check_env() -> bool:
    step("Step 1: environment variables")
    env = os.environ.get("KALSHI_ENVIRONMENT", "demo")
    key_id = os.environ.get("KALSHI_API_KEY_ID", "")
    pem = os.environ.get("KALSHI_PRIVATE_KEY", "")
    pem_path = os.environ.get("KALSHI_PRIVATE_KEY_PATH", "")
    print(f"  KALSHI_ENVIRONMENT      = {env}")
    print(f"  KALSHI_API_KEY_ID       = {'set (' + key_id[:8] + '…)' if key_id else 'NOT SET'}")
    print(f"  KALSHI_PRIVATE_KEY      = {'set (inline)' if pem else 'not set'}")
    print(f"  KALSHI_PRIVATE_KEY_PATH = {pem_path or 'not set'}")
    ok = bool(key_id) and (bool(pem) or bool(pem_path))
    print(f"  → {'✓ basic env config present' if ok else '✗ missing required env vars'}")
    return ok


def check_key_parse() -> bool:
    step("Step 2: private key parses as RSA")
    try:
        key = _auth.load_private_key()
        print(f"  ✓ loaded {key.key_size}-bit RSA private key")
        if key.key_size < 2048:
            print(f"  ⚠ key is {key.key_size}-bit — Kalshi recommends 2048+")
        return True
    except Exception as e:
        print(f"  ✗ {e}")
        return False


def check_balance(client: KalshiClient) -> bool:
    step("Step 3: signed request authenticates")
    try:
        bal = client.get_balance()
    except KalshiAPIError as e:
        print(f"  ✗ API error: {e}")
        if e.status == 401:
            print("    → 401 = signature/key mismatch. Verify KALSHI_API_KEY_ID corresponds to KALSHI_PRIVATE_KEY.")
        if e.status == 403:
            print("    → 403 = key lacks permission. In Kalshi settings, ensure key has 'Read' (and later 'Trade').")
        return False
    except Exception as e:
        print(f"  ✗ unexpected: {e}")
        return False
    cents = bal.get("balance")
    if cents is None:
        print(f"  ⚠ balance response had no 'balance' field: {bal}")
        return True
    print(f"  ✓ account balance: ${cents/100:,.2f} ({bal})")
    if client.environment == "demo" and cents == 0:
        print("    → demo account at $0 — go to demo-app.kalshi.com and click 'Reset Balance' to refill")
    return True


def check_sports_markets(client: KalshiClient) -> bool:
    step("Step 4: sports market visibility")
    found_any = False
    for sport, series in SPORT_TO_SERIES.items():
        try:
            resp = client.list_events(status="open", series_ticker=series, limit=5)
        except KalshiAPIError as e:
            print(f"  {sport:>3} ({series}): ✗ {e.status} — series may have changed")
            continue
        events = resp.get("events", [])
        if events:
            found_any = True
            sample = events[0]
            title = sample.get("title", "?")
            ticker = sample.get("ticker", sample.get("event_ticker","?"))
            print(f"  {sport:>3} ({series}): {len(events)} open event(s) · sample: {ticker} '{title[:50]}'")
        else:
            print(f"  {sport:>3} ({series}): 0 open events (off-season or no markets right now)")
    if not found_any:
        print("  ⚠ no sports markets found across any series — check if Kalshi has changed series tickers")
        return False
    return True


def check_pick_mapping(client: KalshiClient) -> bool:
    step("Step 5: pick→market mapping against today's slate")
    et_today = datetime.now(ZoneInfo("America/New_York")).strftime("%Y-%m-%d")
    picks_path = Path(f"data/picks/{et_today}.json")
    if not picks_path.exists():
        print(f"  · no picks file for {et_today} — skipping (run after morning logger)")
        return True
    try:
        data = json.loads(picks_path.read_text())
    except Exception as e:
        print(f"  ✗ couldn't parse {picks_path}: {e}")
        return False
    ml_picks = [p for p in data.get("picks", []) if p.get("betType") == "ml"]
    print(f"  Found {len(ml_picks)} ML pick(s) in {picks_path}")
    if not ml_picks:
        return True
    results = map_picks(client, ml_picks)
    summary = {"matched": 0, "ambiguous": 0, "no_event": 0, "no_market": 0, "unsupported": 0}
    for r in results:
        summary[r["status"]] = summary.get(r["status"], 0) + 1
        p = r["pick"]
        line = f"  [{r['status']:11}] {p.get('sport',''):>3} {p.get('pickLabel',''):<35}"
        if r["status"] == "matched":
            line += f" → {r['market_ticker']} (yes side: {r['yes_side']}, ask: {r.get('current_yes_ask_cents')}¢)"
        elif r.get("reason"):
            line += f" — {r['reason']}"
        print(line)
        # When ambiguous, print the competing candidates so we can see what's
        # being conflated (real doubleheaders vs futures/alt-line markets vs
        # season-long props with same teams).
        if r["status"] == "ambiguous":
            for c in r.get("candidates", [])[:5]:
                print(f"      · {c.get('ticker','?'):40} '{c.get('title','?')[:60]}'")
    print(f"\n  Summary: {summary}")
    return summary["matched"] > 0 or len(ml_picks) == 0


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--map-today", action="store_true",
                    help="Also test pick→market mapping against today's picks file.")
    args = ap.parse_args()

    print("Kalshi setup verification")
    print(f"Environment: {os.environ.get('KALSHI_ENVIRONMENT', 'demo')}")

    if not check_env():
        print("\n→ See scripts/kalshi/README.md for setup steps")
        sys.exit(1)
    if not check_key_parse():
        sys.exit(1)
    client = KalshiClient()
    if not check_balance(client):
        sys.exit(1)
    if not check_sports_markets(client):
        sys.exit(1)
    if args.map_today:
        if not check_pick_mapping(client):
            sys.exit(1)

    print("\n✅ All Phase 1 checks passed. Ready for Phase 2 (dry-run order simulation).")


if __name__ == "__main__":
    main()
