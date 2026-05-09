"""
Map BTF picks → Kalshi market tickers.

Phase 1 covers ML picks only. Spread/total picks need their own logic
(Kalshi has limited spread coverage and stadium-total contracts vary).

Mapping flow for ML:
  1. From the pick, extract {sport, home, away, picked_team, game_date}
  2. Find Kalshi event matching (sport, home, away, date) — search by title
  3. From the event, find the binary "will TEAM win" market
  4. Return ticker + the YES/NO direction we want
     (we always want YES on the picked team's win contract)

Match scoring is conservative — we'd rather miss a pick than place on the
wrong market. If multiple candidates match, we abort and log an "ambiguous"
result for human review.
"""
from __future__ import annotations

import re
from datetime import datetime
from typing import Optional

from .client import KalshiClient


# Kalshi sports series tickers — verify against current docs.
# Names are stable but tickers can change; if list_events returns 404 or
# empty, check https://kalshi.com for the current series.
SPORT_TO_SERIES = {
    "MLB": "KXMLBGAME",
    "NBA": "KXNBAGAME",
    "NHL": "KXNHLGAME",
    "NFL": "KXNFLGAME",
}


def _normalize(s: str) -> str:
    """Lowercase + strip non-alphanumerics for fuzzy team/title matching."""
    return re.sub(r"[^a-z0-9]+", "", (s or "").lower())


# Hub team-name → Kalshi ticker abbreviation. Used as a backup signal when
# yes_sub_title doesn't disambiguate. Add new entries when a `no_market`
# result surfaces a market we can verify by suffix.
_KALSHI_TEAM_ABBR: dict[str, set[str]] = {
    # NHL
    "Anaheim Ducks": {"ANA"}, "Boston Bruins": {"BOS"}, "Buffalo Sabres": {"BUF"},
    "Calgary Flames": {"CGY"}, "Carolina Hurricanes": {"CAR"}, "Chicago Blackhawks": {"CHI"},
    "Colorado Avalanche": {"COL"}, "Columbus Blue Jackets": {"CBJ"},
    "Dallas Stars": {"DAL"}, "Detroit Red Wings": {"DET"}, "Edmonton Oilers": {"EDM"},
    "Florida Panthers": {"FLA"}, "Los Angeles Kings": {"LAK", "LA"},
    "Minnesota Wild": {"MIN"}, "Montreal Canadiens": {"MTL"}, "Nashville Predators": {"NSH"},
    "New Jersey Devils": {"NJD", "NJ"}, "New York Islanders": {"NYI"},
    "New York Rangers": {"NYR"}, "Ottawa Senators": {"OTT"}, "Philadelphia Flyers": {"PHI"},
    "Pittsburgh Penguins": {"PIT"}, "San Jose Sharks": {"SJS", "SJ"},
    "Seattle Kraken": {"SEA"}, "St. Louis Blues": {"STL"},
    "Tampa Bay Lightning": {"TBL", "TB"}, "Toronto Maple Leafs": {"TOR"},
    "Utah Mammoth": {"UTA"}, "Vancouver Canucks": {"VAN"},
    "Vegas Golden Knights": {"VGK"}, "Washington Capitals": {"WSH"},
    "Winnipeg Jets": {"WPG"},
    # MLB
    "Arizona Diamondbacks": {"ARI"}, "Atlanta Braves": {"ATL"}, "Baltimore Orioles": {"BAL"},
    "Boston Red Sox": {"BOS"}, "Chicago Cubs": {"CHC", "CHI"}, "Chicago White Sox": {"CWS", "CHW"},
    "Cincinnati Reds": {"CIN"}, "Cleveland Guardians": {"CLE"}, "Colorado Rockies": {"COL"},
    "Detroit Tigers": {"DET"}, "Houston Astros": {"HOU"}, "Kansas City Royals": {"KC", "KCR"},
    "Los Angeles Angels": {"LAA"}, "Los Angeles Dodgers": {"LAD"}, "Miami Marlins": {"MIA"},
    "Milwaukee Brewers": {"MIL"}, "Minnesota Twins": {"MIN"}, "New York Mets": {"NYM"},
    "New York Yankees": {"NYY"}, "Athletics": {"OAK", "ATH"},
    "Philadelphia Phillies": {"PHI"}, "Pittsburgh Pirates": {"PIT"},
    "San Diego Padres": {"SD", "SDP"}, "San Francisco Giants": {"SF", "SFG"},
    "Seattle Mariners": {"SEA"}, "St. Louis Cardinals": {"STL"},
    "Tampa Bay Rays": {"TB", "TBR"}, "Texas Rangers": {"TEX"},
    "Toronto Blue Jays": {"TOR"}, "Washington Nationals": {"WSH", "WAS"},
    # NBA
    "Atlanta Hawks": {"ATL"}, "Boston Celtics": {"BOS"}, "Brooklyn Nets": {"BKN"},
    "Charlotte Hornets": {"CHA"}, "Chicago Bulls": {"CHI"}, "Cleveland Cavaliers": {"CLE"},
    "Dallas Mavericks": {"DAL"}, "Denver Nuggets": {"DEN"}, "Detroit Pistons": {"DET"},
    "Golden State Warriors": {"GSW", "GS"}, "Houston Rockets": {"HOU"},
    "Indiana Pacers": {"IND"}, "LA Clippers": {"LAC"}, "Los Angeles Clippers": {"LAC"},
    "Los Angeles Lakers": {"LAL"}, "Memphis Grizzlies": {"MEM"}, "Miami Heat": {"MIA"},
    "Milwaukee Bucks": {"MIL"}, "Minnesota Timberwolves": {"MIN"},
    "New Orleans Pelicans": {"NOP", "NO"}, "New York Knicks": {"NYK"},
    "Oklahoma City Thunder": {"OKC"}, "Orlando Magic": {"ORL"},
    "Philadelphia 76ers": {"PHI"}, "Phoenix Suns": {"PHX"},
    "Portland Trail Blazers": {"POR"}, "Sacramento Kings": {"SAC"},
    "San Antonio Spurs": {"SA", "SAS"}, "Toronto Raptors": {"TOR"},
    "Utah Jazz": {"UTA"}, "Washington Wizards": {"WAS", "WSH"},
}


