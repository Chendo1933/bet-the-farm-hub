#!/usr/bin/env python3
"""
Bet The Farm Hub — BettingPros ATS/O-U scraper
Scrapes bettingpros.com for all team ATS and O/U records (overall, home, away)
for NBA and NHL, then writes data/ats_refresh.json and runs refresh_ats.py.

Usage:
    python scripts/scrape_ats.py            # full scrape + patch hub
    python scripts/scrape_ats.py --dry-run  # scrape only, don't patch

Scraped data per team (NBA):  aw, al, haw, hal, aaw, aal, ov, un
Scraped data per team (NHL):  plw, pll, ov, un  (puck line; home/away not available)

Disambiguation:
  "Los Angeles" NBA  → Lakers (48+ wins) vs Clippers (38 wins) by W-L record
  "New York"    NHL  → Islanders (41+ wins) vs Rangers (29 wins) by W-L record
"""

import asyncio
import json
import os
import re
import subprocess
import sys
from datetime import date

# ── Hub DB names ──────────────────────────────────────────────────────────────
# Maps (sport, bettingpros_display_name) → hub DB team name.
# For teams that share a city, uses (sport, display_name, wl_wins) to disambiguate.

NBA_NAME_MAP = {
    "Atlanta":       "Atlanta Hawks",
    "Boston":        "Boston Celtics",
    "Brooklyn":      "Brooklyn Nets",
    "Charlotte":     "Charlotte Hornets",
    "Chicago":       "Chicago Bulls",
    "Cleveland":     "Cleveland Cavaliers",
    "Dallas":        "Dallas Mavericks",
    "Denver":        "Denver Nuggets",
    "Detroit":       "Detroit Pistons",
    "Golden State":  "Golden State Warriors",
    "Houston":       "Houston Rockets",
    "Indiana":       "Indiana Pacers",
    "Memphis":       "Memphis Grizzlies",
    "Miami":         "Miami Heat",
    "Milwaukee":     "Milwaukee Bucks",
    "Minnesota":     "Minnesota Timberwolves",
    "New Orleans":   "New Orleans Pelicans",
    "New York":      "New York Knicks",       # only one NBA "New York"
    "Oklahoma City": "Oklahoma City Thunder",
    "Orlando":       "Orlando Magic",
    "Philadelphia":  "Philadelphia 76ers",
    "Phoenix":       "Phoenix Suns",
    "Portland":      "Portland Trail Blazers",
    "Sacramento":    "Sacramento Kings",
    "San Antonio":   "San Antonio Spurs",
    "Toronto":       "Toronto Raptors",
    "Utah":          "Utah Jazz",
    "Washington":    "Washington Wizards",
    # Ambiguous — resolved by W-L wins below
    # "Los Angeles"  → Lakers (higher wins) or Clippers (lower wins)
}

NHL_NAME_MAP = {
    "Anaheim":       "Anaheim Ducks",
    "Boston":        "Boston Bruins",
    "Buffalo":       "Buffalo Sabres",
    "Calgary":       "Calgary Flames",
    "Carolina":      "Carolina Hurricanes",
    "Chicago":       "Chicago Blackhawks",
    "Colorado":      "Colorado Avalanche",
    "Columbus":      "Columbus Blue Jackets",
    "Dallas":        "Dallas Stars",
    "Detroit":       "Detroit Red Wings",
    "Edmonton":      "Edmonton Oilers",
    "Florida":       "Florida Panthers",
    "Minnesota":     "Minnesota Wild",
    "Montreal":      "Montreal Canadiens",
    "Nashville":     "Nashville Predators",
    "New Jersey":    "New Jersey Devils",
    "Ottawa":        "Ottawa Senators",
    "Philadelphia":  "Philadelphia Flyers",
    "Pittsburgh":    "Pittsburgh Penguins",
    "San Jose":      "San Jose Sharks",
    "Seattle":       "Seattle Kraken",
    "St. Louis":     "St. Louis Blues",
    "Tampa Bay":     "Tampa Bay Lightning",
    "Toronto":       "Toronto Maple Leafs",
    "Utah":          "Utah Mammoth",
    "Vancouver":     "Vancouver Canucks",
    "Vegas":         "Vegas Golden Knights",
    "Washington":    "Washington Capitals",
    "Winnipeg":      "Winnipeg Jets",
    # Ambiguous — resolved by W-L wins below
    # "Los Angeles"  → Kings (hub name)
    # "New York"     → Islanders (higher wins) or Rangers (lower wins)
}


