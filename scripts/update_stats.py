#!/usr/bin/env python3
"""
Bet The Farm Hub — Daily stats auto-updater
Fetches current W/L + scoring data from ESPN's public API and patches
the NBA, NHL, MLB, and NFL arrays inside index.html.
Also computes ATS/O/U records from accumulated enriched results files
(data/results/*.json that have 'spread' data from the schedule snapshots).

Array index reference (0-based, from const SIDX in hub):
  NBA:  0=name 1=conf 2=div 3=W 4=L  5=aw 6=al  8=haw 9=hal  10=aaw 11=aal  12=ov 13=un  15=ppg 16=papg
  NHL:  0=name 1=conf 2=div 3=W 4=L 5=OTL  6=plw 7=pll(haw/hal/aaw/aal alias plw/pll) 13=ov 14=un  16=gf 17=ga
  MLB:  0=name 1=lg   2=div 3=W 4=L  5=aw 6=al  8=haw 9=hal  10=aaw 11=aal  12=ov 13=un  15=rs 16=ra 17=era 18=avg
  NFL:  0=name 1=conf 2=div 3=W 4=L  5=aw 6=al  8=haw 9=hal  10=aaw 11=aal  12=ov 13=un  15=ppg 16=papg
"""

import re
import sys
import json
import os
import glob
import requests
from collections import defaultdict
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
    # MLB
    "Cleveland Guardians":  "Cleveland Guardians",   # exact, just in case
    "Oakland Athletics":    "Oakland Athletics",
    # NFL
    "Washington Commanders":"Washington Commanders",
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
    """Convert ESPN stats list [{name, value, abbreviation}] to {name: value} dict.
    Also indexes by abbreviation so we catch both forms."""
    out = {}
    for s in (stats_list or []):
        if s.get("name"):    out[s["name"]]         = s.get("value")
        if s.get("abbreviation"): out[s["abbreviation"]] = s.get("value")
    return out


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
                if isinstance(val, float):
                    # Use 3 decimals for batting avg (.285), 2 for ERA (3.42), 1 for PPG
                    if val < 1:
                        parts[idx] = f"{val:.3f}"
                    elif val < 10:
                        parts[idx] = f"{val:.2f}"
                    else:
                        parts[idx] = f"{val:.1f}"
                else:
                    parts[idx] = str(int(val))
        if parts == before:
            return 0
        indent   = len(line) - len(line.lstrip())
        trailing = "," if s.endswith(",") else ""
        html_lines[i] = " " * indent + "[" + ",".join(parts) + "]" + trailing + "\n"
        return 1
    return 0   # team not found in hub


def fetch_standings(urls, sport_label):
    """Try each URL, return entries list or []."""
    for url in urls:
        d = espn_get(url)
        if not d:
            continue
        entries = d.get("standings", {}).get("entries", [])
        if not entries:
            for child in d.get("children", []):
                entries += child.get("standings", {}).get("entries", [])
        if entries:
            print(f"  ✓ {sport_label} standings: {len(entries)} teams")
            return entries
    print(f"  ✗ {sport_label} standings: no data")
    return []


# ── NBA ───────────────────────────────────────────────────────────────────────
#   W=3  L=4  ppg=15  papg=16

def update_nba(html_lines):
    print("\n── NBA ──────────────────────────────────────────────────────────")
    urls = [
        "https://site.api.espn.com/apis/v2/sports/basketball/nba/standings",
        "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/standings",
    ]
    total = 0
    for entry in fetch_standings(urls, "NBA"):
        raw   = entry.get("team", {}).get("displayName", "")
        name  = ESPN_NAME_MAP.get(raw, raw)
        sm    = stat_map(entry.get("stats", []))

        w  = sm.get("wins",   sm.get("OTWins",   0)) or 0
        l  = sm.get("losses", sm.get("OTLosses", 0)) or 0
        gp = sm.get("gamesPlayed") or (w + l) or 1

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

def update_nhl(html_lines):
    print("\n── NHL ──────────────────────────────────────────────────────────")
    urls = [
        "https://site.api.espn.com/apis/v2/sports/hockey/nhl/standings",
        "https://site.api.espn.com/apis/site/v2/sports/hockey/nhl/standings",
    ]
    total = 0
    for entry in fetch_standings(urls, "NHL"):
        raw  = entry.get("team", {}).get("displayName", "")
        name = ESPN_NAME_MAP.get(raw, raw)
        sm   = stat_map(entry.get("stats", []))

        w   = sm.get("wins",     0) or 0
        l   = sm.get("losses",   0) or 0
        otl = sm.get("otLosses", sm.get("OT",   sm.get("overtimeLosses", 0))) or 0
        gf  = sm.get("goalsFor",   sm.get("pointsFor",    0)) or 0
        ga  = sm.get("goalsAgainst",sm.get("pointsAgainst",0)) or 0

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


# ── MLB ───────────────────────────────────────────────────────────────────────
#   W=3  L=4  rs=15 (season total)  ra=16 (season total)
#   era=17 and avg=18 intentionally NOT overwritten — those are pre-season priors

