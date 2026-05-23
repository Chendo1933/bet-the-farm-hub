#!/usr/bin/env python3
"""
Pipeline health-check + daily heartbeat — the dead-man's switch.

WHY THIS EXISTS
  Every workflow already pings WEBHOOK_URL via `if: failure()`. But that
  only fires when a workflow RUNS and FAILS. It structurally CANNOT catch:
    1. A workflow that never ran (cron disabled, or skipped because an
       upstream link in the workflow_run chain failed — skipped jobs don't
       fire `if: failure()`).
    2. A workflow that succeeded but did nothing it should have (e.g.
       place_orders exits 0 having placed nothing due to a silent bug — it
       looks identical to "no good bets today").
    3. A bad/expired trade credential that only surfaces mid-bet — exactly
       the PEM-parse failure that silently killed live betting for 5 days.

  This script runs on its OWN independent daily cron and ACTIVELY verifies
  the pipeline is alive: it authenticates to Kalshi with the TRADE key and
  reads the balance (proactively validating the credential before the next
  betting window), and confirms the dry-run / results files are fresh. It
  emits ONE push every day:
    ✅ healthy heartbeat   (low priority) — its absence is itself a signal
    ⚠️ warning             (stale data / silent no-action)
    🚨 critical alert      (trade credential dead / can't reach Kalshi)

USAGE
  WEBHOOK_URL=...  KALSHI_ENVIRONMENT=live \
  KALSHI_API_KEY_ID=<trade key>  KALSHI_PRIVATE_KEY=<trade pem> \
  python scripts/kalshi/health_check.py [--dry]

  --dry  Print the report to stdout, don't POST to the webhook.
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import sys
import urllib.request
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

CONFIG_PATH  = "data/kalshi_config.json"
DRYRUN_DIR   = "data/kalshi_dryrun"
ORDERS_DIR   = "data/kalshi_orders"
RESULTS_DIR  = "data/results"
LIVE_PERF    = "data/kalshi_live_perf.json"

# Staleness thresholds (days). Generous to avoid false alarms on the
# inherent 1-day lag of results and any single dropped cron.
DRYRUN_STALE_DAYS  = 2   # dry-run should write a snapshot every day
RESULTS_STALE_DAYS = 3   # results lag ~1 day; >3 means the logger is down


def _today_et() -> datetime:
    return datetime.now(ZoneInfo("America/New_York"))


def _load_config() -> dict:
    try:
        raw = json.loads(Path(CONFIG_PATH).read_text())
        return {k: v for k, v in raw.items()
                if not k.startswith("_") and not k.endswith("_doc")}
    except Exception:
        return {}


def _latest_dated_file(directory: str) -> tuple[str | None, str | None]:
    """Return (YYYY-MM-DD, path) of the newest date-named file in a dir.
    Filenames are the source of truth — mtime is useless in CI (fresh checkout)."""
    best_date = None; best_path = None
    for p in glob.glob(f"{directory}/*.json"):
        stem = Path(p).stem
        if len(stem) == 10 and stem[4] == "-" and stem[7] == "-":
            if best_date is None or stem > best_date:
                best_date = stem; best_path = p
    return best_date, best_path


def _days_since(date_str: str | None, today: datetime) -> int | None:
    if not date_str:
        return None
    try:
        d = datetime.strptime(date_str, "%Y-%m-%d").date()
    except ValueError:
        return None
    return (today.date() - d).days


def _send(title: str, body: str, webhook: str, priority: str, tags: str) -> None:
    """Match the ntfy/JSON auto-detect convention used elsewhere in the suite."""
    if "ntfy.sh" in webhook or "/ntfy/" in webhook:
        req = urllib.request.Request(
            webhook, data=body.encode("utf-8"),
            headers={"Title": title, "Priority": priority, "Tags": tags},
            method="POST",
        )
    else:
        payload = json.dumps({"content": f"{title}\n{body}",
                              "text": f"{title}\n{body}"}).encode("utf-8")
        req = urllib.request.Request(
            webhook, data=payload,
            headers={"Content-Type": "application/json"}, method="POST",
        )
    with urllib.request.urlopen(req, timeout=10) as r:
        if r.status < 200 or r.status >= 300:
            raise RuntimeError(f"webhook responded HTTP {r.status}")


def check_credential(cfg: dict, today: datetime) -> tuple[list[str], list[str], float | None]:
    """Authenticate to Kalshi with whatever key env is wired in and read the
    balance. This is the headline check — it validates the exact credential
    that failed silently for 5 days, BEFORE the next betting window.

    Returns (criticals, infos, balance_dollars)."""
    crit: list[str] = []; info: list[str] = []
    auto_on = bool(cfg.get("auto_trading_enabled"))
    env = (os.environ.get("KALSHI_ENVIRONMENT") or cfg.get("environment") or "demo").lower()

    has_key = os.environ.get("KALSHI_API_KEY_ID")
    has_pem = os.environ.get("KALSHI_PRIVATE_KEY") or os.environ.get("KALSHI_PRIVATE_KEY_PATH")
    if not (has_key and has_pem):
        # Only a problem if we're supposed to be trading.
        if auto_on:
            crit.append("Trade credential NOT configured (KALSHI_API_KEY_ID / "
                        "KALSHI_PRIVATE_KEY missing) but auto_trading_enabled=true — "
                        "no live orders can be placed.")
        else:
            info.append("Kalshi keys not provided to health-check (auto-trading off — OK).")
        return crit, info, None

    try:
        from kalshi.client import KalshiClient, KalshiAPIError
        client = KalshiClient(environment=env)
        bal = client.get_balance()
        balance_dollars = (bal.get("balance", 0) or 0) / 100
        info.append(f"Kalshi {env} auth OK · balance ${balance_dollars:,.2f}")
        if auto_on and balance_dollars < 1:
            info.append("Balance under $1 — no live bets will clear pre-flight (fund to resume).")
        return crit, info, balance_dollars
    except Exception as e:
        msg = str(e)
        # The signature of the 5-day outage was a PEM parse error.
        if "private key" in msg.lower() or "pem" in msg.lower():
            crit.append(f"TRADE KEY BROKEN — could not load/parse private key: {msg[:160]}. "
                        f"This is the silent-outage failure mode. Re-paste "
                        f"KALSHI_TRADE_PRIVATE_KEY (must include BEGIN/END lines).")
        else:
            crit.append(f"Kalshi {env} auth/balance FAILED: {msg[:200]}")
        return crit, info, None


def check_freshness(today: datetime) -> tuple[list[str], list[str]]:
    """Confirm the daily-producing stages actually produced files recently."""
    warn: list[str] = []; info: list[str] = []

    dr_date, _ = _latest_dated_file(DRYRUN_DIR)
    dr_age = _days_since(dr_date, today)
    if dr_age is None:
        warn.append(f"No dry-run snapshots found in {DRYRUN_DIR}/ — the dry-run "
                    f"stage may never have run.")
    elif dr_age >= DRYRUN_STALE_DAYS:
        warn.append(f"Dry-run snapshot is {dr_age}d old (latest {dr_date}) — the "
                    f"pick→dry-run chain looks stalled.")
    else:
        info.append(f"Dry-run fresh (latest {dr_date}, {dr_age}d).")

    rs_date, _ = _latest_dated_file(RESULTS_DIR)
    rs_age = _days_since(rs_date, today)
    if rs_age is None:
        warn.append(f"No results files found in {RESULTS_DIR}/ — results logger down.")
    elif rs_age >= RESULTS_STALE_DAYS:
        warn.append(f"Results are {rs_age}d old (latest {rs_date}) — the nightly "
                    f"results logger looks stalled.")
    else:
        info.append(f"Results fresh (latest {rs_date}, {rs_age}d).")

    return warn, info


def check_silent_no_action(cfg: dict, today: datetime) -> list[str]:
    """If auto-trading is on and today's dry-run produced placeable candidates
    but no order receipt landed, place_orders may have silently done nothing.
    Conservative: only flag when the dry-run clearly had would-place picks."""
    warn: list[str] = []
    if not cfg.get("auto_trading_enabled"):
        return warn
    today_str = today.strftime("%Y-%m-%d")
    dr_path = Path(DRYRUN_DIR) / f"{today_str}.json"
    if not dr_path.exists():
        return warn  # freshness check already covers a missing dry-run
    try:
        dr = json.loads(dr_path.read_text())
    except Exception:
        return warn
    # Count placeable LIVE candidates in today's dry-run snapshot. Prefer the
    # authoritative summary count; fall back to the orders array length. Note:
    # paper_orders are excluded on purpose — they never place real money.
    candidates = 0
    summary = dr.get("summary")
    if isinstance(summary, dict) and isinstance(summary.get("orders_would_place"), int):
        candidates = summary["orders_would_place"]
    else:
        for key in ("would_place", "orders", "live_orders", "placed_orders"):
            v = dr.get(key)
            if isinstance(v, list):
                candidates = max(candidates, len(v))
    if candidates == 0:
        return warn  # legitimately no bets today
    orders_path = Path(ORDERS_DIR) / f"{today_str}.json"
    if not orders_path.exists():
        warn.append(f"Auto-trading ON and today's dry-run had {candidates} placeable "
                    f"candidate(s), but no order receipt {orders_path} exists — "
                    f"place_orders may have failed or been skipped (chain break).")
    return warn


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry", action="store_true",
                    help="Print report only, do not POST to webhook")
    args = ap.parse_args()

    today = _today_et()
    cfg = _load_config()

    crit, cred_info, _balance = check_credential(cfg, today)
    fresh_warn, fresh_info = check_freshness(today)
    silent_warn = check_silent_no_action(cfg, today)

    warns = fresh_warn + silent_warn
    infos = cred_info + fresh_info

    auto = "ON" if cfg.get("auto_trading_enabled") else "OFF"
    env = (cfg.get("environment") or "demo")
    header = f"BTF pipeline health · {today.strftime('%Y-%m-%d %H:%M ET')} · auto-trading {auto} ({env})"

    lines = [header, ""]
    if crit:
        lines.append("🚨 CRITICAL:")
        lines += [f"  • {c}" for c in crit]
        lines.append("")
    if warns:
        lines.append("⚠️ WARNINGS:")
        lines += [f"  • {w}" for w in warns]
        lines.append("")
    if infos:
        lines.append("Status:")
        lines += [f"  • {i}" for i in infos]
    body = "\n".join(lines).rstrip()

    if crit:
        title = "🚨 BTF pipeline ALERT"; priority = "5"; tags = "rotating_light,warning"
    elif warns:
        title = "⚠️ BTF pipeline warning"; priority = "4"; tags = "warning"
    else:
        title = "✅ BTF pipeline healthy"; priority = "2"; tags = "white_check_mark"

    print(body)
    print()

    if args.dry:
        print(f"[DRY] would send title={title!r} priority={priority}")
        return

    webhook = os.environ.get("WEBHOOK_URL", "").strip()
    if not webhook:
        print("WEBHOOK_URL not set — report printed but not pushed")
        return
    try:
        _send(title, body, webhook, priority=priority, tags=tags)
        print(f"✓ Pushed health report ({title})")
    except Exception as e:
        # Don't let a webhook hiccup mask the report — but DO fail the
        # workflow so the per-workflow failure alert is the backstop.
        print(f"✗ Failed to push health report: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