def resolve_nba_name(display: str, wl_wins: int) -> str | None:
    if display == "Los Angeles":
        return "Los Angeles Lakers" if wl_wins >= 45 else "Los Angeles Clippers"
    return NBA_NAME_MAP.get(display)


def resolve_nhl_name(display: str, wl_wins: int) -> str | None:
    if display == "Los Angeles":
        return "Los Angeles Kings"
    if display == "New York":
        return "New York Islanders" if wl_wins >= 38 else "New York Rangers"
    return NHL_NAME_MAP.get(display)


# ── JS extractor ──────────────────────────────────────────────────────────────
EXTRACT_JS = """
(() => {
    const rows = [...document.querySelectorAll('tr')].slice(1);
    return rows.map(tr => {
        const cells = [...tr.querySelectorAll('td')];
        return cells.map(td => td.textContent.trim());
    }).filter(r => r.length >= 2);
})()
"""

OPEN_FILTER_JS = """
() => {
    // Find the filter pill button (shows current selection: 'All Games', 'Home', etc.)
    const btns = [...document.querySelectorAll('button')];
    const filterBtn = btns.find(b =>
        ['All Games','Home','Away'].includes(b.textContent.trim())
    );
    if (filterBtn) { filterBtn.click(); return true; }
    return false;
}
"""

CLICK_FILTER_JS = """
(filterLabel) => {
    const items = [...document.querySelectorAll('ul li, li[role="option"]')];
    const target = items.find(el => el.textContent.trim() === filterLabel);
    if (target) { target.click(); return true; }
    return false;
}
"""


def parse_record(record_str: str) -> tuple[int, int]:
    """Parse '43-29-1' or '43-29' → (wins, losses), ignoring pushes."""
    parts = record_str.strip().split("-")
    try:
        return int(parts[0]), int(parts[1])
    except (IndexError, ValueError):
        return 0, 0


def parse_wl(team_cell: str) -> tuple[str, int]:
    """Parse 'Charlotte(39-34)' → ('Charlotte', 39)."""
    m = re.match(r"^(.+?)\((\d+)-\d+\)", team_cell)
    if m:
        return m.group(1).strip(), int(m.group(2))
    return team_cell.strip(), 0


def rows_to_dict(rows: list, sport: str, filter_name: str) -> dict:
    """Convert extracted table rows to {hub_db_name: (wins, losses)}."""
    result = {}
    resolve = resolve_nba_name if sport == "nba" else resolve_nhl_name
    for row in rows:
        if len(row) < 2:
            continue
        display, wl_wins = parse_wl(row[0])
        hub_name = resolve(display, wl_wins)
        if not hub_name:
            print(f"  [WARN] {sport.upper()} {filter_name}: no DB match for '{display}' (W={wl_wins})")
            continue
        w, l = parse_record(row[1])
        result[hub_name] = (w, l)
    return result


async def apply_filter(page, label: str):
    """Open the game filter dropdown and select a filter option."""
    await page.evaluate(OPEN_FILTER_JS)
    await page.wait_for_timeout(400)
    await page.evaluate(CLICK_FILTER_JS, label)
    await page.wait_for_timeout(1200)