def update_mlb(html_lines):
    print("\n── MLB ──────────────────────────────────────────────────────────")
    urls = [
        "https://site.api.espn.com/apis/v2/sports/baseball/mlb/standings",
        "https://site.api.espn.com/apis/site/v2/sports/baseball/mlb/standings",
    ]
    total = 0
    for entry in fetch_standings(urls, "MLB"):
        raw  = entry.get("team", {}).get("displayName", "")
        name = ESPN_NAME_MAP.get(raw, raw)
        sm   = stat_map(entry.get("stats", []))

        w  = sm.get("wins",   0) or 0
        l  = sm.get("losses", 0) or 0
        # ESPN uses pointsFor/Against for runs in baseball standings
        rs = int(sm.get("pointsFor",    sm.get("runsScored",   sm.get("RS", 0))) or 0)
        ra = int(sm.get("pointsAgainst",sm.get("runsAllowed",  sm.get("RA", 0))) or 0)

        updates = {3: int(w), 4: int(l)}
        if rs: updates[15] = rs
        if ra: updates[16] = ra

        n = patch_rows(html_lines, name, updates)
        if n:
            total += n
            print(f"  ✓ {name}: {int(w)}-{int(l)}"
                  + (f" | RS/RA {rs}/{ra}" if rs else ""))
        else:
            print(f"  · {name}: not in hub DB (ESPN='{raw}')")
    return total


# ── NFL ───────────────────────────────────────────────────────────────────────
#   W=3  L=4  T=5  ppg=16  papg=17

def update_nfl(html_lines):
    print("\n── NFL ──────────────────────────────────────────────────────────")
    urls = [
        "https://site.api.espn.com/apis/v2/sports/football/nfl/standings",
        "https://site.api.espn.com/apis/site/v2/sports/football/nfl/standings",
    ]
    total = 0
    for entry in fetch_standings(urls, "NFL"):
        raw  = entry.get("team", {}).get("displayName", "")
        name = ESPN_NAME_MAP.get(raw, raw)
        sm   = stat_map(entry.get("stats", []))

        w   = sm.get("wins",   0) or 0
        l   = sm.get("losses", 0) or 0
        t   = sm.get("ties",   sm.get("T", 0)) or 0
        gp  = sm.get("gamesPlayed") or (w + l + t) or 1

        pf_total = sm.get("pointsFor",     0) or 0
        pa_total = sm.get("pointsAgainst", 0) or 0
        ppg  = round(pf_total / gp, 1) if pf_total else sm.get("avgPointsFor",  0) or 0
        papg = round(pa_total / gp, 1) if pa_total else sm.get("avgPointsAgainst", 0) or 0

        updates = {3: int(w), 4: int(l), 5: int(t)}
        if ppg:  updates[16] = float(ppg)
        if papg: updates[17] = float(papg)

        n = patch_rows(html_lines, name, updates)
        if n:
            total += n
            print(f"  ✓ {name}: {int(w)}-{int(l)}-{int(t)}"
                  + (f" | PPG {ppg}/{papg}" if ppg else ""))
        else:
            print(f"  · {name}: not in hub DB (ESPN='{raw}')")
    return total


# ── ATS / O-U from accumulated results ───────────────────────────────────────
#
# ATS indices per sport (0-based, matches SIDX in hub):
#   NBA:  aw=5  al=6  haw=8  hal=9  aaw=10 aal=11  ov=12 un=13
#   NHL:  plw=6 pll=7  (puck-line proxy; no O/U columns in schema)
#   MLB:  aw=6  al=7  haw=9  hal=10 aaw=11 aal=12  (no O/U in schema)
#   NFL:  aw=6  al=7  haw=9  hal=10 aaw=11 aal=12  ov=13 un=14
#
# grade_ats(home_score, away_score, spread):
#   spread  = home team spread (negative = home favored, e.g. -5.5)
#   margin  = (home_score - away_score) + spread
#   >0 → home covered   <0 → away covered   ==0 → push (skip)
#
# grade_ou(home_score, away_score, total):
#   combined = home_score + away_score
#   > total → over   < total → under   == total → push (skip)

RESULTS_DIR = "data/results"

ATS_INDICES = {
    # Sourced from const SIDX in Bet The Farm Hub.html (0-based)
    # nba: aw=5,al=6,haw=8,hal=9,aaw=10,aal=11,ov=12,un=13
    # nhl: aw=6,al=7,haw=6,hal=7,aaw=6,aal=7,ov=13,un=14  (haw/hal/aaw/aal alias plw/pll)
    # mlb: aw=5,al=6,haw=8,hal=9,aaw=10,aal=11,ov=12,un=13
    # nfl: aw=5,al=6,haw=8,hal=9,aaw=10,aal=11,ov=12,un=13
    "nba": dict(aw=5,  al=6,  haw=8,  hal=9,  aaw=10, aal=11, ov=12, un=13),
    "nhl": dict(aw=6,  al=7,  haw=6,  hal=7,  aaw=6,  aal=7,  ov=13, un=14),
    "mlb": dict(aw=5,  al=6,  haw=8,  hal=9,  aaw=10, aal=11, ov=12, un=13),
    "nfl": dict(aw=5,  al=6,  haw=8,  hal=9,  aaw=10, aal=11, ov=12, un=13),
}