def _team_abbreviations(team_name: str) -> set[str]:
    """Look up known Kalshi ticker abbreviations for a hub team name."""
    if not team_name: return set()
    abbrs = _KALSHI_TEAM_ABBR.get(team_name, set()).copy()
    # Also try without 'St.' / 'St ' normalization
    alt = team_name.replace("St.", "St").replace("St ", "St. ")
    if alt != team_name:
        abbrs |= _KALSHI_TEAM_ABBR.get(alt, set())
    return abbrs


def _team_keywords(name: str) -> set[str]:
    """
    Pull useful keywords from a full team name. e.g., 'Atlanta Braves' →
    {'atlanta', 'braves', 'atl'}. Helps match against Kalshi titles which
    sometimes use mascot only ('Braves'), sometimes city ('Atlanta'), etc.
    """
    if not name: return set()
    parts = re.findall(r"[A-Za-z]+", name)
    out = {p.lower() for p in parts if len(p) > 2}
    out.add(_normalize(name))
    return out


def _picked_team_name(pick: dict) -> Optional[str]:
    """
    Determine which team the pick is on for ML.
    Pick label format examples:
      'New York Yankees ML (-136)'    → 'New York Yankees'
      'Colorado Avalanche ML (+102)'  → 'Colorado Avalanche'
    Falls back to atsPick/home/away mapping if label parse fails.
    """
    label = pick.get("pickLabel", "") or pick.get("pick", "")
    m = re.match(r"^(.+?)\s+ML\s*\(", label)
    if m:
        return m.group(1).strip()
    # Fallback: use atsPick + home/away
    ats = pick.get("atsPick")
    if ats == "home":
        return pick.get("home")
    if ats == "away":
        return pick.get("away")
    # Last resort — use pickedTeam if set
    return pick.get("pickedTeam") or None


def _event_matches_game(event: dict, sport: str, home: str, away: str) -> int:
    """
    Score how well an event matches our (home, away) pair. 0 = no match,
    higher = better. We want strong evidence — scoring rewards both teams
    appearing in the event title/subtitle.
    """
    title = (event.get("title", "") + " " + event.get("sub_title", "") + " " +
             event.get("subtitle", "") + " " + event.get("ticker", "")).lower()
    home_kw = _team_keywords(home)
    away_kw = _team_keywords(away)
    home_hit = any(kw in title for kw in home_kw)
    away_hit = any(kw in title for kw in away_kw)
    if home_hit and away_hit:
        return 2
    if home_hit or away_hit:
        return 1
    return 0


