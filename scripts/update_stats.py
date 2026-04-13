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

def fetch_nhl_special_teams():
    """
    Fetch PP% and PK% from the NHL Stats API (free, no key needed).
    Returns {team_full_name: {pp: float, pk: float}} or {} on failure.
    """
    now = datetime.now()
    # NHL season spans two calendar years: e.g. 2025-26 season = seasonId 20252026
    year = now.year if now.month >= 9 else now.year - 1
    season_id = f"{year}{year + 1}"
    url = (
        f"https://api.nhle.com/stats/rest/en/team/summary"
        f"?isAggregate=false&isGame=false"
        f"&cayenneExp=seasonId={season_id}%20and%20gameTypeId=2"
    )
    # NHL API team name → hub team name corrections
    NHL_NAME_MAP = {
        "Utah Hockey Club": "Utah Mammoth",
        "Montréal Canadiens": "Montreal Canadiens",
    }
    try:
        resp = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
        if resp.status_code != 200:
            print(f"  · NHL Stats API returned {resp.status_code} — PP%/PK% skipped")
            return {}
        data = resp.json()
        result = {}
        for team in data.get("data", []):
            name = team.get("teamFullName", "")
            name = NHL_NAME_MAP.get(name, name)
            pp = team.get("powerPlayPct")
            pk = team.get("penaltyKillPct")
            if name and pp is not None and pk is not None:
                result[name] = {"pp": round(pp * 100, 1), "pk": round(pk * 100, 1)}
        print(f"  ✓ NHL Stats API: {len(result)} teams with PP%/PK%")
        return result
    except Exception as e:
        print(f"  · NHL Stats API error: {e} — PP%/PK% skipped")
        return {}


def fetch_nhl_goalie_stats():
    """
    Fetch starting goalie SV% and GAA from the NHL Stats API.
    For each team, picks the goalie with the most games started.
    Returns {hub_team_name: {sv: float, gaa: float, name: str}} or {} on failure.
    """
    now = datetime.now()
    year = now.year if now.month >= 9 else now.year - 1
    season_id = f"{year}{year + 1}"
    url = (
        f"https://api.nhle.com/stats/rest/en/goalie/summary"
        f"?isAggregate=false&isGame=false"
        f"&cayenneExp=seasonId={season_id}%20and%20gameTypeId=2"
        f"&sort=%5B%7B%22property%22%3A%22gamesStarted%22%2C%22direction%22%3A%22DESC%22%7D%5D"
        f"&limit=90"
    )
    # NHL API abbreviation → hub team name
    NHL_ABBR_MAP = {
        "ANA": "Anaheim Ducks", "BOS": "Boston Bruins", "BUF": "Buffalo Sabres",
        "CAR": "Carolina Hurricanes", "CBJ": "Columbus Blue Jackets",
        "CGY": "Calgary Flames", "CHI": "Chicago Blackhawks",
        "COL": "Colorado Avalanche", "DAL": "Dallas Stars",
        "DET": "Detroit Red Wings", "EDM": "Edmonton Oilers",
        "FLA": "Florida Panthers", "LAK": "Los Angeles Kings",
        "MIN": "Minnesota Wild", "MTL": "Montreal Canadiens",
        "NJD": "New Jersey Devils", "NSH": "Nashville Predators",
        "NYI": "New York Islanders", "NYR": "New York Rangers",
        "OTT": "Ottawa Senators", "PHI": "Philadelphia Flyers",
        "PIT": "Pittsburgh Penguins", "SEA": "Seattle Kraken",
        "SJS": "San Jose Sharks", "STL": "St. Louis Blues",
        "TBL": "Tampa Bay Lightning", "TOR": "Toronto Maple Leafs",
        "UTA": "Utah Mammoth", "VAN": "Vancouver Canucks",
        "VGK": "Vegas Golden Knights", "WPG": "Winnipeg Jets",
        "WSH": "Washington Capitals",
    }
    try:
        resp = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
        if resp.status_code != 200:
            print(f"  · NHL goalie API returned {resp.status_code} — SV%/GAA skipped")
            return {}
        data = resp.json()
        # Group by team, pick goalie with most games started per team
        by_team = {}
        for g in data.get("data", []):
            abbr = (g.get("teamAbbrevs") or "").split(",")[0]  # handle traded players
            gs = g.get("gamesStarted", 0) or 0
            if abbr and gs > by_team.get(abbr, {}).get("gs", 0):
                by_team[abbr] = {
                    "gs": gs,
                    "sv": g.get("savePct"),
                    "gaa": g.get("goalsAgainstAverage"),
                    "name": g.get("goalieFullName", ""),
                }
        result = {}
        for abbr, info in by_team.items():
            team_name = NHL_ABBR_MAP.get(abbr)
            if team_name and info["sv"] is not None and info["gaa"] is not None:
                result[team_name] = {
                    "sv": round(info["sv"], 4),
                    "gaa": round(info["gaa"], 3),
                    "name": info["name"],
                }
        print(f"  ✓ NHL goalie API: {len(result)} teams with starter SV%/GAA")
        return result
    except Exception as e:
        print(f"  · NHL goalie API error: {e} — SV%/GAA skipped")
        return {}


