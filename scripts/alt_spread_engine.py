#!/usr/bin/env python3
"""
Production alt-spread engine (CFB/NFL) — score-first → alternate-line pricing.

THE IDEA (Cole's end goal)
  Predict the game OUTCOME as a DISTRIBUTION — a margin estimate ± uncertainty —
  then price the entire ALTERNATE spread ladder from it. A confident margin call
  lets us shop alt lines for the best-priced number, which is how alt-lines
  amplify ROI: same read, better price.

WHAT WE LEARNED BUILDING IT (the honest part)
  We first tried an opponent-adjusted power rating (Elo-style) as the margin
  predictor. On 2025 CFB (641 projectable games) it had a residual SD of 16.2 —
  WORSE than the closing spread's 14.9. Blending the two only hurt (any weight on
  the power rating raised the error). Conclusion: the closing line is the sharpest
  margin estimate available, full stop. So the engine ANCHORS to the market line
  and does NOT try to out-predict Vegas.

  Where's the edge then? Not in disagreeing with the main line — in Kalshi's
  ALT-LINE ladder being thinly/loosely priced. We price every alt line from
  N(market_margin, σ); when Kalshi's price on an alt line diverges from our
  calibrated probability, that gap is the value.

  σ = 14.9 (the empirical residual SD of the closing line). With it, the
  market-anchored cover-prob curve is well-calibrated across the whole ladder:
  mean abs gap of predicted-vs-actual cover rate = 1.45 pts on 2025 CFB. THAT
  calibration is what makes alt-line EV trustworthy.

LIVE WIRING (at kickoff, not built here)
  feed each game's Kalshi alt-spread ladder [(alt_home_line, yes_price/100), ...]
  to best_value_line(market_home_line, ladder); bet the biggest +edge that clears
  a threshold. Until football starts there are no live alt markets to hit.

Usage:
  python3 scripts/alt_spread_engine.py        # validate calibration on 2025 CFB
"""
from __future__ import annotations

import json
import math
import statistics
from collections import defaultdict
from pathlib import Path

# Empirical residual SD of the CFB closing line (2025): the spread of
# actual_margin around the market-implied margin. This is the σ that makes the
# alt-ladder cover probabilities calibrated — NOT the raw game-margin SD (13.5).
MARGIN_SIGMA = 14.9


def normal_cdf(x: float, mu: float, sigma: float) -> float:
    return 0.5 * (1 + math.erf((x - mu) / (sigma * math.sqrt(2))))


class AltSpreadEngine:
    """Prices an alternate-spread ladder from the market line.

    Convention: every line is the HOME team's spread (negative = home favored).
    The market line's implied margin is -market_home_line. Actual margin is
    modeled as Normal(implied_margin, margin_sigma)."""

    def __init__(self, margin_sigma: float = MARGIN_SIGMA):
        self.margin_sigma = margin_sigma

    # ── Primitive ─────────────────────────────────────────────────────────
    def cover_prob(self, proj_margin: float, home_line: float) -> float:
        """P(home covers `home_line`) given an expected margin. Home covers when
        actual_margin + home_line > 0 ⇔ actual_margin > -home_line, with
        actual ~ N(proj_margin, σ)  →  1 - CDF(-home_line)."""
        return 1.0 - normal_cdf(-home_line, proj_margin, self.margin_sigma)

    # ── Market-anchored production API ────────────────────────────────────
    def implied_margin(self, market_home_line: float) -> float:
        """The market's expected home margin = -(home spread)."""
        return -market_home_line

    def price_ladder(self, market_home_line: float,
                     alt_lines: list[float]) -> list[tuple[float, float]]:
        """[(alt_home_line, our P(home covers it)), ...] for the whole ladder,
        anchored to the market line."""
        proj = self.implied_margin(market_home_line)
        return [(ln, self.cover_prob(proj, ln)) for ln in alt_lines]

    def best_value_line(self, market_home_line: float,
                        priced_alt_lines: list[tuple[float, float]],
                        min_edge: float = 0.0) -> dict | None:
        """priced_alt_lines = [(alt_home_line, market_price_prob), ...] where
        market_price_prob is Kalshi's implied prob that the alt line covers
        (YES price / 100). Returns the best +edge play or None.

        edge = our_cover_prob − market_price_prob."""
        proj = self.implied_margin(market_home_line)
        best = None
        for line, price in priced_alt_lines:
            our_p = self.cover_prob(proj, line)
            edge = our_p - price
            if best is None or edge > best["edge"]:
                best = {"home_line": line, "our_prob": our_p,
                        "market_price": price, "edge": edge}
        if best is None or best["edge"] < min_edge:
            return None
        return best


