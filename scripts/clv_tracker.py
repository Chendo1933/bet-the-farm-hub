#!/usr/bin/env python3
"""
Closing-Line Value (CLV) tracker — MLB Ground Zero edition.

CLV is the sharpest, least-noisy edge signal — meaningful at small samples where
W/L is pure variance. Under Ground Zero (one market: MLB alt-totals, paper-only
until proven), CLV on paper alt-total candidates IS the promotion gate's input.

  PROMOTION GATE: paper alt-total ROI >= 0% AND avg CLV >= +1% over >= 20 bets.

TWO MARKETS GRADED HERE
  paper_alt_total  (PRIMARY — the only active market post-Ground-Zero)
    "Shadow CLV": re-price each paper candidate at the CLOSING market total
    using the same calibrated AltTotalEngine that priced it at entry. If the
    closing total moved TOWARD the side we papered, +CLV (our edge was real;
    we caught the market early). If it moved AWAY, -CLV.

    entry_p = engine.<side>_prob(morning_market_total, alt_line)
    close_p = engine.<side>_prob(close_market_total,   alt_line)
    CLV     = close_p - entry_p

  live_ml          (LEGACY — kept for the historical record; nothing new added)
    De-vigged sportsbook morning vs pregame moneyline for the bet's side. Live
    ML is dropped under Ground Zero, so this stops accumulating.

DATA SOURCES
  data/odds_snapshots/{date}-morning.json  (entry total)
  data/odds_snapshots/{date}-pregame.json  (close total + closing moneylines)
  data/kalshi_dryrun/{date}.json           (paper alt-total candidates)
  data/kalshi_orders/{date}.json           (legacy live ML receipts)

OUTPUT
  data/kalshi_clv_perf.json:
    {
      "updated":   "...",
      "paper_alt_total": { "summary": {...}, "bets": [...] },
      "live_ml":         { "summary": {...}, "bets": [...] }
    }

Usage:
  python3 scripts/clv_tracker.py            # write + print
  python3 scripts/clv_tracker.py --quiet    # write only
"""
from __future__ import annotations

import argparse
import glob
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from alt_total_engine_mlb import AltTotalEngine

CLV_PERF_OUT = Path("data/kalshi_clv_perf.json")
ORDERS_DIR   = "data/kalshi_orders"      # legacy live ML
DRYRUN_DIR   = "data/kalshi_dryrun"      # paper alt-total
SNAP_DIR     = "data/odds_snapshots"


def _norm(n: str) -> str:
    return {"Oakland Athletics": "Athletics", "Sacramento Athletics": "Athletics"}.get(n, n)


def _am_to_prob(ml: float) -> float:
    return (-ml) / ((-ml) + 100) if ml < 0 else 100 / (ml + 100)


def _load_snap(date: str, label: str) -> dict | None:
    p = Path(f"{SNAP_DIR}/{date}-{label}.json")
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text())
    except Exception:
        return None


def _find_game(snap: dict | None, home: str, away: str) -> dict | None:
    """Return the LATEST entry for this game in the snapshot. The midday snapshot
    accumulates multiple sweeps, so a game can appear twice with different totals
    as the line moves; the per-entry `snapshot_time` orders them. Returning the
    latest is what we want for 'closing total'."""
    if not snap:
        return None
    best = None
    best_t = ""
    for g in snap.get("games", {}).get("mlb", []):
        if _norm(g.get("home", "")) == home and _norm(g.get("away", "")) == away:
            st = g.get("snapshot_time") or ""
            if best is None or st > best_t:
                best = g; best_t = st
    return best


# ── PAPER ALT-TOTAL (PRIMARY under Ground Zero) ───────────────────────────────
def _latest_close_total(date: str, home: str, away: str) -> tuple[float | None, str | None]:
    """Return (close_total, source_label). Prefer the LATEST snapshot that
    actually contains the game — pregame, then midday, then morning. The
    pregame sweep finishes ~7pm ET so it can miss late west-coast games
    (the Odds API window varies); without this fallback those games get
    silently dropped from CLV. Returns (None, None) if no snapshot has it."""
    for label in ("pregame", "midday", "morning"):
        snap = _load_snap(date, label)
        g = _find_game(snap, home, away)
        if g and g.get("total") is not None:
            return (g["total"], label)
    return (None, None)


