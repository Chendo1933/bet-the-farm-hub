#!/usr/bin/env python3
"""
Bet The Farm Hub — Daily pick logger
Uses Playwright to headlessly load the hub, inject the Odds API key,
wait for live picks to generate, then save ALL picks with real game
data to data/picks/YYYY-MM-DD.json for nightly performance grading.

Required GitHub Secret: ODDS_API_KEY (your the-odds-api.com key)
If the secret isn't set OR live games fail to load, the script exits
with code 1 so the GitHub Actions log shows a clear failure rather
than silently saving ungradeable ATS-trend picks.

Run before games start (~noon ET) via log-picks.yml workflow.
"""

import asyncio
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

# Eastern Time is the reference timezone for game scheduling — MLB/NBA/NHL
# schedules, and the hub's TODAY_GAMES `date` strings ("Apr 17"), are all ET-based.
# Using ET for our filename + filter avoids UTC-drift bugs where the logger
# runs before midnight ET but after midnight UTC and tags picks with the wrong day.
ET_ZONE = ZoneInfo("America/New_York")

DATA_DIR   = "data/picks"
SCHED_DIR  = "data/schedules"
SNAP_DIR   = "data/odds_snapshots"
HUB_FILE   = "index.html"
PORT       = 8181
TIMEOUT    = 90_000   # 90s — allow time for multiple sport API calls (MLB+NBA+NHL each with 700ms stagger)


