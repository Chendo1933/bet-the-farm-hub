#!/usr/bin/env python3
"""
Bet The Farm Hub — teamrankings.com ATS + O/U scraper

Scrapes season-long ATS (against-the-spread) and O/U (over/under) records
from teamrankings.com for MLB, NBA, and NFL, then writes
data/ats_refresh.json in the format refresh_ats.py expects and calls
refresh_ats.py to patch index.html.

Why this exists (2026-05-10):
  The previous data flow computed ATS/O-U records from data/results/*.json.
  That archive missed ~5-10 games per team because log_results.py
  doesn't capture every daily slate (odds-API gaps, edge cases in
  schedule fetch). The hub's ATS column drifted further from reality
  every week. teamrankings publishes the authoritative season totals
  on a per-team page that's served as plain HTML — no JS, no auth —
  so we can grab the full slate in one ~30-second HTTP burst per day.

  (Earlier history: a previous version of this script scraped
  bettingpros.com via Playwright + Chromium. That was disabled in commit
  a8da932 in favor of the archive-based approach, which has now also
  proven insufficient. This rewrite drops the browser dependency
  entirely — plain requests + regex — and uses teamrankings instead.)

Coverage:
  - MLB: ATS overall + O/U overall  (in-season → tables present)
  - NBA: same                       (playoffs → tables still served)
  - NFL: same                       (off-season → last regular season's numbers)
  - NHL: not exposed by teamrankings during off-season → fall back to
    update_stats.py's archive-based computation

Output:
  - data/ats_refresh.json — the same format refresh_ats.py has always
    consumed. Only fills aw/al (overall ATS) and ov/un (overall O/U).
    Home/away splits (haw/hal/aaw/aal) are left to update_stats.py's
    archive-based update_ats_ou(), which is still authoritative for
    those columns.

Usage:
  python scripts/scrape_ats.py            # full scrape + patch hub
  python scripts/scrape_ats.py --dry-run  # scrape only, print to stdout

Designed to run in a daily GitHub Actions workflow (~30 seconds end-
to-end). No browser dependency.
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

OUTPUT_PATH = "data/ats_refresh.json"
TIMEOUT     = 20

# Realistic UA — teamrankings serves the same data to any browser-shaped
# request. Using BetTheFarm/x.x in the UA so the operator can identify
# our traffic in their logs if they care to look.
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36 BetTheFarm/1.0"
    ),
}

# Per-sport scraping config:
#   ats_url / ou_url   - pages with season-long totals
#   slug_to_hub        - manual overrides for teamrankings slugs that
#                        don't unambiguously resolve to a hub team name
#                        via the "last word = mascot" heuristic
SPORT_CONFIG = {
    "mlb": {
        "ats_url": "https://www.teamrankings.com/mlb/trends/ats_trends/",
        "ou_url":  "https://www.teamrankings.com/mlb/trends/ou_trends/",
        "slug_to_hub": {
            "chi-sox-white-sox":     "Chicago White Sox",
            "chi-cubs-cubs":         "Chicago Cubs",
            "ny-yankees-yankees":    "New York Yankees",
            "ny-mets-mets":          "New York Mets",
            "la-angels-angels":      "Los Angeles Angels",
            "la-dodgers-dodgers":    "Los Angeles Dodgers",
            "sf-giants-giants":      "San Francisco Giants",
            "sacramento-athletics":  "Athletics",
        },
    },
    "nba": {
        "ats_url": "https://www.teamrankings.com/nba/trends/ats_trends/",
        "ou_url":  "https://www.teamrankings.com/nba/trends/ou_trends/",
        "slug_to_hub": {
            "la-lakers-lakers":       "Los Angeles Lakers",
            "la-clippers-clippers":   "Los Angeles Clippers",
            "ny-knicks-knicks":       "New York Knicks",
        },
    },
    "nfl": {
        "ats_url": "https://www.teamrankings.com/nfl/trends/ats_trends/",
        "ou_url":  "https://www.teamrankings.com/nfl/trends/ou_trends/",
        "slug_to_hub": {
            "la-rams-rams":              "Los Angeles Rams",
            "la-chargers-chargers":      "Los Angeles Chargers",
            "ny-giants-giants":          "New York Giants",
            "ny-jets-jets":              "New York Jets",
        },
    },
}


def fetch(url: str) -> str:
    """Fetch URL, return HTML body. Raises on non-200 or timeout."""
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        return resp.read().decode("utf-8", errors="ignore")


def parse_first_table(html: str) -> list[dict]:
    """
    Pull the first <table>'s data rows. Returns a list of dicts:
      [{slug: "atlanta-braves", text: "Atlanta", cells: [...]}, ...]
    """
    m = re.search(r"<table[^>]*>(.+?)</table>", html, re.DOTALL)
    if not m:
        return []
    rows = re.findall(r"<tr[^>]*>(.+?)</tr>", m.group(1), re.DOTALL)
    out = []
    for row in rows[1:]:  # skip header
        cells_html = re.findall(r"<t[hd][^>]*>(.+?)</t[hd]>", row, re.DOTALL)
        if not cells_html:
            continue
        first = cells_html[0]
        text  = re.sub(r"<[^>]+>", "", first).strip()
        # Extract slug from /[sport]/team/[slug] link
        link = re.search(r'href="https?://[^"]*/(?:mlb|nba|nfl|nhl)/team/([^"/]+)', first)
        slug = link.group(1) if link else None
        cells = [re.sub(r"<[^>]+>", "", c).strip() for c in cells_html]
        out.append({"slug": slug, "text": text, "cells": cells})
    return out


def parse_record(s: str) -> tuple[int, int] | None:
    """
    Parse '28-13-0' → (28, 13). Ignores pushes/ties (third number).
    Returns None if the format isn't recognized — caller should skip.
    """
    parts = s.strip().split("-")
    if len(parts) < 2:
        return None
    try:
        return int(parts[0]), int(parts[1])
    except ValueError:
        return None


# Hub team names per sport — used to validate / disambiguate slug resolution.
# These are the exact strings sitting in index.html's sport arrays at
# column 0. If a team gets renamed in the hub, add it here too.
HUB_TEAMS = {
    "mlb": {
        "Arizona Diamondbacks", "Atlanta Braves", "Baltimore Orioles",
        "Boston Red Sox", "Chicago Cubs", "Chicago White Sox",
        "Cincinnati Reds", "Cleveland Guardians", "Colorado Rockies",
        "Detroit Tigers", "Houston Astros", "Kansas City Royals",
        "Los Angeles Angels", "Los Angeles Dodgers", "Miami Marlins",
        "Milwaukee Brewers", "Minnesota Twins", "New York Mets",
        "New York Yankees", "Athletics", "Philadelphia Phillies",
        "Pittsburgh Pirates", "San Diego Padres", "San Francisco Giants",
        "Seattle Mariners", "St. Louis Cardinals", "Tampa Bay Rays",
        "Texas Rangers", "Toronto Blue Jays", "Washington Nationals",
    },
    "nba": {
        "Atlanta Hawks", "Boston Celtics", "Brooklyn Nets",
        "Charlotte Hornets", "Chicago Bulls", "Cleveland Cavaliers",
        "Dallas Mavericks", "Denver Nuggets", "Detroit Pistons",
        "Golden State Warriors", "Houston Rockets", "Indiana Pacers",
        "Los Angeles Clippers", "Los Angeles Lakers", "Memphis Grizzlies",
        "Miami Heat", "Milwaukee Bucks", "Minnesota Timberwolves",
        "New Orleans Pelicans", "New York Knicks", "Oklahoma City Thunder",
        "Orlando Magic", "Philadelphia 76ers", "Phoenix Suns",
        "Portland Trail Blazers", "Sacramento Kings", "San Antonio Spurs",
        "Toronto Raptors", "Utah Jazz", "Washington Wizards",
    },
    "nfl": {
        "Arizona Cardinals", "Atlanta Falcons", "Baltimore Ravens",
        "Buffalo Bills", "Carolina Panthers", "Chicago Bears",
        "Cincinnati Bengals", "Cleveland Browns", "Dallas Cowboys",
        "Denver Broncos", "Detroit Lions", "Green Bay Packers",
        "Houston Texans", "Indianapolis Colts", "Jacksonville Jaguars",
        "Kansas City Chiefs", "Las Vegas Raiders", "Los Angeles Chargers",
        "Los Angeles Rams", "Miami Dolphins", "Minnesota Vikings",
        "New England Patriots", "New Orleans Saints", "New York Giants",
        "New York Jets", "Philadelphia Eagles", "Pittsburgh Steelers",
        "San Francisco 49ers", "Seattle Seahawks", "Tampa Bay Buccaneers",
        "Tennessee Titans", "Washington Commanders",
    },
}


def resolve_hub_name(slug: str | None, sport_cfg: dict, hub_names: set[str]) -> str | None:
    """
    Map a teamrankings slug like 'chi-sox-white-sox' to a hub team name
    like 'Chicago White Sox'. Uses manual overrides first, then falls
    back to a "last-word(s) = mascot" heuristic.

    Two-pass match strategy: try increasingly long mascot suffixes
    (1 word, then 2, then 3). Return ONLY when exactly one hub team
    matches — never return a non-unique match. This protects against
    suffix collisions like 'sox' matching both Boston Red Sox and
    Chicago White Sox, which (before this fix, 2026-05-10) caused
    Boston's scraped ATS to land in the White Sox row whenever set
    iteration ordered White Sox first. Mascot 'red sox' (take=2) is
    unique to Boston; mascot 'white sox' is unique to Chicago.

    Same pattern: NFL 'jets' / 'giants' would collide if both NY teams
    had identical mascots — they don't here, but adding the require-
    unique-match rule means we'd never silently misroute them either.
    """
    if not slug:
        return None
    overrides = sport_cfg.get("slug_to_hub", {})
    if slug in overrides:
        return overrides[slug]
    parts = slug.split("-")
    for take in range(1, min(4, len(parts) + 1)):
        mascot = " ".join(parts[-take:]).lower()
        matches = [n for n in hub_names if n.lower().endswith(mascot)]
        if len(matches) == 1:
            return matches[0]
        # Multiple matches → try longer suffix. Zero matches → also try longer
        # (some slugs are city-only like 'cincinnati-reds' where mascot = 'reds'
        # finds Cincinnati Reds uniquely — but if not we keep trying).
    return None


def scrape_sport(sport: str) -> list[dict]:
    """
    Scrape both ATS and O/U pages for one sport, merge by team, return
    a list of entries ready to drop into ats_refresh.json's sport array.
    Logs any teams that fail to resolve so we can fix slug_to_hub.
    """
    cfg = SPORT_CONFIG[sport]
    hub_names = HUB_TEAMS[sport]
    print(f"\n── {sport.upper()} ─────────────────────────────────────────────")

    # ATS table → {hub_name: (aw, al)}
    print(f"  fetching {cfg['ats_url']}")
    try:
        ats_rows = parse_first_table(fetch(cfg["ats_url"]))
    except urllib.error.HTTPError as e:
        print(f"  ✗ HTTP {e.code} on ATS page — skipping {sport.upper()}")
        return []
    except Exception as e:
        print(f"  ✗ Fetch failed: {type(e).__name__}: {e} — skipping {sport.upper()}")
        return []
    if not ats_rows:
        print(f"  ✗ No ATS table found for {sport.upper()} (off-season?)")
        return []

    ats_by_hub: dict[str, tuple[int, int]] = {}
    unresolved: list[str] = []
    for row in ats_rows:
        hub = resolve_hub_name(row["slug"], cfg, hub_names)
        if not hub:
            unresolved.append(row["slug"] or row["text"])
            continue
        rec = parse_record(row["cells"][1] if len(row["cells"]) > 1 else "")
        if rec:
            ats_by_hub[hub] = rec
    if unresolved:
        print(f"  ⚠  Unresolved ATS slugs (add to slug_to_hub): {unresolved}")
    print(f"  ✓ ATS: {len(ats_by_hub)} teams resolved")

    # O/U table → {hub_name: (ov, un)}
    print(f"  fetching {cfg['ou_url']}")
    try:
        ou_rows = parse_first_table(fetch(cfg["ou_url"]))
    except urllib.error.HTTPError as e:
        print(f"  ⚠  HTTP {e.code} on O/U page — ATS-only for {sport.upper()}")
        ou_rows = []
    except Exception as e:
        print(f"  ⚠  O/U fetch failed: {type(e).__name__} — ATS-only for {sport.upper()}")
        ou_rows = []

    ou_by_hub: dict[str, tuple[int, int]] = {}
    for row in ou_rows:
        hub = resolve_hub_name(row["slug"], cfg, hub_names)
        if not hub:
            continue
        rec = parse_record(row["cells"][1] if len(row["cells"]) > 1 else "")
        if rec:
            ou_by_hub[hub] = rec
    if ou_rows:
        print(f"  ✓ O/U: {len(ou_by_hub)} teams resolved")

    # Merge — every team in ats_by_hub gets an entry (O/U optional).
    out = []
    for hub_name, (aw, al) in ats_by_hub.items():
        entry = {"team": hub_name, "aw": aw, "al": al}
        if hub_name in ou_by_hub:
            ov, un = ou_by_hub[hub_name]
            entry["ov"] = ov
            entry["un"] = un
        out.append(entry)
    return out


def main():
    dry_run = "--dry-run" in sys.argv
    print(f"[scrape_ats] {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")
    if dry_run:
        print("  (dry-run mode — no files written, no hub patched)\n")

    payload = {
        "source": f"teamrankings.com — {datetime.now(timezone.utc).strftime('%Y-%m-%d')}",
        "as_of":  datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "sports": {},
    }
    for sport in SPORT_CONFIG.keys():
        entries = scrape_sport(sport)
        if entries:
            payload["sports"][sport] = entries

    if not payload["sports"]:
        print("\n✗ No data scraped from any sport — refusing to overwrite ats_refresh.json")
        sys.exit(1)

    print(f"\nTotal sports scraped: {list(payload['sports'].keys())}")
    if dry_run:
        print("\n--- payload preview ---")
        print(json.dumps(payload, indent=2)[:1500])
        print("...")
        return

    Path(OUTPUT_PATH).parent.mkdir(parents=True, exist_ok=True)
    Path(OUTPUT_PATH).write_text(json.dumps(payload, indent=2))
    print(f"\n✓ Wrote {OUTPUT_PATH}")

    # Now apply to the hub via refresh_ats.py
    print(f"\n→ Applying to index.html via refresh_ats.py…")
    result = subprocess.run(
        [sys.executable, "scripts/refresh_ats.py", OUTPUT_PATH],
        capture_output=False,
    )
    if result.returncode != 0:
        sys.exit(f"✗ refresh_ats.py exited with {result.returncode}")
    print("\n[scrape_ats] Done ✓")


if __name__ == "__main__":
    main()