def collect_paper_alt_total() -> list[dict]:
    """Shadow CLV: re-price each paper alt-total bet at the closing market total
    and compare to entry. Uses data already on disk — no new fetches needed."""
    eng = AltTotalEngine()
    rows: list[dict] = []
    for f in sorted(glob.glob(f"{DRYRUN_DIR}/*.json")):
        d = json.loads(Path(f).read_text())
        date = d.get("date") or Path(f).stem
        for o in d.get("paper_orders", []) or []:
            p = o.get("pick") or {}
            if p.get("betType") != "alt_total":
                continue
            entry_total = p.get("market_total")
            line = p.get("line"); side = (p.get("side") or "").lower()
            home = _norm(p.get("home", "")); away = _norm(p.get("away", ""))
            if entry_total is None or line is None or side not in ("over", "under"):
                continue
            close_total, src = _latest_close_total(date, home, away)
            if close_total is None:
                continue   # no snapshot has the game — truly ungradeable
            prob = eng.over_prob if side == "over" else eng.under_prob
            entry_p = prob(entry_total, line)
            close_p = prob(close_total, line)
            rows.append({
                "date": date, "away": away, "home": home,
                "line": line, "side": side,
                "entry_total": entry_total, "close_total": close_total,
                "close_source": src,
                "entry_prob": round(entry_p, 4), "close_prob": round(close_p, 4),
                "clv": round(close_p - entry_p, 4),
                "beat_close": close_p > entry_p,
            })
    return rows


# ── LIVE ML (LEGACY — no new data once auto-trading is off) ──────────────────
def _ml_for_team(g: dict | None, team_is_home: bool):
    if not g:
        return None
    ih = _am_to_prob(g.get("ml_home")) if g.get("ml_home") is not None else None
    ia = _am_to_prob(g.get("ml_away")) if g.get("ml_away") is not None else None
    if ih is None or ia is None:
        return None
    vig = ih + ia
    return (ih / vig) if team_is_home else (ia / vig)


def collect_live_ml() -> list[dict]:
    rows: list[dict] = []
    for f in sorted(glob.glob(f"{ORDERS_DIR}/*.json")):
        d = json.loads(Path(f).read_text())
        date = d.get("date")
        for o in d.get("placed_orders", []):
            if o.get("test"):
                continue
            p = o.get("dryrun_pick", {})
            if (p.get("sport") or "").upper() != "MLB":
                continue
            team = _norm(p.get("pickedTeam") or "")
            home = _norm(p.get("home") or ""); away = _norm(p.get("away") or "")
            if not team:
                continue
            mg = _find_game(_load_snap(date, "morning"), home, away)
            cg = _find_game(_load_snap(date, "pregame"), home, away)
            team_is_home = (team == home)
            entry = _ml_for_team(mg, team_is_home)
            close = _ml_for_team(cg, team_is_home)
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


# ── Aggregation + I/O ────────────────────────────────────────────────────────
def _summarize(rows: list[dict]) -> dict:
    n = len(rows)
    if n == 0:
        return {"n": 0}
    beat = sum(1 for r in rows if r["beat_close"])
    return {
        "n": n,
        "avg_clv_pct": round(100 * sum(r["clv"] for r in rows) / n, 2),
        "beat_close": beat,
        "beat_close_pct": round(100 * beat / n, 1),
    }


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()

    paper = collect_paper_alt_total()
    live = collect_live_ml()
    out = {
        "updated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "paper_alt_total": {"summary": _summarize(paper), "bets": paper},
        "live_ml":         {"summary": _summarize(live),  "bets": live},
    }
    CLV_PERF_OUT.write_text(json.dumps(out, indent=2))

    if args.quiet:
        return

    print("Closing-Line Value · MLB Ground Zero\n")
    ps, ls = out["paper_alt_total"]["summary"], out["live_ml"]["summary"]

    print("═══ PAPER alt-total (the active market) ═══")
    if ps["n"]:
        print(f"  n={ps['n']} · avg CLV {ps['avg_clv_pct']:+.2f}% · "
              f"beat close {ps['beat_close']}/{ps['n']} ({ps['beat_close_pct']:.0f}%)")
        for r in paper[-8:]:
            arrow = "↑" if r["clv"] > 0 else ("↓" if r["clv"] < 0 else "·")
            print(f"  {r['date']}  {r['side'].upper()} {r['line']:>5} "
                  f"{r['away'][:14]+'@'+r['home'][:14]:<30}  "
                  f"entry {100*r['entry_prob']:>5.1f}% close {100*r['close_prob']:>5.1f}%  "
                  f"{100*r['clv']:>+5.1f} {arrow}")
    else:
        print("  no graded bets yet — waiting for paper candidates + pregame snapshot")

    print("\n═══ LIVE ML (legacy — no new data) ═══")
    if ls["n"]:
        print(f"  n={ls['n']} · avg CLV {ls['avg_clv_pct']:+.2f}% · "
              f"beat close {ls['beat_close']}/{ls['n']}")
    else:
        print("  no graded live bets")

    print(f"\n  ✅ Wrote {CLV_PERF_OUT}")
    print("  Read: avg CLV > 0 → market moves toward our picks → real edge.")
    print("  Promotion gate: paper alt-total ROI >= 0% AND avg CLV >= +1% over >= 20 bets.")


if __name__ == "__main__":
    main()
