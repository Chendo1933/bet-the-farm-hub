#!/usr/bin/env python3
"""
Closing-Line Value (CLV) tracker for our live MLB ML bets.

WHY CLV
  Win/loss is hopelessly noisy at small samples (our live record is ~12 bets).
  CLV measures whether the market moved TOWARD our pick after we bet — i.e. did
  we get a better number than the closing line. The closing line is the sharpest
  price in the market, so beating it is the most reliable early indicator of a
  real edge, long before W/L stabilizes. Positive average CLV ⇒ the model is
  finding genuine value; flat/negative CLV ⇒ we're just along for the variance.

METHOD (de-vigged sportsbook lines, apples-to-apples)
  For each live MLB ML bet:
    entry  = de-vigged implied P(our team) from the MORNING odds snapshot
             (≈ when our chain places, ~9am ET)
    close  = de-vigged implied P(our team) from the PREGAME snapshot
             (≈ game time — the closing line)
    CLV    = close − entry   (in probability points)
  Positive CLV = the line moved our way after we bet (we beat the close).

  Snapshots: data/odds_snapshots/{date}-{morning,pregame}.json (both-side
  moneylines per game → proper de-vig). Pregame coverage began 2026-05-21, so
  only bets on/after that are gradeable; it accrues every day going forward.

Usage:
  python3 scripts/clv_tracker.py            # report + write data/kalshi_clv_perf.json
  python3 scripts/clv_tracker.py --quiet    # write only
"""
from __future__ import annotations

import argparse
import glob
import json
from pathlib import Path

ORDERS_DIR = "data/kalshi_orders"
SNAP_DIR = "data/odds_snapshots"
CLV_PERF_OUT = Path("data/kalshi_clv_perf.json")


def _norm(n: str) -> str:
    return {"Oakland Athletics": "Athletics", "Sacramento Athletics": "Athletics"}.get(n, n)


def am_to_prob(ml: float) -> float:
    return (-ml) / ((-ml) + 100) if ml < 0 else 100 / (ml + 100)


def _load_snap(date: str, label: str) -> dict | None:
    p = Path(f"{SNAP_DIR}/{date}-{label}.json")
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text())
    except Exception:
        return None


def _game_ml(snap: dict | None, home: str, away: str):
    """Return (ml_home, ml_away) for the game, or None."""
    if not snap:
        return None
    for g in snap.get("games", {}).get("mlb", []):
        if _norm(g.get("home", "")) == home and _norm(g.get("away", "")) == away:
            if g.get("ml_home") is not None and g.get("ml_away") is not None:
                return (g["ml_home"], g["ml_away"])
    return None


def _novig_team(ml_home: float, ml_away: float, team_is_home: bool) -> float | None:
    ih = am_to_prob(ml_home); ia = am_to_prob(ml_away)
    vig = ih + ia
    if vig <= 0:
        return None
    return (ih / vig) if team_is_home else (ia / vig)


def collect_clv() -> list[dict]:
    rows = []
    for f in sorted(glob.glob(f"{ORDERS_DIR}/*.json")):
        d = json.loads(Path(f).read_text())
        date = d.get("date")
        for o in d.get("placed_orders", []):
            if o.get("test"):
                continue
            p = o.get("dryrun_pick", {})
            if (p.get("sport") or "").upper() != "MLB":
                continue   # snapshots are MLB-only; NHL ML not gradeable here
            team = _norm(p.get("pickedTeam") or "")
            home = _norm(p.get("home") or ""); away = _norm(p.get("away") or "")
            if not team or not home or not away:
                continue
            em = _game_ml(_load_snap(date, "morning"), home, away)
            cm = _game_ml(_load_snap(date, "pregame"), home, away)
            if not em or not cm:
                continue
            team_is_home = (team == home)
            entry = _novig_team(*em, team_is_home)
            close = _novig_team(*cm, team_is_home)
            if entry is None or close is None:
                continue
            rows.append({
                "date": date, "team": team,
                "entry_prob": round(entry, 4), "close_prob": round(close, 4),
                "clv": round(close - entry, 4),
                "beat_close": close > entry,
                "outcome": o.get("outcome"),
            })
    return rows


def summarize(rows: list[dict]) -> dict:
    n = len(rows)
    if n == 0:
        return {"n": 0}
    avg = sum(r["clv"] for r in rows) / n
    beat = sum(1 for r in rows if r["beat_close"])
    # CLV among wins vs losses — a calibration sanity check on whether CLV tracks results
    graded = [r for r in rows if r["outcome"] in ("win", "loss")]
    return {
        "n": n,
        "avg_clv_pct": round(100 * avg, 2),
        "beat_close_pct": round(100 * beat / n, 1),
        "beat_close": beat,
        "graded": len(graded),
    }


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--quiet", action="store_true", help="write perf file only")
    args = ap.parse_args()

    rows = collect_clv()
    summary = summarize(rows)
    CLV_PERF_OUT.write_text(json.dumps({"summary": summary, "bets": rows}, indent=2))

    if args.quiet:
        return

    print("Closing-Line Value (CLV) · live MLB ML bets\n")
    if not rows:
        print("  No gradeable bets yet (need morning+pregame snapshots; pregame "
              "coverage began 2026-05-21).")
        return
    print(f"  {'date':<11} {'team':<16} {'entry':>6} {'close':>6} {'CLV':>7} {'result':>7}")
    for r in rows:
        arrow = "↑" if r["clv"] > 0 else ("↓" if r["clv"] < 0 else "·")
        print(f"  {r['date']:<11} {r['team'][:16]:<16} {100*r['entry_prob']:>5.1f}% "
              f"{100*r['close_prob']:>5.1f}% {100*r['clv']:>+5.1f} {arrow}  "
              f"{(r['outcome'] or 'pending'):>7}")
    print()
    print(f"  Bets: {summary['n']} · beat the close: {summary['beat_close']}/{summary['n']} "
          f"({summary['beat_close_pct']:.0f}%) · avg CLV: {summary['avg_clv_pct']:+.2f}%")
    print(f"  ✅ Wrote {CLV_PERF_OUT}")
    print("\n  Read: avg CLV > 0 (and beat-close > 50%) = the market consistently")
    print("  moves toward our picks → real edge, even before W/L confirms it.")


if __name__ == "__main__":
    main()
