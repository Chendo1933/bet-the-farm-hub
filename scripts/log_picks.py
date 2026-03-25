#!/usr/bin/env python3
"""
Bet The Farm Hub — Daily pick logger
Uses Playwright to headlessly load the hub, inject the Odds API key,
wait for live picks to generate, then save Elite + Strong picks to
data/picks/YYYY-MM-DD.json for nightly performance grading.

Required GitHub Secret: ODDS_API_KEY (your the-odds-api.com key)
If the secret isn't set, falls back to static ATS-trend picks.

Run before games start (~noon ET) via log-picks.yml workflow.
"""

import asyncio
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone

DATA_DIR  = "data/picks"
HUB_FILE  = "index.html"
PORT      = 8181
TIMEOUT   = 45_000   # 45s — allow time for ESPN + Odds API calls


async def scrape_picks(api_key: str | None) -> list[dict]:
    from playwright.async_api import async_playwright

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context()

        # Inject Odds API key into localStorage before the page loads
        # The hub reads localStorage.btf_odds_key on startup and auto-refreshes
        if api_key:
            await context.add_init_script(
                f"localStorage.setItem('btf_odds_key', '{api_key}');"
            )
            print(f"  ✓ Odds API key injected ({api_key[:6]}…)")
        else:
            print("  ⚠  No ODDS_API_KEY — will capture static ATS-trend picks only")

        page = await context.new_page()

        # Suppress console noise from the hub
        page.on("console", lambda m: None)
        page.on("pageerror", lambda e: print(f"  page error: {e}"))

        await page.goto(f"http://localhost:{PORT}/{HUB_FILE}", wait_until="domcontentloaded")
        print(f"  ✓ Hub loaded at localhost:{PORT}")

        if api_key:
            # Wait for TODAY_GAMES to populate (odds refresh fires automatically)
            print("  ⏳ Waiting for live odds to load…")
            try:
                await page.wait_for_function(
                    "Array.isArray(window.TODAY_GAMES) && window.TODAY_GAMES.length > 0",
                    timeout=TIMEOUT
                )
                game_count = await page.evaluate("window.TODAY_GAMES.length")
                print(f"  ✓ {game_count} game(s) loaded from ESPN/Odds API")
            except Exception:
                print("  ⚠  Timed out waiting for live games — using static picks")

        # Click the Hot Bets tab to trigger hbRender() + BTF_PICKS export
        await page.click("button[onclick*=\"showTab('hotbets'\"]")
        print("  ✓ Hot Bets tab activated")

        # Wait for BTF_PICKS_READY flag we added to hbRender()
        await page.wait_for_function("window.BTF_PICKS_READY === true", timeout=10_000)

        picks = await page.evaluate("window.BTF_PICKS")
        print(f"  ✓ {len(picks)} total picks generated")

        await browser.close()
        return picks


def main():
    now_utc  = datetime.now(timezone.utc)
    date_key = now_utc.strftime("%Y-%m-%d")
    api_key  = os.environ.get("ODDS_API_KEY", "").strip() or None

    print(f"[log_picks] {now_utc.strftime('%Y-%m-%d %H:%M UTC')}")
    os.makedirs(DATA_DIR, exist_ok=True)

    # Start a local HTTP server so the hub can make same-origin API calls
    server = subprocess.Popen(
        [sys.executable, "-m", "http.server", str(PORT)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    time.sleep(1.5)

    try:
        picks = asyncio.run(scrape_picks(api_key))
    finally:
        server.terminate()

    # Filter to Elite + Strong only WITH actual game data (atsPick set, opponent known)
    # Picks with atsPick=null are ATS-trend picks (no live matchup) — ungradeable
    tracked = [
        p for p in picks
        if p.get("tier") in ("elite", "strong")
        and p.get("atsPick") is not None
        and p.get("away", "").strip() != ""
    ]
    print(f"\n  📊 {len(tracked)} Elite/Strong pick(s) to track:")
    for p in tracked:
        print(f"     [{p['tier'].upper():6}] {p['sport']} · {p['pickLabel']} "
              f"({p['home']} vs {p['away']}) · {p['score100']}%")

    out = {
        "date":    date_key,
        "logged":  now_utc.isoformat(),
        "has_live_odds": api_key is not None,
        "picks":   tracked,
        "all_picks_count": len(picks),
    }

    out_path = os.path.join(DATA_DIR, f"{date_key}.json")
    with open(out_path, "w") as f:
        json.dump(out, f, indent=2)
    print(f"\n✅ {len(tracked)} pick(s) saved → {out_path}")

    if not tracked:
        print("ℹ  No Elite/Strong picks today — nothing to grade later")

    sys.exit(0)


if __name__ == "__main__":
    main()
