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
from kalshi.pick_mapper import find_market_for_ml_pick, find_market_for_spread_pick
from kalshi.stake import kelly_stake_dollars, effective_caps


# ── F5 (First 5 Innings) paper-track helpers ──────────────────────────────
# These produce paper-only picks for KXMLBF5TOTAL markets using the same
# pitcher-tier signal as the live hub's full-game O/U scorer — but applied
# to F5 specifically, where starting pitchers are essentially the entire
# story (no bullpen, no late-inning chaos). Expected to outperform full-
# game O/U paper picks because the signal isn't diluted by bullpen variance.

PITCHER_DATA_PATH = "data/pitcher_data.json"

# Same park factors the hub uses (kept synced manually — see MLB_PARK_FACTORS
# in index.html). Only needed for F5 if we extend the model later; the
# current minimum implementation uses pitcher signal alone.
_F5_PARK_FACTORS = {
    'Baltimore Orioles':1.00,'Boston Red Sox':1.08,'New York Yankees':1.03,
    'Tampa Bay Rays':0.96,'Toronto Blue Jays':1.02,'Chicago White Sox':1.01,
    'Cleveland Guardians':0.95,'Detroit Tigers':0.97,'Kansas City Royals':0.98,
    'Minnesota Twins':1.03,'Houston Astros':0.97,'Los Angeles Angels':1.01,
    'Athletics':0.99,'Seattle Mariners':0.93,'Texas Rangers':1.05,
    'Atlanta Braves':1.01,'Miami Marlins':0.95,'New York Mets':0.97,
    'Philadelphia Phillies':1.02,'Washington Nationals':1.01,
    'Chicago Cubs':1.04,'Cincinnati Reds':1.05,'Milwaukee Brewers':0.97,
    'Pittsburgh Pirates':0.98,'St. Louis Cardinals':0.99,
    'Arizona Diamondbacks':1.02,'Colorado Rockies':1.28,'Los Angeles Dodgers':0.98,
    'San Diego Padres':0.93,'San Francisco Giants':0.92,
}


def _load_pitcher_cache() -> dict:
    """Load data/pitcher_data.json — produced by backfill_pitcher_data.py."""
    try:
        return json.loads(Path(PITCHER_DATA_PATH).read_text())
    except FileNotFoundError:
        return {"starters_by_gamePk": {}, "game_logs": {}}


def _cumulative_pitcher_stats(starts: list, before_date: str) -> dict | None:
    """Cumulative ERA + FIP from all of a pitcher's starts strictly before
    `before_date`. Returns None when there are zero prior starts."""
    prior = [s for s in (starts or []) if s.get("date") and s["date"] < before_date]
    if not prior:
        return None
    total_ip = sum(s.get("ip", 0) for s in prior)
    if total_ip < 1: return None
    total_er = sum(s.get("er", 0) for s in prior)
    total_bb = sum(s.get("bb", 0) for s in prior)
    total_k  = sum(s.get("k", 0) for s in prior)
    total_hr = sum(s.get("hr", 0) for s in prior)
    era = (9 * total_er) / total_ip
    # FIP = ((13*HR + 3*BB - 2*K) / IP) + ~3.10 constant
    fip = ((13*total_hr + 3*total_bb - 2*total_k) / total_ip) + 3.10
    return {"era": round(era,2), "fip": round(fip,2), "ip": round(total_ip,1),
            "starts": len(prior), "last_date": prior[-1]["date"]}


def _days_between(d1: str, d2: str) -> int | None:
    try:
        return (datetime.strptime(d2, "%Y-%m-%d") - datetime.strptime(d1, "%Y-%m-%d")).days
    except Exception:
        return None


def _recent_era(starts: list, before_date: str, n: int = 3) -> float | None:
    """Avg ERA over a pitcher's last n starts before the date (recent trend)."""
    prior = [s for s in (starts or []) if s.get("date") and s["date"] < before_date]
    if len(prior) < n: return None
    rec = prior[-n:]
    ip = sum(s.get("ip", 0) for s in rec)
    if ip < 1: return None
    return 9 * sum(s.get("er", 0) for s in rec) / ip