async def scrape_picks(api_key: str | None) -> tuple[list[dict], list[dict]]:
    from playwright.async_api import async_playwright

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context()

        # Inject Odds API key into localStorage before the page loads
        # The hub reads localStorage.btf_odds_key on startup and auto-refreshes
        # Use json.dumps() to safely encode the key — raw f-string interpolation into
        # a single-quoted JS string breaks if the key contains quotes, newlines, or
        # trailing whitespace (common when GitHub Secrets are copy-pasted), causing
        # "Invalid or unexpected token" page errors and the hub never loading the key.
        if api_key:
            safe_key = api_key.strip()   # strip any accidental whitespace/newlines
            await context.add_init_script(
                f"localStorage.setItem('btf_odds_key', {json.dumps(safe_key)});"
            )
            print(f"  ✓ Odds API key injected ({safe_key[:6]}…)")
        else:
            print("  ❌ No ODDS_API_KEY set — cannot log real game picks")
            sys.exit(1)

        page = await context.new_page()

        # Print errors/warnings from the hub; suppress noisy info/debug logs
        def _on_console(m):
            if m.type in ("error", "warning"):
                print(f"  [hub:{m.type}] {m.text}")
        page.on("console", _on_console)
        page.on("pageerror", lambda e: print(f"  [page error] {e}"))

        await page.goto(f"http://localhost:{PORT}/{HUB_FILE}", wait_until="domcontentloaded")
        print(f"  ✓ Hub loaded at localhost:{PORT}")

        if api_key:
            # Inject morning odds snapshot for line-movement tracking (Factor F12).
            # The hub reads window.BTF_MORNING_ODDS when building TODAY_GAMES and
            # computes spreadMove = current spread − morning spread.
            morning_path = os.path.join(SNAP_DIR, f"{datetime.now(timezone.utc).strftime('%Y-%m-%d')}-morning.json")
            if os.path.exists(morning_path):
                with open(morning_path) as mf:
                    morning_json = mf.read()
                await page.evaluate(f"window.BTF_MORNING_ODDS = {morning_json};")
                print(f"  ✓ Morning odds snapshot injected for line movement")
            else:
                print(f"  ⚠  No morning snapshot found ({morning_path}) — F12 will skip")

            # The hub's auto-refresh only fires when the Analyzer tab is opened.
            # In headless mode we never open that tab, so we must call refreshAllOdds()
            # explicitly via JS. Wait for init() to finish first (key loaded into DOM),
            # then trigger the fetch and wait up to 45s for TODAY_GAMES to populate.
            await page.wait_for_function("typeof refreshAllOdds === 'function'", timeout=10_000)
            print("  ⏳ Triggering live odds fetch…")
            await page.evaluate("refreshAllOdds()")
            try:
                await page.wait_for_function(
                    "typeof TODAY_GAMES !== 'undefined' && Array.isArray(TODAY_GAMES) && TODAY_GAMES.length > 0",
                    timeout=TIMEOUT
                )
                game_count = await page.evaluate("TODAY_GAMES.length")
                print(f"  ✓ {game_count} game(s) loaded from ESPN/Odds API")
            except Exception:
                # Read hub's status bar to surface why the API fetch failed
                try:
                    oab_status = await page.evaluate(
                        "document.querySelector('.oab-status')?.textContent?.trim() || '(no status text found)'"
                    )
                    print(f"  [hub status] {oab_status}")
                    game_count_raw = await page.evaluate(
                        "typeof TODAY_GAMES !== 'undefined' ? TODAY_GAMES.length : 'undefined'"
                    )
                    print(f"  [TODAY_GAMES.length] {game_count_raw}")
                except Exception as diag_err:
                    print(f"  [diag error] {diag_err}")
                print("  ❌ Timed out waiting for live games — refusing to save trend picks")
                await browser.close()
                sys.exit(1)

        # Click the Hot Bets tab to trigger hbRender() + BTF_PICKS export
        await page.click("button[onclick*=\"showTab('hotbets'\"]")
        print("  ✓ Hot Bets tab activated")

        # Wait for BTF_PICKS_READY flag we added to hbRender()
        await page.wait_for_function("window.BTF_PICKS_READY === true", timeout=10_000)

        picks = await page.evaluate("window.BTF_PICKS")
        print(f"  ✓ {len(picks)} total picks generated")

        # Capture schedule snapshot — ALL today's games with spread/total
        # TODAY_GAMES is a `let` variable (not a window property) — access without window.
        # Guard with ||[] so a timeout/empty-odds run returns [] instead of crashing.
        today_games = await page.evaluate(
            "(typeof TODAY_GAMES !== 'undefined' ? TODAY_GAMES : []).map(g => ({sport:g.sport, home:g.home, away:g.away,"
            " date:g.date, time:g.time, spread:g.spread, total:g.total,"
            " spreadMove:g.spreadMove??null, totalMove:g.totalMove??null}))"
        )
        print(f"  ✓ {len(today_games)} game(s) in today's schedule snapshot")

        await browser.close()
        return picks, today_games