def update_nhl(html_lines):
    print("\n── NHL ──────────────────────────────────────────────────────────")
    urls = [
        "https://site.api.espn.com/apis/v2/sports/hockey/nhl/standings",
        "https://site.api.espn.com/apis/site/v2/sports/hockey/nhl/standings",
    ]

    # Fetch special teams data from NHL Stats API
    special_teams = fetch_nhl_special_teams()
    # Fetch starter goalie SV%/GAA from NHL Stats API
    goalie_stats = fetch_nhl_goalie_stats()

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

        # Patch PP% and PK% from NHL Stats API (indices 18, 19)
        st = special_teams.get(name, {})
        if st.get("pp") is not None:
            updates[18] = st["pp"]
        if st.get("pk") is not None:
            updates[19] = st["pk"]

        # Patch starter goalie SV% and GAA (indices 24, 25)
        gl = goalie_stats.get(name, {})
        if gl.get("sv") is not None:
            updates[24] = gl["sv"]
        if gl.get("gaa") is not None:
            updates[25] = gl["gaa"]

        n = patch_rows(html_lines, name, updates)
        if n:
            total += n
            pp_str = f" | PP {st['pp']}% PK {st['pk']}%" if st else ""
            gl_str = f" | G: {gl['name'].split()[-1]} SV%{gl['sv']:.3f}" if gl else ""
            print(f"  ✓ {name}: {int(w)}-{int(l)}-{int(otl)}"
                  + (f" | GF/GA {int(gf)}/{int(ga)}" if gf else "")
                  + pp_str + gl_str)
        else:
            print(f"  · {name}: not in hub DB (ESPN='{raw}')")
    return total


# ── MLB ───────────────────────────────────────────────────────────────────────
#   W=3  L=4  rs=15 (season total)  ra=16 (season total)
#   era=17 and avg=18 intentionally NOT overwritten — those are pre-season priors
#   ops=23  whip=24  (from MLB Stats API)

MLB_NAME_MAP_STATSAPI = {
    "Arizona Diamondbacks": "Arizona Diamondbacks",
    "Cleveland Guardians":  "Cleveland Guardians",
}


def fetch_mlb_advanced():
    """
    Fetch team OPS (hitting) and WHIP (pitching) from MLB Stats API (free, no key).
    Returns {team_full_name: {ops: float, whip: float}} or {} on failure.
    """
    year = datetime.now().year
    hitting_url = (
        f"https://statsapi.mlb.com/api/v1/teams/stats"
        f"?stats=season&group=hitting&season={year}&sportIds=1"
    )
    pitching_url = (
        f"https://statsapi.mlb.com/api/v1/teams/stats"
        f"?stats=season&group=pitching&season={year}&sportIds=1"
    )
    result = {}
    try:
        # Fetch hitting stats (OPS)
        resp = requests.get(hitting_url, headers=HEADERS, timeout=TIMEOUT)
        if resp.status_code != 200:
            print(f"  · MLB Stats API hitting returned {resp.status_code} — OPS skipped")
            return {}
        for split in resp.json().get("stats", [{}])[0].get("splits", []):
            name = split.get("team", {}).get("name", "")
            name = MLB_NAME_MAP_STATSAPI.get(name, name)
            ops = split.get("stat", {}).get("ops")
            if name and ops is not None:
                result.setdefault(name, {})["ops"] = float(ops)

        # Fetch pitching stats (WHIP)
        resp = requests.get(pitching_url, headers=HEADERS, timeout=TIMEOUT)
        if resp.status_code != 200:
            print(f"  · MLB Stats API pitching returned {resp.status_code} — WHIP skipped")
            return result
        for split in resp.json().get("stats", [{}])[0].get("splits", []):
            name = split.get("team", {}).get("name", "")
            name = MLB_NAME_MAP_STATSAPI.get(name, name)
            whip = split.get("stat", {}).get("whip")
            if name and whip is not None:
                result.setdefault(name, {})["whip"] = float(whip)

        print(f"  ✓ MLB Stats API: {len(result)} teams with OPS/WHIP")
        return result
    except Exception as e:
        print(f"  · MLB Stats API error: {e} — OPS/WHIP skipped")
        return result


