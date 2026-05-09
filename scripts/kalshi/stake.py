"""
Stake sizing for Kalshi orders — fractional Kelly with config-driven caps.

Kalshi binary contract math:
  Buy YES at price `a` cents (0 < a < 100). Contract pays $1.00 if YES
  resolves true, $0.00 otherwise.
  Decimal odds for the YES side: 1 / (a/100) = 100/a
  For Kelly, b (net odds) = decimal_odds - 1 = (100-a)/a

Kelly formula for binary outcome:
  f* = (b·p - q) / b
where:
  p = our model's probability of YES resolving true
  q = 1 - p
  b = net decimal odds

f* < 0  → no edge (don't bet)
f* = 0.10 → bet 10% of bankroll for max-growth (full Kelly)
We multiply by `kelly_fraction` (default 0.25) for variance control.

The result is then floored at 0 and capped at `max_stake_per_pick_dollars`.
Returns 0 dollars when:
  - ask price is unavailable / out of (0, 100) range
  - model probability ≤ ask-implied probability (no edge)
  - YES ask is above `skip_if_yes_ask_above_cents` (heavy fav, poor RoR)
"""
from __future__ import annotations


def kelly_stake_dollars(
    *,
    bankroll_dollars: float,
    kelly_fraction: float,
    model_prob: float,
    yes_ask_cents: int | None,
    max_stake_dollars: float,
    skip_if_yes_ask_above_cents: int | None = None,
) -> dict:
    """
    Compute fractional-Kelly stake in dollars for a single pick.

    Returns dict:
      {
        "stake_dollars": float,   # 0 if don't-bet conditions hit
        "contracts": int,         # whole contracts at this price (floor)
        "edge_pct": float | None, # (model_prob - implied_prob); None if no ask
        "kelly_fraction_used": float,
        "skip_reason": str | None # 'no_ask' / 'price_out_of_range' /
                                  #  'no_edge' / 'ask_too_high' / 'no_bankroll'
      }
    """
    if bankroll_dollars <= 0:
        return _result(0, 0, None, kelly_fraction, "no_bankroll")
    if yes_ask_cents is None:
        return _result(0, 0, None, kelly_fraction, "no_ask")
    if yes_ask_cents <= 0 or yes_ask_cents >= 100:
        return _result(0, 0, None, kelly_fraction, "price_out_of_range")
    if skip_if_yes_ask_above_cents is not None and yes_ask_cents >= skip_if_yes_ask_above_cents:
        return _result(0, 0, None, kelly_fraction, "ask_too_high")

    a = yes_ask_cents / 100.0       # price as probability (0–1)
    implied_prob = a
    edge_pct = model_prob - implied_prob

    if edge_pct <= 0:
        return _result(0, 0, edge_pct, kelly_fraction, "no_edge")

    # Kelly: f* = (b·p - q) / b
    b = (1 - a) / a
    p = model_prob
    q = 1 - p
    f_star = (b * p - q) / b

    if f_star <= 0:
        # Defensive — should be caught by edge_pct check above, but float math
        return _result(0, 0, edge_pct, kelly_fraction, "no_edge")

    raw_stake = bankroll_dollars * kelly_fraction * f_star
    stake = min(raw_stake, max_stake_dollars)
    # Whole-contract integer count at YES price `a` per contract
    contracts = int(stake / a) if a > 0 else 0
    actual_stake = contracts * a   # rebound to actual whole-contract spend
    return _result(round(actual_stake, 2), contracts, edge_pct, kelly_fraction, None)


def _result(stake, contracts, edge_pct, kf, skip_reason):
    return {
        "stake_dollars": stake,
        "contracts": contracts,
        "edge_pct": round(edge_pct, 4) if edge_pct is not None else None,
        "kelly_fraction_used": kf,
        "skip_reason": skip_reason,
    }