def main():
    # ── Optional --out-suffix flag ────────────────────────────────────────────
    # When set, picks are written to data/picks/{date}-{suffix}.json instead of
    # the canonical {date}.json. Used by the 12:30 PM ET backup logger to write
    # a comparison snapshot without overwriting the primary slate (which is
    # what gets graded tomorrow). Schedule snapshot uses the same suffix.
    out_suffix = ""
    for arg in sys.argv[1:]:
        if arg.startswith("--out-suffix="):
            out_suffix = "-" + arg.split("=", 1)[1].strip()

    now_utc  = datetime.now(timezone.utc)
    now_et   = now_utc.astimezone(ET_ZONE)
    # Filename uses ET date so picks for "today's games in ET" land in the ET-labeled file.
    date_key = now_et.strftime("%Y-%m-%d")
    # Hub formats game dates like "Apr 17" (no zero-pad). Build the exact string to
    # filter against TODAY_GAMES[].date and pick[].date so we drop tomorrow's games.
    today_label = f"{now_et.strftime('%b')} {now_et.day}"
    api_key  = os.environ.get("ODDS_API_KEY", "").strip() or None

    print(f"[log_picks] {now_utc.strftime('%Y-%m-%d %H:%M UTC')} "
          f"(ET: {now_et.strftime('%Y-%m-%d %H:%M')} — filtering for '{today_label}')")
    os.makedirs(DATA_DIR, exist_ok=True)

    # Start a local HTTP server so the hub can make same-origin API calls
    server = subprocess.Popen(
        [sys.executable, "-m", "http.server", str(PORT)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    time.sleep(1.5)

    try:
        picks, today_games = asyncio.run(scrape_picks(api_key))
    finally:
        server.terminate()

    # The hub's TODAY_GAMES window is 36h (today + tomorrow) so the UI can preview
    # upcoming slates. For our persistent day-labeled log we only want games on ET-today —
    # otherwise tomorrow's picks land in today's file and can never be matched against
    # today's results file.
    tomorrow_games = [g for g in today_games if g.get("date") != today_label]
    today_games    = [g for g in today_games if g.get("date") == today_label]
    if tomorrow_games:
        print(f"  ℹ  Filtered out {len(tomorrow_games)} game(s) scheduled for future dates "
              f"(kept only '{today_label}')")

    # Save schedule snapshot so log_results.py can pair scores with spreads later
    if today_games:
        os.makedirs(SCHED_DIR, exist_ok=True)
        sched_path = os.path.join(SCHED_DIR, f"{date_key}{out_suffix}.json")
        with open(sched_path, "w") as f:
            json.dump({
                "date":      date_key,
                "logged":    now_utc.isoformat(),
                "has_odds":  api_key is not None,
                "games":     today_games,
            }, f, indent=2)
        spread_count = sum(1 for g in today_games if g.get("spread") is not None)
        print(f"✅ Schedule snapshot: {len(today_games)} game(s), "
              f"{spread_count} with spread data → {sched_path}")
    else:
        print("⚠  No TODAY_GAMES captured — schedule snapshot skipped")

    # Keep ALL picks with real game data — opponent must be known (away != "").
    # Spread/ML picks have atsPick set; O/U picks have betType='ou' + pickedTeam.
    # ATS-trend picks have away="" and atsPick=None — those are ungradeable and excluded.
    # We no longer filter by tier so every scored game gets tracked for calibration.
    #
    # Also drop picks whose game is NOT today in ET (the hub's 36h TODAY_GAMES window
    # bleeds tomorrow's games into today's pick list). Without this filter, tomorrow's
    # games get saved in today's picks file and then orphan at grading time because
    # today's results file only has today's games.
    future_picks = [p for p in picks if p.get("date") and p.get("date") != today_label]
    if future_picks:
        print(f"  ℹ  Filtered out {len(future_picks)} pick(s) on future-date games "
              f"(hub's 36h preview window — not today's slate)")

    tracked = [
        p for p in picks
        if p.get("away", "").strip() != ""
        and (p.get("atsPick") is not None or p.get("betType") == "ou")
        and p.get("date") == today_label
    ]

    if not tracked:
        print("  ❌ No real-game picks found even with live odds — check hub scoring output")
        sys.exit(1)

    print(f"\n  📊 {len(tracked)} pick(s) to track (all tiers with live game data):")
    for p in tracked:
        print(f"     [{p.get('tier','?').upper():6}] {p['sport']} · {p['pickLabel']} "
              f"({p['home']} vs {p['away']}) · {p['score100']}%")

    out = {
        "date":    date_key,
        "logged":  now_utc.isoformat(),
        "has_live_odds": api_key is not None,
        "picks":   tracked,
        "all_picks_count": len(picks),
    }

    out_path = os.path.join(DATA_DIR, f"{date_key}{out_suffix}.json")
    with open(out_path, "w") as f:
        json.dump(out, f, indent=2)
    print(f"\n✅ {len(tracked)} pick(s) saved → {out_path}")

    tier_counts = {}
    for p in tracked:
        t = p.get("tier", "lean")
        tier_counts[t] = tier_counts.get(t, 0) + 1
    print(f"   Breakdown: {tier_counts}")

    sys.exit(0)


if __name__ == "__main__":
    main()
