#!/usr/bin/env python3
"""
BTF morning summary — fires once per day via the webhook.

Sends a single push notification with:
  - Yesterday's reconciled PnL (combined paper + live)
  - Today's planned auto-bets (count + total stake)
  - 7-day rolling performance
  - Bet-sizing stage recommendation (advisory only — never auto-changes config)

Design intent:
  This script is read-only on data and write-only on the webhook. It
  never modifies kalshi_config.json or any placed orders. The point is
  to give the operator passive visibility — open the phone, see the
  morning summary, decide whether to bump stake caps. If the script
  itself fails, the workflow's notify-failure step still fires (we
  re-use the existing webhook channel).

Usage:
  python3 scripts/kalshi/daily_summary.py            # today's summary
  python3 scripts/kalshi/daily_summary.py --dry      # print to stdout only
  python3 scripts/kalshi/daily_summary.py --date YYYY-MM-DD

Env:
  WEBHOOK_URL  — same secret used by .github/actions/notify-failure.
                 Auto-detected: ntfy.sh vs Discord/Slack JSON.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

CONFIG_PATH        = "data/kalshi_config.json"
DRYRUN_DIR         = "data/kalshi_dryrun"
ORDERS_DIR         = "data/kalshi_orders"
PAPER_PERF_PATH    = "data/kalshi_dryrun_perf.json"
LIVE_PERF_PATH     = "data/kalshi_live_perf.json"


def _today_et_date() -> str:
    return datetime.now(ZoneInfo("America/New_York")).strftime("%Y-%m-%d")


def _load_perf(path: str) -> dict | None:
    p = Path(path)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text())
    except Exception:
        return None


def _read_today_plan(date_key: str) -> dict:
    """Pull today's auto-bet plan from the dry-run snapshot."""
    p = Path(DRYRUN_DIR) / f"{date_key}.json"
    if not p.exists():
        return {"orders_would_place": 0, "total_stake_dollars": 0.0, "picks_total": 0}
    data = json.loads(p.read_text())
    summary = data.get("summary", {})
    return {
        "orders_would_place":     summary.get("orders_would_place", 0),
        "total_stake_dollars":    summary.get("total_stake_dollars", 0.0),
        "picks_total":            summary.get("picks_total", 0),
        # Live account snapshot (added 2026-05-17). Older snapshots without
        # these fields fall back to showing just bankroll alone.
        "bankroll_used_dollars":  summary.get("bankroll_used_dollars"),
        "open_positions_dollars": summary.get("open_positions_dollars"),
        "total_account_dollars":  summary.get("total_account_dollars"),
    }


def _rolling_pnl(perf: dict | None, days: int = 7) -> dict:
    """Sum the last `days` daily entries from a perf object."""
    if not perf or not perf.get("daily"):
        return {"placed": 0, "wins": 0, "losses": 0, "stake": 0.0, "pnl": 0.0, "days": 0}
    recent = perf["daily"][-days:]
    return {
        "placed":   sum(d.get("placed", 0) for d in recent),
        "wins":     sum(d.get("wins", 0)   for d in recent),
        "losses":   sum(d.get("losses", 0) for d in recent),
        "stake":    sum(d.get("total_stake_dollars", 0.0)  for d in recent),
        "pnl":      sum(d.get("total_pnl_dollars", 0.0)    for d in recent),
        "days":     len(recent),
    }


def _stage_recommendation(cfg: dict, live_perf: dict | None) -> tuple[str, str]:
    """
    Returns (current_stage_label, advice_line).

    Stages are encoded by max_stake_per_pick_dollars. We never auto-bump;
    we just compare actual PnL against the next stage's gate and advise
    in the morning summary. Operator flips the config when they're ready.

    Stage gates were chosen so each step doubles capital deployed while
    requiring at least 2 weeks of consistent positive ROI to advance:
      $5  → $10 : 14 days of live data, total ROI ≥ 0%
      $10 → $25 : 30 days of live data, total ROI ≥ +5%
      $25 → $50 : 60 days of live data, total ROI ≥ +10%
    """
    current_cap = float(cfg.get("max_stake_per_pick_dollars") or 0)
    if current_cap >= 50:
        return (f"$50 cap (mature)", "")
    if current_cap >= 25:
        threshold_days, threshold_roi, next_cap = 60, 10.0, 50
        current_label = "$25 cap"
    elif current_cap >= 10:
        threshold_days, threshold_roi, next_cap = 30, 5.0, 25
        current_label = "$10 cap"
    else:
        threshold_days, threshold_roi, next_cap = 14, 0.0, 10
        current_label = f"${int(current_cap)} cap (Phase 3 launch)"

    if not live_perf or not live_perf.get("daily"):
        advice = f"  Bump gate: ${next_cap} after {threshold_days}d of live data ≥ {threshold_roi:+.0f}% ROI (currently 0d of live data)"
        return current_label, advice

    days_live = len(live_perf["daily"])
    total_pnl = float(live_perf.get("total_pnl_dollars") or 0)
    total_stake = float(live_perf.get("total_stake_dollars") or 0)
    roi = (total_pnl / total_stake * 100) if total_stake > 0 else 0.0

    if days_live >= threshold_days and roi >= threshold_roi:
        advice = (f"  ✅ READY TO BUMP: {days_live}d live, ROI {roi:+.1f}% — "
                  f"edit data/kalshi_config.json max_stake_per_pick_dollars to ${next_cap}")
    else:
        days_missing = max(0, threshold_days - days_live)
        roi_gap = threshold_roi - roi
        if days_missing > 0 and roi_gap > 0:
            advice = (f"  Bump gate: {days_live}/{threshold_days}d live, "
                      f"ROI {roi:+.1f}% (need ≥{threshold_roi:+.0f}%) → ${next_cap}")
        elif days_missing > 0:
            advice = (f"  Bump gate: {days_live}/{threshold_days}d live, "
                      f"ROI {roi:+.1f}% ✓ → ${next_cap}")
        else:
            advice = (f"  Bump gate: {days_live}d live ✓, "
                      f"ROI {roi:+.1f}% (need ≥{threshold_roi:+.0f}%) → ${next_cap}")
    return current_label, advice