async def scrape_sport(page, sport: str) -> dict:
    """Scrape ATS (all/home/away) and O/U (all) for one sport. Returns merged team dict."""
    base = f"https://www.bettingpros.com/{sport}"
    teams = {}

    # ── ATS All Games ──
    # Use "load" instead of "networkidle" — bettingpros has persistent ad traffic
    # that prevents networkidle from ever firing. Table data is in the initial HTML.
    await page.goto(f"{base}/against-the-spread-standings/", wait_until="load", timeout=45000)
    await page.wait_for_selector("tr td", timeout=20000)
    rows = await page.evaluate(EXTRACT_JS)
    ats_all = rows_to_dict(rows, sport, "ATS-All")
    print(f"  {sport.upper()} ATS All:  {len(ats_all)} teams")

    # ── ATS Home ──
    await apply_filter(page, "Home")
    rows = await page.evaluate(EXTRACT_JS)
    ats_home = rows_to_dict(rows, sport, "ATS-Home")
    print(f"  {sport.upper()} ATS Home: {len(ats_home)} teams")

    # ── ATS Away ──
    await apply_filter(page, "Away")
    rows = await page.evaluate(EXTRACT_JS)
    ats_away = rows_to_dict(rows, sport, "ATS-Away")
    print(f"  {sport.upper()} ATS Away: {len(ats_away)} teams")

    # ── O/U All Games ──
    await page.goto(f"{base}/over-under-standings/", wait_until="load", timeout=45000)
    await page.wait_for_selector("tr td", timeout=20000)
    rows = await page.evaluate(EXTRACT_JS)
    ou_all = rows_to_dict(rows, sport, "O/U-All")
    print(f"  {sport.upper()} O/U  All:  {len(ou_all)} teams")

    # ── Merge ──
    all_teams = set(ats_all) | set(ats_home) | set(ats_away) | set(ou_all)
    for team in all_teams:
        aw, al   = ats_all.get(team,  (0, 0))
        haw, hal = ats_home.get(team, (0, 0))
        aaw, aal = ats_away.get(team, (0, 0))
        ov, un   = ou_all.get(team,   (0, 0))
        teams[team] = dict(aw=aw, al=al, haw=haw, hal=hal, aaw=aaw, aal=aal, ov=ov, un=un)

    return teams


async def run_scrape() -> dict:
    from playwright.async_api import async_playwright

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) "
                       "Chrome/124.0.0.0 Safari/537.36"
        )
        page = await context.new_page()

        print("Scraping NBA...")
        nba = await scrape_sport(page, "nba")

        print("Scraping NHL...")
        nhl_raw = await scrape_sport(page, "nhl")
        # NHL uses plw/pll field names (puck line) — rename aw/al
        nhl = {}
        for team, d in nhl_raw.items():
            nhl[team] = dict(plw=d["aw"], pll=d["al"], ov=d["ov"], un=d["un"])

        await browser.close()
        return {"nba": nba, "nhl": nhl}


def build_json(data: dict) -> dict:
    today = date.today().isoformat()
    out = {
        "source": "bettingpros.com",
        "season": "2025-26",
        "as_of": today,
        "scraped_by": "scripts/scrape_ats.py (automated)",
        "_note": "NBA includes home/away ATS splits. NHL puck-line only (home/away not on site).",
        "sports": {
            "nba": [{"team": t, **v} for t, v in sorted(data["nba"].items())],
            "nhl": [{"team": t, **v} for t, v in sorted(data["nhl"].items())],
        }
    }
    return out


def main():
    dry_run = "--dry-run" in sys.argv
    out_path = "data/ats_refresh.json"

    print("=" * 60)
    print("BettingPros ATS Scraper")
    print("=" * 60)

    data = asyncio.run(run_scrape())

    # Summarise
    nba_count = len(data["nba"])
    nhl_count = len(data["nhl"])
    print(f"\nScraped: {nba_count}/30 NBA teams, {nhl_count}/32 NHL teams")

    payload = build_json(data)

    if dry_run:
        print("\n[DRY RUN] Would write:")
        print(json.dumps(payload, indent=2)[:2000])
        return

    os.makedirs("data", exist_ok=True)
    with open(out_path, "w") as f:
        json.dump(payload, f, indent=2)
    print(f"\nWrote {out_path}")

    # Run the patcher
    print("\nPatching hub...")
    result = subprocess.run(
        [sys.executable, "scripts/refresh_ats.py", out_path],
        capture_output=True, text=True
    )
    print(result.stdout)
    if result.returncode != 0:
        print("STDERR:", result.stderr, file=sys.stderr)
        sys.exit(result.returncode)


if __name__ == "__main__":
    main()
