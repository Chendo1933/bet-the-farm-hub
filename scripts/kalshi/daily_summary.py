#!/usr/bin/env python3
"""
BTF push-notification dispatcher — two modes for two daily cadences.

  --mode recap   Sends yesterday's results + 7d rolling + stage advisor.
                 Fired by .github/workflows/kalshi-daily-recap.yml
                 immediately after Kalshi Reconcile completes (early AM).

  --mode plan    Sends "we just placed these orders today" with the
                 ticker/contract/price breakdown. Fired by
                 .github/workflows/kalshi-daily-plan.yml immediately
                 after Kalshi Place Orders (Live) completes (~12:10 PM ET).

Why split: prior to 2026-05-20, this was one combined notification
fired after Reconcile only. That meant "today's plan" came from a
stale dry-run snapshot — by morning the user got "yesterday: 1W 1L"
followed by "today: 0 bets queued" because the dry-run hadn't run
for today yet. Splitting into two pings fixes the ordering: morning
ping has only confirmed yesterday's results, post-placement ping
has the actual just-placed bets.

This script is read-only on data and write-only on the webhook. It
never modifies kalshi_config.json or any placed orders. The point is
passive visibility — open the phone, see the notification, decide
whether to bump stake caps. If the script itself fails, the
workflow's notify-failure step still fires (same webhook channel,
different priority).

Usage:
  python3 scripts/kalshi/daily_summary.py --mode recap
  python3 scripts/kalshi/daily_summary.py --mode plan
  python3 scripts/kalshi/daily_summary.py --mode recap --dry  # print only
  python3 scripts/kalshi/daily_summary.py --mode plan --date 2026-05-21

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


def _load_dryrun(date_key: str) -> dict | None:
    """Read the dry-run snapshot (auto-bet candidates + bankroll info)."""
    p = Path(DRYRUN_DIR) / f"{date_key}.json"
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text())
    except Exception:
        return None


def _load_orders(date_key: str) -> dict | None:
    """Read today's order receipts file (post-placement)."""
    p = Path(ORDERS_DIR) / f"{date_key}.json"
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text())
    except Exception:
        return None


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


# ── Recap mode ──────────────────────────────────────────────────────────────
# Yesterday-focused. Fires after Reconcile (early AM) when yesterday's
# outcomes have been graded and PnL written. Does NOT mention today's
# auto-bets because dry-run hasn't run yet at this point.

def build_recap(date_key: str) -> tuple[str, str]:
    """Returns (title, body) for the recap notification.

    `date_key` is today's ET date — the recap talks about yesterday
    relative to that, which is whatever's at the tail of the perf
    files (already filtered to most-recent reconciled day).
    """
    cfg = json.loads(Path(CONFIG_PATH).read_text())
    cfg = {k: v for k, v in cfg.items() if not k.startswith("_") and not k.endswith("_doc")}

    paper_perf = _load_perf(PAPER_PERF_PATH)
    live_perf  = _load_perf(LIVE_PERF_PATH)

    def _last_day(perf):
        if not perf or not perf.get("daily"): return None
        return perf["daily"][-1]
    last_paper = _last_day(paper_perf)
    last_live  = _last_day(live_perf)

    paper_7d = _rolling_pnl(paper_perf, 7)
    live_7d  = _rolling_pnl(live_perf, 7)
    stage_label, advice = _stage_recommendation(cfg, live_perf)

    lines = [f"🌙 BTF Recap · {date_key}", ""]

    # Yesterday — live preferred, paper as fallback
    if last_live and last_live.get("placed", 0) > 0:
        prev = last_live
        lines.append(f"YESTERDAY (LIVE): {prev.get('placed',0)} placed · "
                     f"{prev.get('wins',0)}W {prev.get('losses',0)}L · "
                     f"${prev.get('total_pnl_dollars',0):+.2f} "
                     f"({prev.get('roi_pct',0):+.1f}% ROI)")
        # Show per-order detail when we have it (live only — these have ticker info)
        date = prev.get("date")
        orders = _load_orders(date) if date else None
        if orders and orders.get("placed_orders"):
            for o in orders["placed_orders"]:
                if o.get("test"): continue   # skip the smoke-test
                ticker = o.get("ticker", "?")
                # Short label: extract the last "-TEAM" suffix if present
                short = ticker.rsplit("-", 1)[-1] if "-" in ticker else ticker
                outcome = o.get("outcome") or o.get("status", "?")
                pnl = o.get("pnl_dollars")
                pnl_str = f"${pnl:+.2f}" if pnl is not None else "(ungraded)"
                # ✅ for win, ❌ for loss, ⏳ for ungraded
                icon = "✅" if (pnl and pnl > 0) else ("❌" if (pnl and pnl < 0) else "⏳")
                lines.append(f"  {icon} {short} {pnl_str}")
    elif last_paper and last_paper.get("placed", 0) > 0:
        prev = last_paper
        lines.append(f"YESTERDAY (paper): {prev.get('placed',0)} simulated · "
                     f"{prev.get('wins',0)}W {prev.get('losses',0)}L · "
                     f"${prev.get('total_pnl_dollars',0):+.2f} "
                     f"({prev.get('roi_pct',0):+.1f}% ROI)")
    else:
        lines.append("YESTERDAY: no orders placed/simulated")

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

    return f"BTF Recap · {date_key}", "\n".join(lines)