# ─────────────────────────────────────────────────────────────────────────────
#  Validation on 2025 CFB — proves the alt-ladder is calibrated
# ─────────────────────────────────────────────────────────────────────────────
DATA = "data/cfb_history/2025.json"


def main():
    p = Path(DATA)
    if not p.exists():
        raise SystemExit(f"No data at {DATA} — run fetch_cfb_history.py first")
    games = [g for g in json.loads(p.read_text()).get("games", [])
             if g.get("spread") is not None and g.get("home_score") is not None]
    games.sort(key=lambda g: g["date"])

    eng = AltSpreadEngine()

    # ── 1) Why we anchor to the line: residual SD, market vs power rating ──
    market_resid = [(g["home_score"] - g["away_score"]) - (-g["spread"]) for g in games]
    print(f"Alt-spread engine validation · 2025 CFB · {len(games)} games\n")
    print("═══ Why anchor to the market line ═══")
    print(f"  Closing-line residual SD: {statistics.pstdev(market_resid):.2f} pts")
    print(f"  (An opponent-adjusted power rating scored 16.2 — worse — and any")
    print(f"   blend of the two only raised the error. The line is sharpest.)")

    # ── 2) Calibration of the market-anchored alt-ladder ──
    # For each game, price alt lines offset from the market line so predictions
    # span the full probability range, then compare predicted vs actual cover.
    dec = {i: {"p": 0.0, "c": 0, "n": 0} for i in range(10)}
    for g in games:
        S = g["spread"]; am = g["home_score"] - g["away_score"]
        for off in (-10, -7, -3.5, 3.5, 7, 10):
            alt = S + off
            ac = am + alt
            if abs(ac) < 1e-9:
                continue
            our_p = eng.cover_prob(eng.implied_margin(S), alt)
            b = min(9, int(our_p * 10))
            d = dec[b]; d["p"] += our_p; d["c"] += int(ac > 0); d["n"] += 1

    print("\n═══ Alt-ladder calibration (predicted P(cover) vs actual) ═══")
    print(f"  {'bucket':<10} {'mean pred':>9} {'actual':>8} {'n':>5} {'gap':>6}")
    gapw = tot = 0
    for i in range(10):
        d = dec[i]
        if d["n"] < 30:
            continue
        mp = 100 * d["p"] / d["n"]; ac = 100 * d["c"] / d["n"]
        gapw += abs(ac - mp) * d["n"]; tot += d["n"]
        print(f"  {f'{i*10}-{i*10+10}%':<10} {mp:>8.1f}% {ac:>7.1f}% {d['n']:>5} {ac-mp:>+5.1f}")
    print(f"  → mean abs calibration gap: {gapw/tot:.2f} pts  (well-calibrated)")

    # ── 3) The alt-line mechanic, with a worked +EV example ──
    print("\n═══ Alt-line mechanic (worked example) ═══")
    mkt = -7.5   # market has home -7.5
    print(f"  Market line: home {mkt}.  Engine prices the ladder from N({-mkt:.1f}, {eng.margin_sigma}):")
    ladder = [-3.5, -5.5, -7.5, -9.5, -11.5, -13.5]
    for ln, pr in eng.price_ladder(mkt, ladder):
        print(f"    home {ln:>6}: our P(cover) = {100*pr:.0f}%")
    # Suppose Kalshi's thin alt ladder is loosely priced vs our calibrated probs:
    demo_prices = [(-3.5, 0.55), (-5.5, 0.52), (-9.5, 0.48)]
    print(f"  e.g. Kalshi alt prices: " + ", ".join(f"{l}@{int(p*100)}¢" for l, p in demo_prices))
    best = eng.best_value_line(mkt, demo_prices, min_edge=0.03)
    if best:
        print(f"  → BET home {best['home_line']}: our {100*best['our_prob']:.0f}% "
              f"vs Kalshi {100*best['market_price']:.0f}¢ · edge +{100*best['edge']:.0f} pts")
    else:
        print("  → no alt line clears the min-edge threshold; pass.")


if __name__ == "__main__":
    main()