def build_summary(date_key: str) -> str:
    cfg = json.loads(Path(CONFIG_PATH).read_text())
    cfg = {k: v for k, v in cfg.items() if not k.startswith("_") and not k.endswith("_doc")}

    paper_perf = _load_perf(PAPER_PERF_PATH)
    live_perf  = _load_perf(LIVE_PERF_PATH)

    # Yesterday's snapshot from the last entry of each perf file
    def _last_day(perf):
        if not perf or not perf.get("daily"): return None
        return perf["daily"][-1]
    last_paper = _last_day(paper_perf)
    last_live  = _last_day(live_perf)

    today_plan = _read_today_plan(date_key)
    paper_7d = _rolling_pnl(paper_perf, 7)
    live_7d  = _rolling_pnl(live_perf, 7)
    stage_label, advice = _stage_recommendation(cfg, live_perf)

    lines = [f"☀️ BTF Morning Summary · {date_key}", ""]

    # Yesterday
    if last_live and last_live.get("placed", 0) > 0:
        lines.append(f"YESTERDAY (LIVE): {last_live.get('placed',0)} placed · "
                     f"{last_live.get('wins',0)}W {last_live.get('losses',0)}L · "
                     f"${last_live.get('total_pnl_dollars',0):+.2f} "
                     f"({last_live.get('roi_pct',0):+.1f}% ROI)")
    elif last_paper and last_paper.get("placed", 0) > 0:
        lines.append(f"YESTERDAY (paper): {last_paper.get('placed',0)} simulated · "
                     f"{last_paper.get('wins',0)}W {last_paper.get('losses',0)}L · "
                     f"${last_paper.get('total_pnl_dollars',0):+.2f} "
                     f"({last_paper.get('roi_pct',0):+.1f}% ROI)")
    else:
        lines.append("YESTERDAY: no orders placed/simulated")

    # Today's plan
    lines.append("")
    if today_plan["orders_would_place"] > 0:
        lines.append(f"TODAY: {today_plan['orders_would_place']} order(s) queued · "
                     f"${today_plan['total_stake_dollars']:.2f} stake "
                     f"(from {today_plan['picks_total']} total picks)")
    else:
        lines.append(f"TODAY: 0 auto-bets queued (no picks met cal≥{cfg.get('min_calibrated_score','?')} threshold)")
    # Account snapshot. If we have both cash and positions, show the
    # three-line breakdown. Else fall back to the older single-line.
    cash = today_plan.get("bankroll_used_dollars")
    positions = today_plan.get("open_positions_dollars")
    total = today_plan.get("total_account_dollars")
    if cash is not None and positions is not None and positions > 0:
        lines.append(f"  Cash: ${cash:.2f} · Open positions: ${positions:.2f} · Total: ${total:.2f}")
    elif cash is not None:
        lines.append(f"  Bankroll: ${cash:.2f}")

    # 7-day rolling
    lines.append("")
    if live_7d["days"] > 0:
        lines.append(f"7d LIVE: {live_7d['placed']} placed · "
                     f"{live_7d['wins']}W {live_7d['losses']}L · "
                     f"${live_7d['pnl']:+.2f}")
    if paper_7d["days"] > 0:
        lines.append(f"7d paper: {paper_7d['placed']} simulated · "
                     f"{paper_7d['wins']}W {paper_7d['losses']}L · "
                     f"${paper_7d['pnl']:+.2f}")

    # Stage advisor
    lines.append("")
    lines.append(f"STAGE: {stage_label}")
    if advice:
        lines.append(advice)

    return "\n".join(lines)


def send(body: str, webhook: str) -> None:
    """Send to webhook. Auto-detects ntfy.sh (plain text + headers) vs JSON."""
    if "ntfy.sh" in webhook or "/ntfy/" in webhook:
        req = urllib.request.Request(
            webhook,
            data=body.encode("utf-8"),
            headers={
                "Title": "BTF Morning Summary",
                "Priority": "3",   # default — informational, not urgent
                "Tags": "sunny,bar_chart",
            },
            method="POST",
        )
    else:
        payload = json.dumps({"content": body, "text": body}).encode("utf-8")
        req = urllib.request.Request(
            webhook, data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
    with urllib.request.urlopen(req, timeout=10) as r:
        if r.status < 200 or r.status >= 300:
            raise RuntimeError(f"webhook responded HTTP {r.status}")


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--date", help="ET date (default: today)")
    ap.add_argument("--dry", action="store_true", help="Print to stdout only, don't POST")
    args = ap.parse_args()

    date_key = args.date or _today_et_date()
    body = build_summary(date_key)

    print(body)
    print()

    if args.dry:
        print("[DRY] Skipping webhook POST")
        return

    webhook = os.environ.get("WEBHOOK_URL", "").strip()
    if not webhook:
        print("WEBHOOK_URL not set — summary printed but not pushed")
        return

    try:
        send(body, webhook)
        print("✓ Pushed to webhook")
    except Exception as e:
        # Don't fail the workflow over a notification hiccup — just log.
        print(f"⚠ Webhook POST failed: {type(e).__name__}: {e}")


if __name__ == "__main__":
    main()
