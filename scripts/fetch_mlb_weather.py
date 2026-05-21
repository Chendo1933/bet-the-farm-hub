#!/usr/bin/env python3
"""Fetch wind + temp for today's MLB games and write data/weather/{date}.json.

Pipeline:
  1. Pull today's MLB schedule from statsapi.mlb.com (no key needed).
  2. For each home team, look up STADIUM_META (lat/lon/outfield_bearing/is_dome).
  3. Skip domes. For outdoor parks, hit api.weather.gov with NO key, get the
     hourly forecast period that matches gameDate, parse wind speed/direction.
  4. Compute wind_out_component: positive = blowing OUT to CF, negative = IN.
     Used by the hub's hbScoreOU windSignal factor.

Output shape (keyed by home team name as it appears in MLB_PARK_FACTORS):
  {
    "date": "2026-05-17",
    "fetched_at": "...",
    "parks": {
      "Chicago Cubs": {
        "is_dome": false,
        "wind_mph": 14,
        "wind_from_deg": 202,      # NWS direction wind is coming FROM
        "outfield_bearing_deg": 30, # direction toward CF from home plate
        "wind_out_component": 11.2, # speed * cos(angle_from_outfield)
        "wind_label": "out to CF",  # one of: out to CF, in from CF, crosswind, calm
        "temp_f": 71,
        "game_time_utc": "..."
      },
      "Tampa Bay Rays": {"is_dome": true}
    }
  }

Outfield bearings are direction-from-home-plate-to-center-field (0=N, 90=E,
180=S, 270=W). Accurate to ~10 degrees, which is fine because the wind
classifier thresholds at +/- 45 degrees.
"""
from __future__ import annotations

import json
import math
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

import requests

# NWS requires a User-Agent identifying the app. Per their docs.
NWS_HEADERS = {"User-Agent": "bet-the-farm-hub (contact: chendo1933@users.noreply.github.com)"}
NWS_TIMEOUT = 15

# Direction-from-home-plate-toward-CF, in degrees clockwise from N.
# Sources: ballpark aerial imagery + MLB venue surveys. Approximate.
STADIUM_META = {
    # AL East
    "Baltimore Orioles":    {"lat": 39.2839, "lon": -76.6217, "outfield_bearing_deg":  60, "is_dome": False, "park": "Camden Yards"},
    "Boston Red Sox":       {"lat": 42.3467, "lon": -71.0972, "outfield_bearing_deg":  50, "is_dome": False, "park": "Fenway Park"},
    "New York Yankees":     {"lat": 40.8296, "lon": -73.9262, "outfield_bearing_deg":  70, "is_dome": False, "park": "Yankee Stadium"},
    "Tampa Bay Rays":       {"lat": 27.7682, "lon": -82.6534, "outfield_bearing_deg":   0, "is_dome": True,  "park": "Tropicana Field"},
    "Toronto Blue Jays":    {"lat": 43.6414, "lon": -79.3894, "outfield_bearing_deg":   0, "is_dome": True,  "park": "Rogers Centre"},  # retractable; usually closed
    # AL Central
    "Chicago White Sox":    {"lat": 41.8299, "lon": -87.6338, "outfield_bearing_deg":  40, "is_dome": False, "park": "Rate Field"},
    "Cleveland Guardians":  {"lat": 41.4962, "lon": -81.6852, "outfield_bearing_deg":   0, "is_dome": False, "park": "Progressive Field"},
    "Detroit Tigers":       {"lat": 42.3390, "lon": -83.0485, "outfield_bearing_deg": 150, "is_dome": False, "park": "Comerica Park"},
    "Kansas City Royals":   {"lat": 39.0517, "lon": -94.4803, "outfield_bearing_deg":  45, "is_dome": False, "park": "Kauffman Stadium"},
    "Minnesota Twins":      {"lat": 44.9817, "lon": -93.2776, "outfield_bearing_deg":  90, "is_dome": False, "park": "Target Field"},
    # AL West
    "Houston Astros":       {"lat": 29.7572, "lon": -95.3556, "outfield_bearing_deg":   0, "is_dome": True,  "park": "Daikin Park"},     # retractable; closed in summer heat
    "Los Angeles Angels":   {"lat": 33.8003, "lon": -117.8827,"outfield_bearing_deg":  60, "is_dome": False, "park": "Angel Stadium"},
    "Athletics":            {"lat": 38.5803, "lon": -121.5132,"outfield_bearing_deg": 135, "is_dome": False, "park": "Sutter Health Park"},  # 2025-26 Sacramento
    "Seattle Mariners":     {"lat": 47.5914, "lon": -122.3325,"outfield_bearing_deg":   0, "is_dome": True,  "park": "T-Mobile Park"},   # retractable; often closed
    "Texas Rangers":        {"lat": 32.7475, "lon": -97.0827, "outfield_bearing_deg":   0, "is_dome": True,  "park": "Globe Life Field"},# retractable; usually closed
    # NL East
    "Atlanta Braves":       {"lat": 33.8907, "lon": -84.4677, "outfield_bearing_deg":  70, "is_dome": False, "park": "Truist Park"},
    "Miami Marlins":        {"lat": 25.7781, "lon": -80.2197, "outfield_bearing_deg":   0, "is_dome": True,  "park": "loanDepot Park"},  # retractable; often closed
    "New York Mets":        {"lat": 40.7571, "lon": -73.8458, "outfield_bearing_deg":  25, "is_dome": False, "park": "Citi Field"},
    "Philadelphia Phillies":{"lat": 39.9061, "lon": -75.1665, "outfield_bearing_deg":  60, "is_dome": False, "park": "Citizens Bank Park"},
    "Washington Nationals": {"lat": 38.8730, "lon": -77.0074, "outfield_bearing_deg":  25, "is_dome": False, "park": "Nationals Park"},
    # NL Central
    "Chicago Cubs":         {"lat": 41.9484, "lon": -87.6553, "outfield_bearing_deg":  30, "is_dome": False, "park": "Wrigley Field"},
    "Cincinnati Reds":      {"lat": 39.0975, "lon": -84.5066, "outfield_bearing_deg": 100, "is_dome": False, "park": "Great American Ball Park"},
    "Milwaukee Brewers":    {"lat": 43.0280, "lon": -87.9712, "outfield_bearing_deg":   0, "is_dome": True,  "park": "American Family Field"},  # retractable; closed half the time
    "Pittsburgh Pirates":   {"lat": 40.4469, "lon": -80.0057, "outfield_bearing_deg": 110, "is_dome": False, "park": "PNC Park"},
    "St. Louis Cardinals":  {"lat": 38.6226, "lon": -90.1928, "outfield_bearing_deg":  60, "is_dome": False, "park": "Busch Stadium"},
    # NL West
    "Arizona Diamondbacks": {"lat": 33.4453, "lon": -112.0667,"outfield_bearing_deg":   0, "is_dome": True,  "park": "Chase Field"},     # retractable; closed for heat
    "Colorado Rockies":     {"lat": 39.7559, "lon": -104.9942,"outfield_bearing_deg":   0, "is_dome": False, "park": "Coors Field"},
    "Los Angeles Dodgers":  {"lat": 34.0739, "lon": -118.2400,"outfield_bearing_deg":  25, "is_dome": False, "park": "Dodger Stadium"},
    "San Diego Padres":     {"lat": 32.7073, "lon": -117.1566,"outfield_bearing_deg":  70, "is_dome": False, "park": "Petco Park"},
    "San Francisco Giants": {"lat": 37.7786, "lon": -122.3893,"outfield_bearing_deg":  90, "is_dome": False, "park": "Oracle Park"},
}