def _project_f5_total(h_stats, a_stats, h_log, a_log, home, date) -> float:
    """
    Projection-v2 F5 model (validated in scripts/backtest_f5.py — lifts F5
    from 47.8% binary-tier to 52.0% at a fair 4.5 line by adding rest +
    recent form + park):

      base = (home_FIP × 5/9) + (away_FIP × 5/9)   expected F5 runs
      × rest tilt    (≥6 days → 0.93, ≤4 days → 1.06)
      × recent-form tilt (trending bad → 1.05, trending good → 0.96)
      × park factor
    """
    use_fip = h_stats["starts"] >= 3 and a_stats["starts"] >= 3
    hv = h_stats["fip"] if use_fip else h_stats["era"]
    av = a_stats["fip"] if use_fip else a_stats["era"]
    proj = (hv * 5/9) + (av * 5/9)

    h_rest = _days_between(h_stats["last_date"], date)
    a_rest = _days_between(a_stats["last_date"], date)
    if h_rest is not None and a_rest is not None:
        avg_rest = (h_rest + a_rest) / 2
        if avg_rest >= 6:   proj *= 0.93
        elif avg_rest <= 4: proj *= 1.06

    hrf = _recent_era(h_log, date); arf = _recent_era(a_log, date)
    if hrf is not None and arf is not None:
        recent_avg = (hrf + arf) / 2
        season_avg = (h_stats["era"] + a_stats["era"]) / 2
        if recent_avg - season_avg >= 1.0:   proj *= 1.05
        elif season_avg - recent_avg >= 1.0: proj *= 0.96

    pf = _F5_PARK_FACTORS.get(home)
    if pf is not None:
        proj *= pf
    return proj


def _pitcher_tier(val: float) -> str:
    """Same tiering the hub uses (hbScoreOU pitcher branch)."""
    if val <= 3.25: return "elite"
    if val <= 4.00: return "quality"
    if val <= 4.75: return "average"
    return "weak"


def _pitcher_tier(val: float) -> str:
    """Same tiering the hub uses (hbScoreOU pitcher branch)."""
    if val <= 3.25: return "elite"
    if val <= 4.00: return "quality"
    if val <= 4.75: return "average"
    return "weak"


def _f5_market_summary_for_game(client, date_yymmdd: str, home_abbr: str, away_abbr: str) -> list[dict] | None:
    """
    Fetch all KXMLBF5TOTAL line markets for one game. Tickers look like
    KXMLBF5TOTAL-26MAY211605NYMWSH-{line} where {line} is the runs over/under.

    Returns list of {line, yes_ask_cents, no_ask_cents, ticker} sorted by line,
    or None if no markets found.
    """
    # Pull all open F5 total markets in one call, filter to this matchup.
    # The series ticker is global; per-game markets are paginated within it.
    try:
        resp = client.list_markets(series_ticker="KXMLBF5TOTAL", status="open", limit=200)
    except Exception:
        return None
    matchup_key = f"{date_yymmdd}{home_abbr}{away_abbr}"   # one order — Kalshi uses away+home, we try both
    alt_key     = f"{date_yymmdd}{away_abbr}{home_abbr}"
    candidates = []
    for m in resp.get("markets", []) or []:
        t = m.get("ticker", "")
        if matchup_key not in t and alt_key not in t:
            continue
        # Parse line number off the end (e.g. "-5")
        import re
        match = re.search(r"-(\d+)$", t)
        if not match: continue
        candidates.append({
            "ticker": t,
            "line": int(match.group(1)),
            "yes_ask_cents": _price_cents(m, "yes_ask"),
            "no_ask_cents":  _price_cents(m, "no_ask"),
        })
    if not candidates: return None
    candidates.sort(key=lambda c: c["line"])
    return candidates


