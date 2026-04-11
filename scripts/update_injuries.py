#!/usr/bin/env python3
"""
Bet The Farm Hub — Daily injury updater
Fetches current injury reports from ESPN's public API for NBA, NHL, and MLB,
formats them to match the hub's INJURIES object, and patches index.html.

Runs as part of the daily-update.yml workflow (after update_stats.py).
"""

import re
import sys
import json
import requests

HUB_FILE = "index.html"
TIMEOUT  = 15
HEADERS  = {"User-Agent": "Mozilla/5.0 (BetTheFarm/1.0 injury-updater)"}

# ESPN sport/league → API path
SPORT_ENDPOINTS = {
    "nba": "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/injuries",
    "nhl": "https://site.api.espn.com/apis/site/v2/sports/hockey/nhl/injuries",
    "mlb": "https://site.api.espn.com/apis/site/v2/sports/baseball/mlb/injuries",
}

# ESPN team display name → hub DB team name (only non-exact matches needed)
ESPN_NAME_MAP = {
    "Los Angeles Clippers":  "LA Clippers",
    "Montréal Canadiens":    "Montreal Canadiens",
    "Utah Hockey Club":      "Utah Hockey Club",
    "Oakland Athletics":     "Athletics",
}

# ESPN status → hub status. Statuses not in this map are skipped (Probable, GTD w/o action).
STATUS_MAP = {
    "Out":            "Out",
    "IR":             "Out",           # NBA/MLB IR = effectively Out for betting purposes
    "Injured Reserve":"Injured Reserve",  # NHL IR (kept distinct — hub counts it)
    "Day-To-Day":     "Day-To-Day",
    "Questionable":   "Day-To-Day",    # treat Questionable same as D2D
    "Doubtful":       "Out",           # Doubtful rarely plays — treat as Out
}

# Only carry players whose mapped status is one of these into the hub
INCLUDE_STATUSES = {"Out", "Day-To-Day", "Injured Reserve"}


def espn_get(url):
    try:
        r = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        print(f"  ⚠  fetch failed: {url}\n     {e}")
        return None


def fetch_sport_injuries(sport, url):
    """Fetch and parse ESPN injuries for one sport. Returns {team_name: [entries]}."""
    data = espn_get(url)
    if not data:
        return {}

    # ESPN injuries response: top-level "injuries" list, each item = one team
    team_list = data.get("injuries", [])
    result = {}

    for entry in team_list:
        team_info = entry.get("team", {})
        espn_name = team_info.get("displayName", "")
        hub_name  = ESPN_NAME_MAP.get(espn_name, espn_name)

        players = []
        for inj in entry.get("injuries", []):
            # Player name
            athlete  = inj.get("athlete", {})
            pname    = athlete.get("displayName", "")
            if not pname:
                continue

            # Status — ESPN puts it in type.description or a top-level "status" field
            raw_status = (
                inj.get("type", {}).get("description", "")
                or inj.get("status", "")
            )
            hub_status = STATUS_MAP.get(raw_status)
            if hub_status not in INCLUDE_STATUSES:
                continue   # skip Probable, GTD, etc.

            # Detail — ESPN puts it in details.detail, location, or shortComment
            details = inj.get("details", {})
            detail  = (
                details.get("detail", "")
                or inj.get("location", "")
                or inj.get("shortComment", "")
                or ""
            )
            # Trim long comments to just the first sentence / injury type
            detail = detail.split(".")[0].strip()
            # For NHL, keep "Injured Reserve" as the detail if no injury info
            if not detail and hub_status == "Injured Reserve":
                detail = ""

            position = athlete.get("position", {}).get("abbreviation", "")
            players.append({"player": pname, "status": hub_status, "detail": detail, "pos": position})

        if players:
            result[hub_name] = players

    return result


def build_injuries_object():
    """Fetch all sports and merge into one INJURIES dict."""
    all_injuries = {}
    for sport, url in SPORT_ENDPOINTS.items():
        print(f"  Fetching {sport.upper()} injuries…")
        sport_inj = fetch_sport_injuries(sport, url)
        count = sum(len(v) for v in sport_inj.values())
        print(f"    → {len(sport_inj)} teams, {count} players")
        all_injuries.update(sport_inj)
    return all_injuries


def patch_hub(injuries: dict):
    """Replace the INJURIES constant in index.html with fresh data."""
    try:
        with open(HUB_FILE, "r", encoding="utf-8") as f:
            html = f.read()
    except FileNotFoundError:
        print(f"  ✗ {HUB_FILE} not found — run from the repo root")
        sys.exit(1)

    new_json  = json.dumps(injuries, separators=(",", ":"))
    new_block = f"let INJURIES={new_json};"

    # Match the existing INJURIES declaration (handles multiline edge cases)
    pattern = r"let INJURIES=\{.*?\};"
    if not re.search(pattern, html, re.DOTALL):
        print("  ✗ Could not find INJURIES declaration in hub — skipping patch")
        sys.exit(1)

    patched = re.sub(pattern, new_block, html, count=1, flags=re.DOTALL)

    with open(HUB_FILE, "w", encoding="utf-8") as f:
        f.write(patched)

    total = sum(len(v) for v in injuries.values())
    print(f"  ✓ Patched {HUB_FILE}: {len(injuries)} teams, {total} players")


def main():
    print("[update_injuries] Fetching injury reports from ESPN…")
    injuries = build_injuries_object()

    if not injuries:
        print("  ⚠  No injury data fetched — skipping hub patch to avoid clearing existing data")
        sys.exit(0)

    patch_hub(injuries)
    print("[update_injuries] Done ✓")


if __name__ == "__main__":
    main()