# 16-point compass to degrees (direction wind is COMING FROM)
COMPASS_TO_DEG = {
    "N": 0, "NNE": 22.5, "NE": 45, "ENE": 67.5,
    "E": 90, "ESE": 112.5, "SE": 135, "SSE": 157.5,
    "S": 180, "SSW": 202.5, "SW": 225, "WSW": 247.5,
    "W": 270, "WNW": 292.5, "NW": 315, "NNW": 337.5,
}


def parse_wind_speed(s: str) -> int | None:
    """NWS returns 'windSpeed' as '5 mph', '10 to 15 mph', '0 mph', etc.
    Return the average of any range, rounded to int. None if unparseable."""
    if not s:
        return None
    nums = [int(n) for n in re.findall(r"\d+", s)]
    if not nums:
        return None
    return round(sum(nums) / len(nums))


def parse_wind_direction(s: str) -> float | None:
    """NWS returns 'windDirection' as 'NE' / 'SSW' etc. (direction from)."""
    if not s:
        return None
    return COMPASS_TO_DEG.get(s.strip().upper())


def wind_out_to_cf(wind_from_deg: float, wind_mph: float, outfield_bearing_deg: float) -> tuple[float, str]:
    """Decompose wind into out-to-CF component.

    Wind direction is the direction the wind is COMING FROM, so wind blowing
    toward CF means wind_from is roughly opposite to outfield_bearing.

    Returns (signed_component_mph, label):
      positive = blowing out toward CF (helps hitters)
      negative = blowing in from CF (helps pitchers)
      label in {"out to CF", "in from CF", "crosswind", "calm"}
    """
    if wind_mph < 4:
        return 0.0, "calm"
    # Direction the wind is blowing TOWARD (out-bound vector).
    wind_to_deg = (wind_from_deg + 180.0) % 360.0
    # Smallest absolute angle between wind direction and outfield bearing.
    diff = abs(wind_to_deg - outfield_bearing_deg) % 360.0
    if diff > 180.0:
        diff = 360.0 - diff
    # cos(diff) projects the wind vector onto the outfield axis.
    # diff=0   -> +1 (full out)
    # diff=90  ->  0 (pure crosswind)
    # diff=180 -> -1 (full in)
    component = wind_mph * math.cos(math.radians(diff))
    if diff <= 45:
        label = "out to CF"
    elif diff >= 135:
        label = "in from CF"
    else:
        label = "crosswind"
    return round(component, 1), label


