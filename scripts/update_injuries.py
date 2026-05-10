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

# ESPN team display name → hub DB team name. Only non-exact matches.
# Validated against parsed hub team rows on 2026-04-26.
#
# Removed/fixed (2026-04-26):
#   "Los Angeles Clippers" → "LA Clippers"  — hub row is "Los Angeles Clippers"
#     since scrape-ats ran. Old map was writing INJURIES["LA Clippers"] which
#     the hub's pick scoring (which looks up INJURIES[team_row_name]) never
#     found, so Clippers were treated as injury-free for weeks.
#   "Utah Hockey Club" → "Utah Hockey Club"  — was a no-op identity, but Utah
#     renamed mid-season and the hub row is now "Utah Mammoth". ESPN still
#     returns "Utah Hockey Club" so we remap to the new hub name.
ESPN_NAME_MAP = {
    "Montréal Canadiens":    "Montreal Canadiens",
    "Utah Hockey Club":      "Utah Mammoth",
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
    """Fetch and parse ESPN injuries for one sport. Returns {team_name: [entries]}.

    NOTE on the ESPN response shape (verified 2026-05-10):
      Each top-level injuries[] entry now has `displayName` at the entry root
      (NOT nested under entry.team.displayName as it was pre-2026-04-16).
      Each nested injury has `status` at the root (not `type.description`)
      and the human-readable blurb is in `shortComment` / `longComment`
      (not `details.detail` or `location`).

      On 2026-04-16, ESPN restructured this endpoint. Our parser was reading
      the old paths, so every team name resolved to "" and the entire
      INJURIES object collapsed to one empty-string key. F7 (the injury
      scoring factor, 18% weight) silently went to zero for ~25 days
      until the audit on 2026-05-10 surfaced it.
    """
    data = espn_get(url)
    if not data:
        return {}

    team_list = data.get("injuries", [])
    result = {}

    for entry in team_list:
        # New shape: displayName is at the entry root.
        # Old shape (kept as fallback for safety): entry.team.displayName.
        espn_name = entry.get("displayName") or entry.get("team", {}).get("displayName") or ""
        if not espn_name:
            continue   # CRITICAL: never write under an empty key (see docstring)
        hub_name = ESPN_NAME_MAP.get(espn_name, espn_name)

        players = []
        for inj in entry.get("injuries", []):
            athlete = inj.get("athlete", {})
            pname   = athlete.get("displayName", "")
            if not pname:
                continue

            # Status — new shape puts it at inj.status; old shape had
            # inj.type.description. Try new first, fall back to old.
            raw_status = (
                inj.get("status")
                or inj.get("type", {}).get("description", "")
            )
            hub_status = STATUS_MAP.get(raw_status)
            if hub_status not in INCLUDE_STATUSES:
                continue

            # Detail — new shape: shortComment / longComment. Old: details.detail.
            # Prefer shortComment because it's already concise. Skip the single-
            # word case ("out") since it duplicates the status field.
            short = (inj.get("shortComment") or "").strip()
            long_ = (inj.get("longComment") or "").strip()
            old_detail = inj.get("details", {}).get("detail", "") or inj.get("location", "")
            detail = ""
            if short and len(short) > 6 and short.lower() != "out":
                detail = short
            elif long_ and len(long_) > 6 and long_.lower() != "out":
                detail = long_
            elif old_detail:
                detail = old_detail
            # Trim to first sentence to keep the hub display compact
            detail = detail.split(".")[0].strip()

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
    # Belt-and-suspenders: never write entries under an empty or whitespace-only
    # team name. The fetch loop already guards against this, but if ESPN's
    # schema shifts again we want a second line of defense rather than another
    # silent F7-dead-for-25-days incident.
    stripped = {k: v for k, v in all_injuries.items() if k and k.strip()}
    if len(stripped) != len(all_injuries):
        dropped = len(all_injuries) - len(stripped)
        print(f"  ⚠  Dropped {dropped} entries with empty team names")
    return stripped


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

    # Sanity gate. NBA + NHL + MLB together always produce 30+ teams with at
    # least one player each during active seasons. If we have fewer than 10,
    # the response was probably malformed (or ESPN restructured the schema
    # again) — refuse to clobber the existing hub data, just like the empty
    # case. This is the canary that would have caught the 2026-04-16 bug
    # within a single workflow run rather than 25 days later.
    if len(injuries) < 10:
        print(f"  ✗ Only {len(injuries)} team(s) with injuries — refusing to patch the hub")
        print(f"    Sample keys: {list(injuries.keys())[:5]}")
        print(f"    Suspected cause: ESPN response schema change. Investigate manually.")
        sys.exit(1)

    patch_hub(injuries)
    print("[update_injuries] Done ✓")


if __name__ == "__main__":
    main()
