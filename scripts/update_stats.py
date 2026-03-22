#!/usr/bin/env python3
"""
Bet The Farm Hub — Daily stats auto-updater
Fetches current W/L + PPG/scoring data from ESPN's public API and patches
the NBA and NHL arrays inside index.html.  Only commits if values changed.

Array index reference (0-based):
  NBA:  0=name 1=conf 2=div 3=W 4=L  ... 15=ppg 16=papg
  NHL:  0=name 1=conf 2=div 3=W 4=L 5=OTL ... 16=gf 17=ga
"""

import re
import sys
import json
import requests
from datetime import datetime, timezone

HUB_FILE = "index.html"
TIMEOUT  = 15
HEADERS  = {"User-Agent": "Mozilla/5.0 (BetTheFarm/1.0 daily-updater)"}

# ESPN name → hub name corrections (only non-exact matches needed)
ESPN_NAME_MAP = {
    # NBA
    "Los Angeles Clippers": "LA Clippers",
    # NHL
    "Montréal Canadiens":   "Montreal Canadiens",
    "St. Louis Blues":      "St. Louis Blues",   # already matches, just in case
}

# ── helpers ───────────────────────────────────────────────────────────────────

def espn_get(url):
    try:
        r = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        print(f"  ⚠  fetch failed: {url}\n     {e}")
        return None


def stat_map(stats_list):
    """Convert ESPN stats list [{name, value}] to a plain dict."""
    return {s.get("name", ""): s.get("value") for s in (stats_list or [])}


def _parse_js_row(line):
    """
    Parse one JS array row, e.g.: ["OKC","West","NW",47,15,26,33,2,...]
    Returns list of raw string tokens (quoted strings keep their quotes).
    """
    m = re.search(r'\[(.+)\]', line)
    if not m:
        return None
    parts, cur, in_q = [], "", False
    for c in m.group(1):
        if c == '"':
            in_q = not in_q
            cur += c
        elif c == "," and not in_q:
            parts.append(cur)
            cur = ""
        else:
            cur += c
    if cur:
        parts.append(cur)
    return parts


def patch_rows(html_lines, team_name, idx_vals):
    """
    Find the JS row for team_name and update the given {index: value} pairs.
    Returns number of rows changed (0 or 1).
    """
    for i, line in enumerate(html_lines):
        s = line.strip()
        if not s.startswith(f'["{team_name}"'):
            continue
        parts = _parse_js_row(s)
        if not parts:
            continue
        before = parts[:]
        for idx, val in idx_vals.items():
            if idx < len(parts):
                parts[idx] = f"{val:.1f}" if isinstance(val, float) else str(int(val))
        if parts == before:
            return 0
        indent   = len(line) - len(line.lstrip())
        trailing = "," if s.endswith(",") else ""
        html_lines[i] = " " * indent + "[" + ",".join(parts) + "]" + trailing
        return 1
    return 0   # team not found in hub


# ── NBA ───────────────────────────────────────────────────────────────────────
#   W=3  L=4  ppg=15  papg=16

NBA_URLS = [
    "https://site.api.espn.com/apis/v2/sports/basketball/nba/standings",
    "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/standings",
]

def fetch_nba_entries():
    for url in NBA_URLS:
        d = espn_get(url)
        if not d:
            continue
        # Layout 1: flat entries list
        entries = d.get("standings", {}).get("entries", [])
        # Layout 2: entries nested under conference children
        if not entries:
            for child in d.get("children", []):
                entries += child.get("standings", {}).get("entries", [])
        if entries:
            print(f"  ✓ NBA standings: {len(entries)} teams")
            return entries
    print("  ✗ NBA standings: no data")
    return []


