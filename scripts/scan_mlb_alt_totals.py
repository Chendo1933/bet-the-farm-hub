#!/usr/bin/env python3
"""
Scan today's Kalshi MLB alt-total ladders for +EV bets (live-wiring core).

For each MLB game today:
  • anchor = the sportsbook market total (from data/schedules/{date}.json)
  • fetch Kalshi's KXMLBTOTAL alt ladder, derive best YES(over)/NO(under) asks
    from the public orderbook (yes_ask = 1 - best_no_bid, no_ask = 1 - best_yes_bid)
  • price each alt line with AltTotalEngine (N(total, 3.8)) and surface the
    biggest edge = our P(side) − Kalshi ask

Market data is read from Kalshi's PUBLIC orderbook endpoint (no auth needed for
reads). This is the fetcher the paper/live alt-total track will reuse.

Usage:
  python3 scripts/scan_mlb_alt_totals.py [--date YYYY-MM-DD] [--min-edge 0.04]
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
import urllib.request
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent))
from alt_total_engine_mlb import AltTotalEngine

KALSHI = "https://api.elections.kalshi.com/trade-api/v2"

ABBR = {
    "Athletics": "ATH", "Arizona Diamondbacks": "AZ", "Atlanta Braves": "ATL",
    "Baltimore Orioles": "BAL", "Boston Red Sox": "BOS", "Chicago Cubs": "CHC",
    "Chicago White Sox": "CWS", "Cincinnati Reds": "CIN", "Cleveland Guardians": "CLE",
    "Colorado Rockies": "COL", "Detroit Tigers": "DET", "Houston Astros": "HOU",
    "Kansas City Royals": "KC", "Los Angeles Angels": "LAA", "Los Angeles Dodgers": "LAD",
    "Miami Marlins": "MIA", "Milwaukee Brewers": "MIL", "Minnesota Twins": "MIN",
    "New York Mets": "NYM", "New York Yankees": "NYY", "Philadelphia Phillies": "PHI",
    "Pittsburgh Pirates": "PIT", "San Diego Padres": "SD", "San Francisco Giants": "SF",
    "Seattle Mariners": "SEA", "St. Louis Cardinals": "STL", "Tampa Bay Rays": "TB",
    "Texas Rangers": "TEX", "Toronto Blue Jays": "TOR", "Washington Nationals": "WSH",
}


def _get(url: str, tries: int = 3):
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "btf-alttotal"})
            with urllib.request.urlopen(req, timeout=20) as r:
                return json.loads(r.read())
        except Exception:
            if i == tries - 1:
                return None
            time.sleep(0.5)
    return None


MIN_SIZE = 25   # contracts that must rest at the price we'd take (anti-phantom)


def _best_bid(levels, min_size):
    """Highest bid price with at least min_size contracts resting (ignores tiny
    stale orders that create phantom asks). Returns (price, size) or (None,None)."""
    best = None
    for p, q in levels:
        p = float(p); q = float(q)
        if q >= min_size and (best is None or p > best[0]):
            best = (p, q)
    return best if best else (None, None)


def best_asks(ticker: str, min_size: int = MIN_SIZE):
    """Return (yes_ask, no_ask) in prob units, using only price levels with real
    size. yes_ask = 1 - best_no_bid; no_ask = 1 - best_yes_bid."""
    d = _get(f"{KALSHI}/markets/{ticker}/orderbook")
    if not d:
        return (None, None)
    ob = d.get("orderbook_fp") or d.get("orderbook") or {}
    no_bid, _ = _best_bid(ob.get("no_dollars") or [], min_size)
    yes_bid, _ = _best_bid(ob.get("yes_dollars") or [], min_size)
    yes_ask = (1.0 - no_bid) if no_bid is not None else None
    no_ask = (1.0 - yes_bid) if yes_bid is not None else None
    return (yes_ask, no_ask)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--date", help="ET date YYYY-MM-DD (default today)")
    ap.add_argument("--min-edge", type=float, default=0.04,
                    help="Minimum (our_prob - ask) edge to surface (default 0.04)")
    args = ap.parse_args()
    res = find_value_picks(args.date, min_edge=args.min_edge)
    print(f"MLB alt-total scan · {res['date']} {res['now']:%H:%M} ET · "
          f"{res['pregame']} pregame ({res['started']} started, skipped) · "
          f"min edge {args.min_edge:+.0%}\n")
    cands = res["candidates"]
    if cands:
        print(f"{'edge':>5}  {'game':<34} {'mkt':>4}  bet (liquid, sane book)")
        for c in cands:
            print(f"  {100*c['edge']:>+3.0f}  {c['away'][:16]+' @ '+c['home'][:14]:<34} "
                  f"{c['market_total']:>4}  {c['side'].upper()} {c['line']} @ "
                  f"{100*c['price']:.0f}¢ (our {100*c['our_prob']:.0f}%)")
    else:
        print("No +EV alt-total bets clear the threshold today (after liquidity/sanity filters).")
    if res["skipped"]:
        print(f"\nSkipped {len(res['skipped'])} game(s) — book too thin/stale to trust:")
        for s in res["skipped"]:
            opx = f"{100*s['over_at_main']:.0f}¢" if s["over_at_main"] is not None else "no size"
            print(f"  · {s['away'][:16]} @ {s['home'][:14]} (mkt {s['market_total']}): over {s['near_line']} = {opx}")


def find_value_picks(date: str | None = None, min_edge: float = 0.05,
                     line_window: float = 3.5) -> dict:
    """Core reusable scan. Returns dict with date/now/pregame/started/candidates/
    skipped. candidates = [{away,home,market_total,line,side,price,our_prob,edge,
    ticker}], pregame-only, phantom/stale-book filtered. Used by the CLI here and
    by dry_run.py's paper-track generation."""
    now_et = datetime.now(ZoneInfo("America/New_York"))
    date = date or now_et.strftime("%Y-%m-%d")
    sched = Path(f"data/schedules/{date}.json")
    if not sched.exists():
        return {"date": date, "now": now_et, "pregame": 0, "started": 0,
                "candidates": [], "skipped": []}
    all_mlb = [g for g in json.loads(sched.read_text()).get("games", [])
               if (g.get("sport") or "").lower() == "mlb" and g.get("total") is not None]

    def _start_et(g):
        t = (g.get("time") or "").replace(" ET", "").strip()
        try:
            hm = datetime.strptime(t, "%I:%M %p")
            return now_et.replace(hour=hm.hour, minute=hm.minute, second=0, microsecond=0)
        except ValueError:
            return None
    games, started = [], 0
    for g in all_mlb:
        st = _start_et(g)
        if st is not None and st <= now_et:
            started += 1
            continue
        games.append(g)

    md = _get(f"{KALSHI}/markets?series_ticker=KXMLBTOTAL&status=open&limit=500")
    by_code: dict[str, list[tuple[int, str]]] = {}
    for m in (md or {}).get("markets", []):
        mt = re.match(r"KXMLBTOTAL-\d{2}[A-Z]{3}\d{2}\d{4}([A-Z]+)-(\d+)", m.get("ticker", ""))
        if mt:
            by_code.setdefault(mt.group(1), []).append((int(mt.group(2)), m["ticker"]))

    eng = AltTotalEngine()
    candidates, skipped = [], []
    for g in games:
        away, home, total = g["away"], g["home"], g["total"]
        ladder = by_code.get(ABBR.get(away, "?") + ABBR.get(home, "?"))
        if not ladder:
            continue
        priced, tickers = [], {}
        for idx, ticker in sorted(ladder):
            line = idx - 0.5
            if abs(line - total) > line_window:
                continue
            yes_ask, no_ask = best_asks(ticker)
            time.sleep(0.05)
            if yes_ask is None and no_ask is None:
                continue
            priced.append((line, yes_ask if yes_ask is not None else 1.0,
                           no_ask if no_ask is not None else 1.0))
            tickers[line] = ticker
        if not priced:
            continue
        near = min(priced, key=lambda x: abs(x[0] - total))
        if near[1] is None or not (0.35 <= near[1] <= 0.65):
            skipped.append({"away": away, "home": home, "market_total": total,
                            "near_line": near[0], "over_at_main": near[1]})
            continue
        best = eng.best_value_line(total, priced, min_edge=min_edge)
        if best:
            candidates.append({"away": away, "home": home, "market_total": total,
                               "line": best["line"], "side": best["side"],
                               "price": best["market_price"], "our_prob": best["our_prob"],
                               "edge": best["edge"], "ticker": tickers.get(best["line"])})
    candidates.sort(key=lambda c: c["edge"], reverse=True)
    return {"date": date, "now": now_et, "pregame": len(games), "started": started,
            "candidates": candidates, "skipped": skipped}


if __name__ == "__main__":
    main()
