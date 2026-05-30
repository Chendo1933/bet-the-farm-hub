#!/usr/bin/env python3
"""
Promotion gate — does the candidate market clear paper validation?

GROUND ZERO RULE: the only way real money goes live again is if both:
  (1) Paper ROI >= 0% on >= MIN_N graded bets
  (2) Avg CLV   >= MIN_CLV on >= MIN_N graded bets

(Same bet population: only count bets that have BOTH outcomes AND closing-line
data; n = min of the two.)

Writes data/promotion_gate.json so the recap and the ops dashboard can show the
verdict at a glance. No alerts fire here — the recap surfaces it.

Markets tracked:
  paper_alt_total  (MLB alt-totals — the only active market post-Ground-Zero)

Usage:
  python3 scripts/promotion_gate.py
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

ALT_PERF = Path("data/kalshi_alt_total_perf.json")
CLV_PERF = Path("data/kalshi_clv_perf.json")
GATE_OUT = Path("data/promotion_gate.json")

MIN_N    = 20      # bets required before a verdict can be issued
MIN_CLV  = 1.0     # avg CLV % required to pass
MIN_ROI  = 0.0     # ROI % required to pass


def _load(p: Path) -> dict:
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text())
    except Exception:
        return {}


def evaluate() -> dict:
    alt = _load(ALT_PERF)
    clv = _load(CLV_PERF)

    graded = int(alt.get("graded") or 0)
    roi    = float(alt.get("roi_pct") or 0)

    paper_clv = ((clv.get("paper_alt_total") or {}).get("summary") or {})
    clv_n   = int(paper_clv.get("n") or 0)
    clv_avg = float(paper_clv.get("avg_clv_pct") or 0)

    # The effective sample is the intersection — we want both kinds of data on
    # the same bets to draw a conclusion.
    n = min(graded, clv_n)

    if n < MIN_N:
        status = "ACCUMULATING"
        reason = f"n={n}/{MIN_N} (graded {graded} · CLV {clv_n})"
    elif roi >= MIN_ROI and clv_avg >= MIN_CLV:
        status = "READY"
        reason = f"ROI {roi:+.1f}% (>={MIN_ROI}) · CLV {clv_avg:+.2f}% (>={MIN_CLV:+.1f}) · n={n}"
    else:
        # We have the sample but didn't clear one or both bars.
        fails = []
        if roi < MIN_ROI:     fails.append(f"ROI {roi:+.1f}% < {MIN_ROI:+.1f}")
        if clv_avg < MIN_CLV: fails.append(f"CLV {clv_avg:+.2f}% < {MIN_CLV:+.1f}")
        status = "FAILED"
        reason = "; ".join(fails) + f" · n={n}"

    return {
        "market": "paper_alt_total",
        "status": status,
        "reason": reason,
        "n_effective": n,
        "graded_outcomes": graded,
        "clv_n": clv_n,
        "roi_pct": roi,
        "avg_clv_pct": clv_avg,
        "thresholds": {"min_n": MIN_N, "min_roi_pct": MIN_ROI, "min_clv_pct": MIN_CLV},
    }


def main():
    gate = evaluate()
    out = {
        "updated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "markets": {gate["market"]: gate},
    }
    GATE_OUT.write_text(json.dumps(out, indent=2))
    icon = {"READY": "✅", "ACCUMULATING": "⏳", "FAILED": "🚫"}.get(gate["status"], "·")
    print(f"{icon} {gate['market']}: {gate['status']} — {gate['reason']}")
    print(f"  ✅ Wrote {GATE_OUT}")


if __name__ == "__main__":
    main()