def update_nba(html_lines):
    print("\n── NBA ──────────────────────────────────────────────────────────")
    total = 0
    for entry in fetch_nba_entries():
        raw   = entry.get("team", {}).get("displayName", "")
        name  = ESPN_NAME_MAP.get(raw, raw)
        sm    = stat_map(entry.get("stats", []))

        w  = sm.get("wins",   sm.get("OTWins",  0)) or 0
        l  = sm.get("losses", sm.get("OTLosses",0)) or 0
        gp = sm.get("gamesPlayed") or (w + l) or 1

        # ESPN may give total-points or per-game; handle both
        pf_total = sm.get("pointsFor",      0) or 0
        pa_total = sm.get("pointsAgainst",  0) or 0
        ppg  = round(pf_total / gp, 1) if pf_total else sm.get("avgPointsFor",  0) or 0
        papg = round(pa_total / gp, 1) if pa_total else sm.get("avgPointsAgainst", 0) or 0

        updates = {3: int(w), 4: int(l)}
        if ppg:  updates[15] = float(ppg)
        if papg: updates[16] = float(papg)

        n = patch_rows(html_lines, name, updates)
        if n:
            total += n
            print(f"  ✓ {name}: {int(w)}-{int(l)}"
                  + (f" | PPG {ppg}/{papg}" if ppg else ""))
        else:
            print(f"  · {name}: not in hub DB (ESPN='{raw}')")
    return total


# ── NHL ───────────────────────────────────────────────────────────────────────
#   W=3  L=4  OTL=5  gf=16  ga=17

NHL_URLS = [
    "https://site.api.espn.com/apis/v2/sports/hockey/nhl/standings",
    "https://site.api.espn.com/apis/site/v2/sports/hockey/nhl/standings",
]

def fetch_nhl_entries():
    for url in NHL_URLS:
        d = espn_get(url)
        if not d:
            continue
        entries = d.get("standings", {}).get("entries", [])
        if not entries:
            for child in d.get("children", []):
                entries += child.get("standings", {}).get("entries", [])
        if entries:
            print(f"  ✓ NHL standings: {len(entries)} teams")
            return entries
    print("  ✗ NHL standings: no data")
    return []


def update_nhl(html_lines):
    print("\n── NHL ──────────────────────────────────────────────────────────")
    total = 0
    for entry in fetch_nhl_entries():
        raw  = entry.get("team", {}).get("displayName", "")
        name = ESPN_NAME_MAP.get(raw, raw)
        sm   = stat_map(entry.get("stats", []))

        w   = sm.get("wins",         0) or 0
        l   = sm.get("losses",       0) or 0
        otl = sm.get("otLosses",     sm.get("overtimeLosses", 0)) or 0
        gf  = sm.get("pointsFor",    sm.get("goalsFor",   0)) or 0
        ga  = sm.get("pointsAgainst",sm.get("goalsAgainst",0)) or 0

        updates = {3: int(w), 4: int(l), 5: int(otl)}
        if gf:  updates[16] = int(gf)
        if ga:  updates[17] = int(ga)

        n = patch_rows(html_lines, name, updates)
        if n:
            total += n
            print(f"  ✓ {name}: {int(w)}-{int(l)}-{int(otl)}"
                  + (f" | GF/GA {int(gf)}/{int(ga)}" if gf else ""))
        else:
            print(f"  · {name}: not in hub DB (ESPN='{raw}')")
    return total


# ── timestamp banner ──────────────────────────────────────────────────────────

def update_timestamp(html_lines):
    """Update the hdr-note data-updated attribute so the hub shows today's date."""
    now  = datetime.now(timezone.utc)
    date = now.strftime("%b %-d, %Y")   # e.g. Mar 22, 2026
    for i, line in enumerate(html_lines):
        if "hdr-note" in line and "Updated:" in line:
            # Replace everything between "Updated:" and "·" with fresh date
            html_lines[i] = re.sub(
                r"(Updated:)\s*[^·]+",
                f"\\1 {date} (auto) ",
                line
            )
            break


# ── main ──────────────────────────────────────────────────────────────────────

def main():
    print(f"[update_stats] {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")

    try:
        with open(HUB_FILE, encoding="utf-8") as f:
            html_lines = f.read().splitlines(keepends=True)
    except FileNotFoundError:
        print(f"  ✗ {HUB_FILE} not found — are you in the repo root?")
        sys.exit(1)

    changed = 0
    changed += update_nba(html_lines)
    changed += update_nhl(html_lines)

    if changed:
        update_timestamp(html_lines)
        with open(HUB_FILE, "w", encoding="utf-8") as f:
            f.writelines(html_lines)
        print(f"\n✅ {changed} row(s) updated → {HUB_FILE}")
    else:
        print("\nℹ  No changes detected — hub is already up to date")

    sys.exit(0)


if __name__ == "__main__":
    main()