def _price_cents(market: dict, field: str) -> int | None:
    """Read a price field as integer cents, handling both '_dollars' (string)
    and '_cents' (int) formats Kalshi uses across endpoints."""
    v = market.get(f"{field}_dollars")
    if v not in (None, ""):
        try: return round(float(v) * 100)
        except: pass
    v = market.get(field)
    if isinstance(v, (int, float)): return int(v)
    return None


# Compact 30-team MLB abbreviation map used to build Kalshi ticker keys.
# (Kalshi uses team-3-letter codes mashed together in the per-game ticker.)
_MLB_TEAM_ABBR = {
    "Arizona Diamondbacks": "AZ",  "Atlanta Braves": "ATL",
    "Baltimore Orioles": "BAL",    "Boston Red Sox": "BOS",
    "Chicago Cubs": "CHC",         "Chicago White Sox": "CWS",
    "Cincinnati Reds": "CIN",      "Cleveland Guardians": "CLE",
    "Colorado Rockies": "COL",     "Detroit Tigers": "DET",
    "Houston Astros": "HOU",       "Kansas City Royals": "KC",
    "Los Angeles Angels": "LAA",   "Los Angeles Dodgers": "LAD",
    "Miami Marlins": "MIA",        "Milwaukee Brewers": "MIL",
    "Minnesota Twins": "MIN",      "New York Mets": "NYM",
    "New York Yankees": "NYY",     "Athletics": "ATH",
    "Philadelphia Phillies": "PHI","Pittsburgh Pirates": "PIT",
    "San Diego Padres": "SD",      "San Francisco Giants": "SF",
    "Seattle Mariners": "SEA",     "St. Louis Cardinals": "STL",
    "Tampa Bay Rays": "TB",        "Texas Rangers": "TEX",
    "Toronto Blue Jays": "TOR",    "Washington Nationals": "WSH",
}