def find_market_for_ml_pick(client: KalshiClient, pick: dict) -> dict:
    """
    Try to find the YES side of the picked team's win-contract on Kalshi.

    Returns a dict:
      {
        "status": "matched" | "ambiguous" | "no_event" | "no_market" | "unsupported",
        "pick": pick,
        "event_ticker": "...",     (when matched)
        "market_ticker": "...",    (when matched)
        "yes_side": "YES" | "NO",  (which side of the binary contract is the picked team)
        "current_yes_bid": int,    (cents 0-99, when matched)
        "current_yes_ask": int,
        "candidates": [...]        (for debugging when ambiguous)
      }
    """
    sport = pick.get("sport", "").upper()
    series = SPORT_TO_SERIES.get(sport)
    if not series:
        return {"status": "unsupported", "reason": f"No Kalshi series for sport {sport}", "pick": pick}
    bet_type = pick.get("betType")
    if bet_type != "ml":
        return {"status": "unsupported", "reason": f"Phase 1 supports ML only (got {bet_type})", "pick": pick}

    home = pick.get("home", "")
    away = pick.get("away", "")
    picked = _picked_team_name(pick)
    if not picked:
        return {"status": "unsupported", "reason": "Could not determine picked team", "pick": pick}

    # Step 1: find candidate events matching the (home, away) pair within the series.
    events_resp = client.list_events(status="open", series_ticker=series, limit=200)
    events = events_resp.get("events", [])
    scored = [(e, _event_matches_game(e, sport, home, away)) for e in events]
    strong = [e for e, s in scored if s == 2]
    weak   = [e for e, s in scored if s == 1]

    if not strong and not weak:
        return {"status": "no_event", "reason": f"No Kalshi event matched {away} @ {home}",
                "pick": pick, "candidates_scanned": len(events)}
    if len(strong) > 1:
        return {"status": "ambiguous",
                "reason": f"{len(strong)} strong event matches — manual review",
                "pick": pick,
                "candidates": [{"ticker": e.get("ticker"), "title": e.get("title")} for e in strong]}
    event = strong[0] if strong else (weak[0] if len(weak) == 1 else None)
    if event is None:
        return {"status": "ambiguous", "reason": "Multiple weak event matches",
                "pick": pick,
                "candidates": [{"ticker": e.get("ticker"), "title": e.get("title")} for e in weak]}

    event_ticker = event.get("ticker") or event.get("event_ticker")
    if not event_ticker:
        return {"status": "no_event", "reason": "Event has no ticker field", "pick": pick}

    # Step 2: list markets within that event, find the picked team's win contract.
    markets_resp = client.list_markets(status="open", event_ticker=event_ticker, limit=100)
    markets = markets_resp.get("markets", [])
    picked_kw = _team_keywords(picked)

    # Kalshi creates SEPARATE markets per team for moneyline games, e.g.:
    #   KXNHLGAME-26MAY09CARPHI-CAR  → YES = Carolina wins
    #   KXNHLGAME-26MAY09CARPHI-PHI  → YES = Philadelphia wins
    # We must match the market where YES corresponds to OUR picked team —
    # never use market title as a fallback (it contains both team names and
    # always matches, leading to wrong-side bets). Verified manually on
    # demo.kalshi.co market KXNHLGAME-26MAY09CARPHI-PHI on 2026-05-09.
    matched_market = None
    matched_side = "YES"

    # Strongest signal: yes_sub_title explicitly names the team that wins
    # makes YES resolve true (e.g., "PHI Flyers" or "Carolina Hurricanes").
    for m in markets:
        yes_sub = (m.get("yes_sub_title", "") or m.get("yes_subtitle", "") or "").lower()
        if yes_sub and any(kw in yes_sub for kw in picked_kw):
            matched_market = m; matched_side = "YES"; break

    # Backup signal: ticker suffix. Kalshi tickers end in a 2-4 letter team
    # abbreviation after the last dash (e.g., '-CAR', '-PHI'). If the picked
    # team's known abbreviation matches a ticker suffix, that's our market.
    if not matched_market:
        picked_abbrs = _team_abbreviations(picked)
        for m in markets:
            ticker = (m.get("ticker") or "").upper()
            suffix = ticker.rsplit("-", 1)[-1] if "-" in ticker else ""
            if suffix and 2 <= len(suffix) <= 4 and suffix in picked_abbrs:
                matched_market = m; matched_side = "YES"; break

    # NO-side check: rare but possible if Kalshi only listed the opposing
    # team's market and our picked team appears in its no_sub_title.
    if not matched_market:
        for m in markets:
            no_sub = (m.get("no_sub_title", "") or m.get("no_subtitle", "") or "").lower()
            if no_sub and any(kw in no_sub for kw in picked_kw):
                matched_market = m; matched_side = "NO"; break

    if not matched_market:
        return {"status": "no_market",
                "reason": (f"Event {event_ticker} has no market where YES = {picked} wins. "
                           f"Available markets: {[m.get('ticker') for m in markets]}"),
                "pick": pick, "event_ticker": event_ticker,
                "available_markets": [m.get("ticker") for m in markets]}

    yes_bid = matched_market.get("yes_bid")
    yes_ask = matched_market.get("yes_ask")
    return {
        "status": "matched",
        "pick": pick,
        "event_ticker": event_ticker,
        "market_ticker": matched_market.get("ticker"),
        "market_title": matched_market.get("title"),
        "yes_side": matched_side,  # 'YES' = bet YES contract, 'NO' = bet NO contract
        "current_yes_bid_cents": yes_bid,
        "current_yes_ask_cents": yes_ask,
    }


def map_picks(client: KalshiClient, picks: list[dict]) -> list[dict]:
    """Run pick→market mapping over a list of picks. Returns one result per pick."""
    return [find_market_for_ml_pick(client, p) for p in picks]