def compute_ats_records():
    """
    Scan all data/results/*.json files that have 'spread' attached.
    Returns {sport: {db_team_name: {aw,al,haw,hal,aaw,aal,ov,un}}} counts.
    Only counts games where spread is not None.
    """
    records = defaultdict(lambda: defaultdict(lambda: defaultdict(int)))
    # records[sport][team_db_name][stat_key] = count

    pattern = os.path.join(RESULTS_DIR, "*.json")
    result_files = sorted(glob.glob(pattern))

    if not result_files:
        print("  · No results files found in data/results/")
        return records

    total_with_spread = 0
    for fpath in result_files:
        try:
            with open(fpath) as f:
                data = json.load(f)
        except Exception:
            continue

        for sport, games in data.get("sports", {}).items():
            if sport not in ATS_INDICES:
                continue   # skip CFB/CBB — not in update pipeline

            for g in games:
                spread = g.get("spread")
                total  = g.get("total")
                h_score = g.get("home_score")
                a_score = g.get("away_score")
                # Use DB-normalized names if available, fall back to ESPN names
                home_db = g.get("home_db") or g.get("home", "")
                away_db = g.get("away_db") or g.get("away", "")

                if spread is None or h_score is None or a_score is None:
                    continue   # no spread data — can't grade ATS

                total_with_spread += 1

                # ── ATS grade ────────────────────────────────────────────────
                margin = (h_score - a_score) + spread
                if margin > 0:
                    # Home covered
                    records[sport][home_db]["aw"] += 1
                    records[sport][home_db]["haw"] += 1   # home team covered at home
                    records[sport][away_db]["al"] += 1
                    records[sport][away_db]["aal"] += 1   # away team failed to cover away
                elif margin < 0:
                    # Away covered
                    records[sport][away_db]["aw"] += 1
                    records[sport][away_db]["aaw"] += 1   # away team covered away
                    records[sport][home_db]["al"] += 1
                    records[sport][home_db]["hal"] += 1   # home team failed at home
                # margin == 0 → push, skip

                # ── O/U grade ────────────────────────────────────────────────
                if total is None:
                    continue
                combined = h_score + a_score
                if combined > total:
                    records[sport][home_db]["ov"] += 1
                    records[sport][away_db]["ov"] += 1
                elif combined < total:
                    records[sport][home_db]["un"] += 1
                    records[sport][away_db]["un"] += 1
                # combined == total → push, skip

    print(f"  ✓ ATS computation: {total_with_spread} graded game(s) across {len(result_files)} result file(s)")
    return records


def update_ats_ou(html_lines):
    """Compute ATS/O/U from stored results files and patch the hub HTML."""
    print("\n── ATS / O-U (from results archive) ────────────────────────────")

    records = compute_ats_records()
    if not any(records.values()):
        print("  · No games with spread data found — ATS/O/U not updated")
        print("    (This is expected until log_picks.py has run with the new schedule snapshot feature)")
        return 0

    total = 0
    for sport, teams in records.items():
        idx = ATS_INDICES[sport]
        sport_total = 0
        for team_name, stats in teams.items():
            updates = {}
            updates[idx["aw"]]  = stats.get("aw",  0)
            updates[idx["al"]]  = stats.get("al",  0)
            # For NHL, haw/hal/aaw/aal all alias to plw/pll — only set once
            if idx["haw"] != idx["aw"]:
                updates[idx["haw"]] = stats.get("haw", 0)
                updates[idx["hal"]] = stats.get("hal", 0)
                updates[idx["aaw"]] = stats.get("aaw", 0)
                updates[idx["aal"]] = stats.get("aal", 0)
            if idx.get("ov") is not None:
                updates[idx["ov"]] = stats.get("ov", 0)
            if idx.get("un") is not None:
                updates[idx["un"]] = stats.get("un", 0)

            n = patch_rows(html_lines, team_name, updates)
            sport_total += n

        if sport_total:
            print(f"  ✓ {sport.upper()}: {sport_total} team(s) updated with ATS/O/U")
        else:
            print(f"  · {sport.upper()}: no matching rows found in hub DB")
        total += sport_total

    return total


# ── timestamp banner ──────────────────────────────────────────────────────────

def update_timestamp(html_lines):
    """Update the hdr-note data-updated attribute so the hub shows today's date."""
    now  = datetime.now(timezone.utc)
    date = now.strftime("%b %-d, %Y")
    for i, line in enumerate(html_lines):
        if "hdr-note" in line and "Updated:" in line:
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
    changed += update_mlb(html_lines)
    changed += update_nfl(html_lines)
    changed += update_ats_ou(html_lines)

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