def _generate_f5_paper_orders(client, picks: list, date_key: str,
                              paper_min_score: int) -> list[dict]:
    """
    For each MLB game in today's picks with a usable pitcher matchup,
    generate a paper f5_ou order. Strategy:
      - Tier both starters by FIP (or ERA if FIP unavailable, n<3 starts).
      - Both aces (avg ≤3.25) → bet UNDER (most likely a pitchers' duel)
      - Both weak (avg ≥4.75) → bet OVER (lots of runs likely)
      - Ace vs liability → no signal, skip
    Pick the Kalshi F5 line nearest to 50¢ YES — that's where the book is
    least confident, which maximizes the room for our signal to add edge.
    """
    # We need pitcher data + Kalshi markets. Fail soft on either.
    pdata = _load_pitcher_cache()
    if not pdata.get("game_logs"):
        return []

    # Build (home, away) → (h_pid, a_pid) lookup from the starter cache by
    # matching on team names. Kalshi gamePk-keyed entries also have team
    # names embedded; we look them up to find each pitcher's ID.
    starters_by_teams = {}
    for pk, entry in pdata.get("starters_by_gamePk", {}).items():
        ht, at_ = entry.get("home_team"), entry.get("away_team")
        if ht and at_:
            starters_by_teams[(ht, at_)] = (entry.get("home_id"), entry.get("away_id"))

    # Deduplicate MLB games — picks file has multiple picks per game (ML, ATS,
    # O/U) — we only want one F5 paper order per game.
    seen_games = set()
    paper_orders = []
    mlb_picks = [p for p in picks if (p.get("sport") or "").upper() == "MLB"]
    if not mlb_picks: return []

    print(f"  F5 paper track: scanning {len(mlb_picks)} MLB picks across "
          f"{len(set((p.get('home'),p.get('away')) for p in mlb_picks))} game(s)")

    for pick in mlb_picks:
        home = pick.get("home"); away = pick.get("away")
        if not home or not away: continue
        if (home, away) in seen_games: continue
        seen_games.add((home, away))

        # Find this game's starters
        starter_ids = starters_by_teams.get((home, away))
        if not starter_ids or not all(starter_ids):
            continue
        h_pid, a_pid = starter_ids
        h_log = pdata.get("game_logs", {}).get(str(h_pid), [])
        a_log = pdata.get("game_logs", {}).get(str(a_pid), [])
        h_stats = _cumulative_pitcher_stats(h_log, date_key)
        a_stats = _cumulative_pitcher_stats(a_log, date_key)
        if not h_stats or not a_stats:
            continue

        use_fip = h_stats["starts"] >= 3 and a_stats["starts"] >= 3
        h_val = h_stats["fip"] if use_fip else h_stats["era"]
        a_val = a_stats["fip"] if use_fip else a_stats["era"]

        # Find Kalshi F5 markets for this game FIRST — the projection model
        # bets against the actual Kalshi line, not a fixed line.
        date_yymmdd = datetime.strptime(date_key, "%Y-%m-%d").strftime("%y%b%d").upper()
        h_abbr = _MLB_TEAM_ABBR.get(home); a_abbr = _MLB_TEAM_ABBR.get(away)
        if not h_abbr or not a_abbr:
            continue
        f5_markets = _f5_market_summary_for_game(client, date_yymmdd, h_abbr, a_abbr)
        if not f5_markets:
            continue
        # Pick the line closest to 50¢ YES (the book's true F5 estimate)
        def _closest_to_50(m):
            ya = m.get("yes_ask_cents") or 50
            return abs(ya - 50)
        target = min(f5_markets, key=_closest_to_50)
        ya = target.get("yes_ask_cents") or 0
        na = target.get("no_ask_cents") or 0
        if ya + na > 110:   # poor liquidity
            continue
        kalshi_line = target["line"] + 0.5   # "N+ runs" market = over N.5

        # ── Projection-v2 signal (rest + recent form + park) ──────────────
        # Validated in scripts/backtest_f5.py: lifts F5 from 47.8% (binary
        # tier) to 52.0% at a fair line. Bet over/under vs the Kalshi line
        # only when the projection clears it by ≥0.4 runs (noise floor).
        proj = _project_f5_total(h_stats, a_stats, h_log, a_log, home, date_key)
        edge = proj - kalshi_line
        if edge >= 0.4:
            side = "over"
        elif edge <= -0.4:
            side = "under"
        else:
            continue   # projection too close to the line — no edge
        # Confidence scales with how far the projection clears the line.
        confidence = min(0.72, 0.55 + abs(edge) * 0.08)
        if int(confidence * 100) < paper_min_score:
            continue

        synthetic_pick = {
            "sport": "MLB",
            "betType": "f5_ou",
            "home": home,
            "away": away,
            "total": kalshi_line,
            "pickedTeam": "Over" if side == "over" else "Under",
            "pickLabel": f"{'Over' if side == 'over' else 'Under'} {target['line']}.5 F5",
            "score100": int(confidence * 100),
            "tier": "paper",
            "date": date_key,
            "f5_meta": {
                "home_starter_id": h_pid,
                "away_starter_id": a_pid,
                "home_pitcher_val": h_val,
                "away_pitcher_val": a_val,
                "stat_used": "FIP" if use_fip else "ERA",
                "projected_f5": round(proj, 2),
                "kalshi_line": target["line"],
                "edge_runs": round(edge, 2),
                "kalshi_yes_ask_cents": ya,
                "kalshi_no_ask_cents": na,
                "kalshi_ticker": target["ticker"],
            },
        }
        paper_orders.append({
            "pick": synthetic_pick,
            "track": "paper",
            "model_prob": confidence,
            "would_grade": True,
        })

    if paper_orders:
        print(f"    → generated {len(paper_orders)} F5 paper picks")
    else:
        print(f"    → no F5 paper picks (no qualifying matchups today)")
    return paper_orders
# ── end F5 helpers ──────────────────────────────────────────────────────────

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