def fetch_nws_hourly(lat: float, lon: float) -> list[dict] | None:
    """Two-step NWS lookup: /points/{lat,lon} -> .properties.forecastHourly URL,
    then GET that URL. Returns the periods list or None on any failure."""
    try:
        points_url = f"https://api.weather.gov/points/{lat:.4f},{lon:.4f}"
        r = requests.get(points_url, headers=NWS_HEADERS, timeout=NWS_TIMEOUT)
        r.raise_for_status()
        hourly_url = r.json().get("properties", {}).get("forecastHourly")
        if not hourly_url:
            return None
        r2 = requests.get(hourly_url, headers=NWS_HEADERS, timeout=NWS_TIMEOUT)
        r2.raise_for_status()
        return r2.json().get("properties", {}).get("periods") or None
    except Exception as e:
        print(f"  ! NWS fetch failed for ({lat},{lon}): {e}", file=sys.stderr)
        return None


def pick_period_for_game_time(periods: list[dict], game_time_utc: datetime) -> dict | None:
    """Find the hourly forecast period that covers the game's first pitch."""
    for p in periods:
        try:
            start = datetime.fromisoformat(p["startTime"].replace("Z", "+00:00"))
            end = datetime.fromisoformat(p["endTime"].replace("Z", "+00:00"))
            if start <= game_time_utc < end:
                return p
        except (KeyError, ValueError):
            continue
    # Fall back to first period if game time is outside forecast window
    return periods[0] if periods else None


def fetch_mlb_schedule(date_str: str) -> list[dict]:
    """Return list of {home, away, game_time_utc} for date_str (YYYY-MM-DD)."""
    url = f"https://statsapi.mlb.com/api/v1/schedule?sportId=1&date={date_str}"
    try:
        r = requests.get(url, timeout=NWS_TIMEOUT)
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        print(f"! MLB schedule fetch failed: {e}", file=sys.stderr)
        return []
    games = []
    for date_block in data.get("dates", []):
        for g in date_block.get("games", []):
            home = (g.get("teams", {}).get("home", {}).get("team", {}).get("name") or "").strip()
            away = (g.get("teams", {}).get("away", {}).get("team", {}).get("name") or "").strip()
            gt = g.get("gameDate")
            if home and gt:
                try:
                    game_time_utc = datetime.fromisoformat(gt.replace("Z", "+00:00"))
                except ValueError:
                    game_time_utc = None
                games.append({"home": home, "away": away, "game_time_utc": game_time_utc})
    return games


def main(argv: list[str]) -> int:
    # Default to today's UTC date — daily-update.yml runs at 11 UTC = 7am ET,
    # so UTC date == ET date at that hour.
    date_str = argv[1] if len(argv) > 1 else datetime.now(timezone.utc).strftime("%Y-%m-%d")
    print(f"[weather] Fetching for {date_str}")

    games = fetch_mlb_schedule(date_str)
    if not games:
        print("[weather] No MLB games today — writing empty file")

    out = {
        "date": date_str,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "parks": {},
    }

    seen_homes = set()
    for g in games:
        home = g["home"]
        if home in seen_homes:
            continue
        seen_homes.add(home)
        meta = STADIUM_META.get(home)
        if not meta:
            print(f"  ? Unknown stadium for: {home}")
            continue
        if meta["is_dome"]:
            out["parks"][home] = {"is_dome": True, "park": meta["park"]}
            print(f"  - {home}: dome ({meta['park']}), skipping weather")
            continue
        periods = fetch_nws_hourly(meta["lat"], meta["lon"])
        if not periods:
            out["parks"][home] = {"is_dome": False, "park": meta["park"], "error": "no forecast"}
            continue
        gt_utc = g["game_time_utc"] or datetime.now(timezone.utc)
        period = pick_period_for_game_time(periods, gt_utc)
        if not period:
            out["parks"][home] = {"is_dome": False, "park": meta["park"], "error": "no period match"}
            continue
        mph = parse_wind_speed(period.get("windSpeed", "")) or 0
        wfrom = parse_wind_direction(period.get("windDirection", ""))
        temp = period.get("temperature")
        if wfrom is None:
            out["parks"][home] = {
                "is_dome": False, "park": meta["park"],
                "wind_mph": mph, "temp_f": temp,
                "error": "no wind direction",
            }
            continue
        component, label = wind_out_to_cf(wfrom, mph, meta["outfield_bearing_deg"])
        out["parks"][home] = {
            "is_dome": False,
            "park": meta["park"],
            "wind_mph": mph,
            "wind_from_deg": round(wfrom, 1),
            "outfield_bearing_deg": meta["outfield_bearing_deg"],
            "wind_out_component": component,
            "wind_label": label,
            "temp_f": temp,
            "game_time_utc": gt_utc.isoformat(),
        }
        print(f"  ✓ {home} @ {meta['park']}: {mph}mph from {period.get('windDirection')} → {label} ({component:+.1f}mph component)")

    # Write file
    repo_root = Path(__file__).resolve().parent.parent
    out_dir = repo_root / "data" / "weather"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{date_str}.json"
    out_path.write_text(json.dumps(out, indent=2))
    print(f"[weather] Wrote {len(out['parks'])} parks → {out_path.relative_to(repo_root)}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