def update_mlb(html_lines):
    print("\n── MLB ──────────────────────────────────────────────────────────")
    urls = [
        "https://site.api.espn.com/apis/v2/sports/baseball/mlb/standings",
        "https://site.api.espn.com/apis/site/v2/sports/baseball/mlb/standings",
    ]

    # Fetch advanced stats from MLB Stats API
    advanced = fetch_mlb_advanced()

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

        # Patch OPS + WHIP from MLB Stats API (indices 23, 24)
        adv = advanced.get(name, {})
        if adv.get("ops") is not None:
            updates[23] = round(adv["ops"], 3)
        if adv.get("whip") is not None:
            updates[24] = round(adv["whip"], 2)

        n = patch_rows(html_lines, name, updates)
        if n:
            total += n
            adv_str = ""
            if adv.get("ops") is not None:
                adv_str = f" | OPS {adv['ops']:.3f} WHIP {adv.get('whip', 0):.2f}"
            print(f"  ✓ {name}: {int(w)}-{int(l)}"
                  + (f" | RS/RA {rs}/{ra}" if rs else "")
                  + adv_str)
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

        # index.json is a plain list — skip anything that isn't a results dict
        if not isinstance(data, dict) or "sports" not in data:
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


# ── Recent form (last 10 games per team) ─────────────────────────────────────
# Scans results files newest-first to compute each team's last-10 record.
# New SIDX indices: r10w, r10l, r10ppg, r10papg (appended to each sport array)

R10_INDICES = {
    # Matches SIDX r10w/r10l/r10ppg/r10papg values
    "nba": dict(r10w=20, r10l=21, r10ppg=22, r10papg=23),
    "nhl": dict(r10w=20, r10l=21, r10ppg=22, r10papg=23),
    "nfl": dict(r10w=19, r10l=20, r10ppg=21, r10papg=22),
    "cfb": dict(r10w=19, r10l=20, r10ppg=21, r10papg=22),
    "cbb": dict(r10w=19, r10l=20, r10ppg=21, r10papg=22),
    "mlb": dict(r10w=19, r10l=20, r10ppg=21, r10papg=22),
}


def compute_recent_form():
    """
    Scan data/results/*.json newest-first, compute per-team last-10 game records.
    Returns {sport: {team_db_name: {r10w, r10l, r10ppg, r10papg}}}.
    """
    pattern = os.path.join(RESULTS_DIR, "*.json")
    result_files = sorted(glob.glob(pattern), reverse=True)  # newest first

    if not result_files:
        print("  · No results files for recent form")
        return {}

    # Track per team: {sport: {team: {games: int, w: int, l: int, pf: int, pa: int}}}
    team_stats = defaultdict(lambda: defaultdict(lambda: {"games": 0, "w": 0, "l": 0, "pf": 0, "pa": 0}))

    for fpath in result_files:
        fname = os.path.basename(fpath)
        if fname == "index.json":
            continue
        try:
            with open(fpath) as f:
                data = json.load(f)
        except Exception:
            continue

        if not isinstance(data, dict) or "sports" not in data:
            continue

        for sport, games in data.get("sports", {}).items():
            for g in games:
                h_score = g.get("home_score")
                a_score = g.get("away_score")
                if h_score is None or a_score is None:
                    continue

                home_db = g.get("home_db") or g.get("home", "")
                away_db = g.get("away_db") or g.get("away", "")

                # Home team
                ht = team_stats[sport][home_db]
                if ht["games"] < 10:
                    ht["games"] += 1
                    ht["pf"] += h_score
                    ht["pa"] += a_score
                    if h_score > a_score:
                        ht["w"] += 1
                    elif a_score > h_score:
                        ht["l"] += 1

                # Away team
                at = team_stats[sport][away_db]
                if at["games"] < 10:
                    at["games"] += 1
                    at["pf"] += a_score
                    at["pa"] += h_score
                    if a_score > h_score:
                        at["w"] += 1
                    elif h_score > a_score:
                        at["l"] += 1

    # Convert to final format
    result = {}
    for sport, teams in team_stats.items():
        result[sport] = {}
        for team, s in teams.items():
            gp = s["games"] or 1
            result[sport][team] = {
                "r10w": s["w"],
                "r10l": s["l"],
                "r10ppg": round(s["pf"] / gp, 1),
                "r10papg": round(s["pa"] / gp, 1),
            }

    return result


def update_recent_form(html_lines):
    """Compute last-10 form from results files and patch hub arrays."""
    print("\n── Recent Form (last 10 games) ─────────────────────────────────")

    form = compute_recent_form()
    if not form:
        print("  · No recent form data computed")
        return 0

    total = 0
    for sport, teams in form.items():
        idx = R10_INDICES.get(sport)
        if not idx:
            continue
        sport_total = 0
        for team_name, stats in teams.items():
            updates = {
                idx["r10w"]:    stats["r10w"],
                idx["r10l"]:    stats["r10l"],
                idx["r10ppg"]:  stats["r10ppg"],
                idx["r10papg"]: stats["r10papg"],
            }
            n = patch_rows(html_lines, team_name, updates)
            sport_total += n
        if sport_total:
            print(f"  ✓ {sport.upper()}: {sport_total} team(s) updated with last-10 form")
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
    changed += update_recent_form(html_lines)

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