# Empirical calibration shrinkage (added 2026-05-17 from a 605-pick audit).
# Pre-shrinkage, the hub's score100 is consistently overconfident:
#
#   model says ~75%  →  actually wins 61%   (over by 14 pts)
#   model says ~65%  →  actually wins 56%   (over by  9 pts)
#   model says ~55%  →  actually wins 52%   (over by  3 pts)
#
# Pattern is roughly linear: over-confidence ≈ (raw - 50) × 0.4
# So calibrated_prob = 0.5 + (raw_prob - 0.5) × 0.6 brings the model
# in line with observed outcomes (a form of Platt scaling).
#
# Effect on Kelly sizing: bets get smaller across the board, especially
# on high-confidence picks. The min_calibrated_score eligibility gate
# still uses the raw score100 (so the volume of bets is unchanged) —
# we only shrink the probability that goes INTO Kelly's stake formula.
# Re-tune this constant if a future audit shows the bias has changed.
CALIBRATION_SHRINKAGE = 0.6


def _model_prob_from_pick(pick: dict) -> float | None:
    """Calibrated score / 100 → model's win probability, shrunk toward 50%.

    See CALIBRATION_SHRINKAGE comment above for the empirical basis.
    The shrinkage corrects for the model's systematic overconfidence —
    Kelly sizes were ~30% too aggressive on elite picks before this.
    """
    s = pick.get("score100")
    if s is None: return None
    raw = s / 100.0
    calibrated = 0.5 + (raw - 0.5) * CALIBRATION_SHRINKAGE
    return max(0.01, min(0.99, calibrated))