# ── Plan mode ───────────────────────────────────────────────────────────────
# Today-focused. Fires AFTER place_orders.py has run and committed receipts.
# Reports what JUST got placed, with per-order detail.

def build_plan(date_key: str) -> tuple[str, str]:
    """Returns (title, body) for the post-placement plan notification."""
    cfg = json.loads(Path(CONFIG_PATH).read_text())
    cfg = {k: v for k, v in cfg.items() if not k.startswith("_") and not k.endswith("_doc")}

    orders = _load_orders(date_key)
    dryrun = _load_dryrun(date_key)

    lines = [f"🎯 BTF Today's Bets · {date_key}", ""]

    placed = [] if not orders else [o for o in (orders.get("placed_orders") or []) if not o.get("test")]
    skipped = [] if not orders else (orders.get("skipped") or [])

    if not placed and not skipped and not orders:
        # No orders file yet — place_orders hasn't run, or there were
        # no candidates from the dry-run. Surface what the dry-run had
        # so the operator at least sees today's candidates list.
        if dryrun:
            picks_total = (dryrun.get("summary") or {}).get("picks_total", 0)
            would_place = (dryrun.get("summary") or {}).get("orders_would_place", 0)
            lines.append(f"No orders placed today.")
            lines.append(f"  Dry-run saw {picks_total} picks, {would_place} eligible after filters.")
        else:
            lines.append("No order receipts found for today yet.")
        return f"BTF Today · {date_key}", "\n".join(lines)

    if not placed and skipped:
        # All candidates were skipped — usually means markets closed
        # before the workflow could fire, or all already had positions.
        lines.append(f"0 orders placed · {len(skipped)} skipped")
        # Most common skip reasons grouped
        reasons = {}
        for s in skipped:
            r = s.get("skip_reason", "unknown")
            reasons[r] = reasons.get(r, 0) + 1
        for r, n in sorted(reasons.items(), key=lambda x: -x[1]):
            lines.append(f"  · {n}× {r}")
    elif placed:
        total_stake = sum(o.get("stake_dollars") or 0 for o in placed)
        lines.append(f"{len(placed)} order(s) placed · ${total_stake:.2f} total stake")
        for o in placed:
            ticker = o.get("ticker", "?")
            short = ticker.rsplit("-", 1)[-1] if "-" in ticker else ticker
            contracts = o.get("contracts", 0)
            price = o.get("price_cents", 0)
            stake = o.get("stake_dollars", 0)
            status = o.get("status", "?")
            # Compact: SIDE TICKER · 5× @ 54¢ · $2.70 · executed
            lines.append(f"  · {short}: {contracts}× @ {price}¢ = ${stake:.2f} ({status})")
        if skipped:
            lines.append(f"  + {len(skipped)} skipped")

    # Account snapshot — pulled from this morning's dry-run (BEFORE today's
    # stakes were deployed) plus subtract what we just placed.
    if dryrun:
        s = dryrun.get("summary", {})
        pre_cash = s.get("bankroll_used_dollars")
        pre_positions = s.get("open_positions_dollars") or 0
        pre_total = s.get("total_account_dollars")
        placed_stake = sum(o.get("stake_dollars") or 0 for o in placed)
        if pre_cash is not None:
            post_cash = max(0.0, pre_cash - placed_stake)
            post_positions = pre_positions + placed_stake
            post_total = post_cash + post_positions
            lines.append("")
            lines.append(f"After placement (est.):")
            lines.append(f"  Cash ${post_cash:.2f} · Positions ${post_positions:.2f} · Total ${post_total:.2f}")

    return f"BTF Today · {date_key}", "\n".join(lines)


def send(title: str, body: str, webhook: str, priority: str = "3", tags: str = "sunny,bar_chart") -> None:
    """Send to webhook. Auto-detects ntfy.sh (plain text + headers) vs JSON."""
    if "ntfy.sh" in webhook or "/ntfy/" in webhook:
        req = urllib.request.Request(
            webhook,
            data=body.encode("utf-8"),
            headers={
                "Title": title,
                "Priority": priority,
                "Tags": tags,
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
    ap.add_argument("--mode", choices=("recap", "plan"), default="recap",
                    help="recap = yesterday's results (after reconcile); "
                         "plan = today's just-placed bets (after place_orders)")
    ap.add_argument("--date", help="ET date (default: today)")
    ap.add_argument("--dry", action="store_true", help="Print to stdout only, don't POST")
    args = ap.parse_args()

    date_key = args.date or _today_et_date()

    if args.mode == "recap":
        title, body = build_recap(date_key)
        tags = "moon,bar_chart"
    else:   # plan
        title, body = build_plan(date_key)
        tags = "dart,money_with_wings"

    print(body)
    print()

    if args.dry:
        print(f"[DRY mode={args.mode}] Skipping webhook POST")
        return

    webhook = os.environ.get("WEBHOOK_URL", "").strip()
    if not webhook:
        print("WEBHOOK_URL not set — body printed but not pushed")
        return

    try:
        send(title, body, webhook, priority="3", tags=tags)
        print(f"✓ Pushed to webhook (mode={args.mode})")
    except Exception as e:
        # Don't fail the workflow over a notification hiccup — just log.
        print(f"⚠ Webhook POST failed: {type(e).__name__}: {e}")


if __name__ == "__main__":
    main()