def _resolve_use_price(mapping: dict) -> tuple[int | None, str]:
    """
    Pick a price to simulate against, in priority order.
    Returns (price_in_cents, source_label) or (None, 'no_price').

    Priority:
      1. yes_ask           — current ask (what a market buy would cost)
      2. last_price        — most recent trade (good proxy on thin books)
      3. previous_yes_ask  — last tick's ask (stale but still informative)
      4. yes_bid + 2       — estimate from current bid (spread proxy)

    Demo environment frequently has only #2 or #4 available; live should
    usually have #1.
    """
    if (a := mapping.get("current_yes_ask_cents")) is not None:
        return a, "yes_ask"
    if (lp := mapping.get("last_price_cents")) is not None:
        return lp, "last_price"
    if (pa := mapping.get("previous_yes_ask_cents")) is not None:
        return pa, "previous_yes_ask"
    if (b := mapping.get("current_yes_bid_cents")) is not None and 0 < b < 99:
        return b + 2, "bid+2"
    return None, "no_price"


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

    # Resolve environment: KALSHI_ENVIRONMENT env var takes precedence over
    # config file. Lets you switch demo↔live just by re-exporting the var,
    # without editing kalshi_config.json.
    active_env = os.environ.get("KALSHI_ENVIRONMENT") or cfg.get("environment", "demo")
    print(f"Kalshi dry-run · {date_key} · environment={active_env}")
    print(f"  Reading {picks_path}")
    data = json.loads(picks_path.read_text())
    all_picks = data.get("picks", [])
    print(f"  {len(all_picks)} pick(s) in file")

    # Filter to supported bet types (ML only in Phase 2) AND score floor AND
    # not a live-excluded sport (e.g. NHL — no favorite edge, was -100% ROI).
    supported = set(cfg.get("supported_bet_types") or ["ml"])
    min_score = int(cfg.get("min_calibrated_score") or 0)
    excluded_sports = {s.upper() for s in (cfg.get("live_excluded_sports") or [])}
    def _live_ok(p):
        return (p.get("betType") in supported
                and (p.get("score100") or 0) >= min_score
                and (p.get("sport") or "").upper() not in excluded_sports)
    eligible = [p for p in all_picks if _live_ok(p)]
    n_excl = sum(1 for p in all_picks
                 if p.get("betType") in supported
                 and (p.get("score100") or 0) >= min_score
                 and (p.get("sport") or "").upper() in excluded_sports)
    extra = f" (− {n_excl} blocked: sports {sorted(excluded_sports)})" if n_excl else ""
    print(f"  {len(eligible)} eligible after score≥{min_score} + bet-type filter{extra}")

    # Map each eligible pick to a Kalshi market (live API call).
    client = KalshiClient(environment=active_env)

    # Auto-sync bankroll from live Kalshi balance. The hardcoded
    # bankroll_dollars in kalshi_config.json is used as a fallback only —
    # before this auto-sync, the dry-run was sizing Kelly stakes against
    # a stale $500 baseline that diverged from reality every time you
    # deposited or withdrew. The read-only API key has portfolio.read
    # permission so this should succeed in production; if the call fails
    # (rate limit, demo env, permission change) we fall back to the
    # config value so the rest of the dry-run still works.
    config_bankroll = float(cfg.get("bankroll_dollars") or 0)
    bankroll = config_bankroll
    bankroll_source = "config"
    positions_value = 0.0   # market value of currently-open contracts, in dollars
    try:
        bal = client.get_balance()
        live_bankroll = float(bal.get("balance", 0) or 0) / 100.0   # cents → dollars
        if live_bankroll > 0:
            bankroll = live_bankroll
            bankroll_source = "kalshi_live"
    except Exception as e:
        print(f"  ⚠ Could not fetch live balance ({type(e).__name__}: {e}) — falling back to config bankroll ${config_bankroll:.2f}")

    # Also fetch open positions so the morning summary can show Cash vs.
    # Open positions vs. Total. The Kalshi API exposes per-position
    # `market_exposure` (in cents) which is the contracts' current market
    # value — that's what we sum. Fall back to position * fill_avg if the
    # field isn't present in an older API response shape.
    try:
        pos_resp = client.get_positions(limit=200)
        for p in pos_resp.get("market_positions", []) or []:
            if (p.get("position") or 0) == 0:
                continue   # closed
            # Prefer market_exposure if present (in cents); else compute
            # from position * fees_paid-adjusted avg fill price. Both are
            # in cents — divide by 100 for dollars.
            exposure_cents = p.get("market_exposure")
            if exposure_cents is None:
                # Fallback: position count × resting market price.
                qty = abs(int(p.get("position", 0)))
                px  = p.get("last_market_price") or p.get("market_close_price") or 0
                exposure_cents = qty * int(px)
            positions_value += float(exposure_cents) / 100.0
    except Exception as e:
        print(f"  ⚠ Could not fetch positions ({type(e).__name__}: {e}) — positions_value stays $0")

    print(f"  Bankroll for Kelly sizing: ${bankroll:.2f} (source: {bankroll_source})")
    if positions_value > 0:
        print(f"  Open positions value: ${positions_value:.2f} · Total account: ${bankroll + positions_value:.2f}")

    # Shared events cache across all picks — list_events fires at most once
    # per sport instead of once per pick. Critical for live rate limits.
    events_cache: dict = {}
    orders = []
    skipped: dict = {}
    daily_exposure = 0.0
    # Bankroll-relative caps, computed off the live balance synced above.
    caps = effective_caps(cfg, bankroll)
    daily_cap     = caps["daily_dollars"]
    per_pick_cap  = caps["per_pick_dollars"]
    print(f"  Risk caps ({caps['source']}): per-pick ${per_pick_cap:.2f} · "
          f"daily ${daily_cap:.2f} · kill-switch ${caps['kill_switch_dollars']:.2f}")

    for pick in eligible:
        mapping = find_market_for_ml_pick(client, pick, events_cache=events_cache)
        # Resolve price using fallback chain (yes_ask → last_price → bid+2)
        # so a thin demo orderbook doesn't kill the simulation.
        use_price, price_source = _resolve_use_price(mapping)

        order = {
            "pick": pick,
            "market_ticker":  mapping.get("market_ticker"),
            "market_title":   mapping.get("market_title"),
            "yes_side":       mapping.get("yes_side"),
            # Snapshot all available prices for replay/audit
            "yes_ask_cents":      mapping.get("current_yes_ask_cents"),
            "yes_bid_cents":      mapping.get("current_yes_bid_cents"),
            "last_price_cents":   mapping.get("last_price_cents"),
            "previous_yes_ask_cents": mapping.get("previous_yes_ask_cents"),
            "volume_24h":         mapping.get("volume_24h"),
            # The price stake math actually used
            "use_price_cents": use_price,
            "use_price_source": price_source,
            "model_prob":     _model_prob_from_pick(pick),
            "would_place":    False,
            "skip_reason":    None,
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

        # Stake math uses the resolved use_price (with fallback chain) rather
        # than yes_ask alone, so thin orderbooks don't always block sizing.
        sized = kelly_stake_dollars(
            bankroll_dollars            = bankroll,   # auto-synced from live Kalshi balance above
            kelly_fraction              = float(cfg.get("kelly_fraction") or 0.25),
            model_prob                  = order["model_prob"],
            yes_ask_cents               = order["use_price_cents"],
            max_stake_dollars           = per_pick_cap,
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

    # ── PAPER TRACK ─────────────────────────────────────────────────────
    # Generate paper-only orders for bet types in paper_supported_bet_types
    # but NOT in supported_bet_types. Today this means O/U: we want to
    # validate the model against real outcomes without risking money.
    #
    # Paper orders don't need a Kalshi market lookup — we just need the
    # pick metadata. Reconcile.py grades them against pick_history.json
    # outcomes (computed by grade_picks.py) and writes paper-perf with
    # ONLY these orders. The double-counting between paper-perf and
    # live-perf that existed before this split is now gone.
    paper_supported = set(cfg.get("paper_supported_bet_types") or [])
    paper_only = paper_supported - supported   # don't double-count anything live also tracks
    # Paper picks use a separate (lower) score floor than live picks. Live
    # picks need cal ≥65 because real money is at stake; paper picks just
    # need to be above coin-flip (≥50). This lets us accumulate samples
    # fast for validation while keeping live picks selective.
    paper_min = int(cfg.get("paper_min_calibrated_score") or 50)
    paper_orders = []
    # ── Full-game O/U paper picks (from picks file) ─────────────────────
    # These come from the hub's hbScoreOU — bet type "ou". We grade them
    # against the full-game total in reconcile.
    if "ou" in paper_only:
        ou_eligible = [p for p in all_picks
                       if p.get("betType") == "ou"
                       and (p.get("score100") or 0) >= paper_min]
        for pick in ou_eligible:
            paper_orders.append({
                "pick": pick,
                "track": "paper",
                "model_prob": _model_prob_from_pick(pick),
                "would_grade": True,
            })
        print(f"  Paper O/U track: {len(ou_eligible)} candidates (full-game total)")

    # ── F5 (First 5 Innings) paper picks (synthesized here) ─────────────
    # The hub doesn't currently produce f5_ou picks — this Python module
    # generates them from the pitcher data we backfilled (data/pitcher_data.json)
    # and Kalshi's KXMLBF5TOTAL markets. Same paper-track plumbing as O/U
    # but graded against F5 total (innings 1-5 only) rather than final score.
    if "f5_ou" in paper_only:
        f5_orders = _generate_f5_paper_orders(client, all_picks, date_key, paper_min)
        paper_orders.extend(f5_orders)

    # ── Spread paper picks (NHL/NBA/NFL — proven +8.9% ROI historically) ──
    # The hub already produces spread picks. We map each to a Kalshi spread
    # market (KXNHLSPREAD/KXNBASPREAD/KXNFLSPREAD) and paper-track it before
    # going live. MLB spread picks are skipped — Kalshi only has F5 run line.
    if "spread" in paper_only:
        spread_eligible = [p for p in all_picks
                           if p.get("betType") == "spread"
                           and (p.get("score100") or 0) >= paper_min
                           and (p.get("sport") or "").upper() in ("NBA", "NHL", "NFL")]
        spread_made = 0
        for pick in spread_eligible:
            mapping = find_market_for_spread_pick(client, pick, events_cache=events_cache)
            if mapping.get("status") != "matched":
                continue
            paper_orders.append({
                "pick": pick,
                "track": "paper",
                "model_prob": _model_prob_from_pick(pick),
                "would_grade": True,
                "spread_meta": {
                    "market_ticker":      mapping.get("market_ticker"),
                    "yes_side":           mapping.get("yes_side"),
                    "spread_line_bet":    mapping.get("spread_line_bet"),
                    "spread_line_wanted": mapping.get("spread_line_wanted"),
                    "yes_ask_cents":      mapping.get("current_yes_ask_cents"),
                    "no_ask_cents":       mapping.get("no_ask_cents"),
                },
            })
            spread_made += 1
        print(f"  Spread paper track: {len(spread_eligible)} eligible → {spread_made} mapped to Kalshi markets")

    # ── MLB alt-total paper picks (market-anchored, Kalshi alt ladder) ──
    # Not from the hub picks file — synthesized from the AltTotalEngine against
    # Kalshi's KXMLBTOTAL alt ladder (pregame only, phantom/stale-book filtered).
    # See scripts/alt_total_engine_mlb.py + scan_mlb_alt_totals.py.
    if "alt_total" in paper_supported:
        try:
            sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
            from scan_mlb_alt_totals import find_value_picks
            min_edge = float(cfg.get("alt_total_min_edge") or 0.05)
            res = find_value_picks(date_key, min_edge=min_edge)
            for c in res["candidates"]:
                paper_orders.append({
                    "pick": {
                        "sport": "MLB", "betType": "alt_total",
                        "home": c["home"], "away": c["away"],
                        "line": c["line"], "side": c["side"],
                        "market_total": c["market_total"],
                        "pickLabel": f"{c['side'].upper()} {c['line']} ({c['away']} @ {c['home']} total)",
                    },
                    "track": "paper", "would_grade": True,
                    "alt_meta": {"kalshi_price": c["price"], "our_prob": c["our_prob"],
                                 "edge": c["edge"], "ticker": c.get("ticker")},
                })
            print(f"  Alt-total paper track: {res['pregame']} pregame games → "
                  f"{len(res['candidates'])} +EV candidate(s) (min edge {min_edge:+.0%})")
        except Exception as e:
            print(f"  ⚠ alt-total paper scan failed ({type(e).__name__}: {e})")

    summary = {
        "picks_total": len(all_picks),
        "picks_eligible_after_filter": len(eligible),
        "orders_would_place": placed,
        "total_stake_dollars": total_stake,
        "remaining_daily_capacity": round(max(0, daily_cap - total_stake), 2),
        "skipped_by_reason": skipped,
        # Paper track stats (separate from live)
        "paper_candidates": len(paper_orders),
        "paper_bet_types_tracked": sorted(paper_only),
        # Audit trail: which bankroll value drove Kelly sizing today.
        # If this drifts unexpectedly day-over-day, something's wrong upstream.
        "bankroll_used_dollars":   round(bankroll, 2),
        "bankroll_source":         bankroll_source,
        "open_positions_dollars":  round(positions_value, 2),
        "total_account_dollars":   round(bankroll + positions_value, 2),
    }
    out = {
        "date": date_key,
        "logged": datetime.now().isoformat(),
        "config_snapshot": _config_snapshot(cfg),
        "orders": orders,              # LIVE candidates (ML) → place_orders.py reads this
        "paper_orders": paper_orders,  # PAPER candidates (O/U) → reconcile.py paper track only
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
                  f"({o['contracts']:>2} contracts @ {o['use_price_cents']}¢ from {o['use_price_source']}) "
                  f"· model {o['model_prob']*100:.0f}% "
                  f"= edge +{o['edge_pct']*100:.1f}%")
            print(f"        pick: {p.get('sport','?')} {p.get('pickLabel','?')}")

    print(f"\n✅ Saved: {out_path}")


if __name__ == "__main__":
    main()
