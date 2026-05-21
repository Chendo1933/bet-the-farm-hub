#!/usr/bin/env python3
"""
MLB Over/Under backtest harness.

Replays every historical MLB game in data/results/*.json against multiple
model configurations and reports hit rate + ROI for each. Useful for
validating whether the park/pitcher/wind signals in hbScoreOU actually
add edge over the baseline (just team O/U records).

Models compared (run all in one pass):
  M0  always_over     — sanity baseline; bet Over every game
  M1  always_under    — sanity baseline; bet Under every game
  M2  hist_only       — historical O/U records of both teams (current hub
                        signal pre-2026-05-21)
  M3  hist + park     — adds MLB_PARK_FACTORS as standalone signal
  M4  park_only       — park factor alone, no team history

Limitations of this backtest:
  • No weather (would need historical NWS or Open-Meteo archive call per
    game). Park is static, so park-only mode is comparable to live.
  • No pitcher matchup (would need to scrape MLB historical starters and
    their cumulative-ERA at game time). Add when we wire that data in.
  • Hist records are reconstructed chronologically from the archive —
    a team's first ~5 games carry no signal (Bayesian gate). Whole-
    season records weren't available at game-1 in the live model
    either, so this is a fair comparison.
  • Hub also gates picks via min_confidence ≥ 0.55. We replicate that.

Usage:
  python3 scripts/backtest_ou.py             # full run, all models
  python3 scripts/backtest_ou.py --year 2026 # filter to one season
  python3 scripts/backtest_ou.py --csv out.csv  # also dump per-game CSV

Output:
  Per-model hit rate, ROI at -110 juice, sample size, and the magnitude
  of edge each signal adds over the baseline.
"""
from __future__ import annotations

import argparse
import csv
import glob
import json
import sys
from collections import defaultdict
from pathlib import Path
from typing import Optional

# Same table as the hub's MLB_PARK_FACTORS. Keep in sync if tuned.
MLB_PARK_FACTORS = {
    'Baltimore Orioles': 1.00, 'Boston Red Sox': 1.08, 'New York Yankees': 1.03,
    'Tampa Bay Rays': 0.96, 'Toronto Blue Jays': 1.02,
    'Chicago White Sox': 1.01, 'Cleveland Guardians': 0.95, 'Detroit Tigers': 0.97,
    'Kansas City Royals': 0.98, 'Minnesota Twins': 1.03,
    'Houston Astros': 0.97, 'Los Angeles Angels': 1.01, 'Athletics': 0.99,
    'Seattle Mariners': 0.93, 'Texas Rangers': 1.05,
    'Atlanta Braves': 1.01, 'Miami Marlins': 0.95, 'New York Mets': 0.97,
    'Philadelphia Phillies': 1.02, 'Washington Nationals': 1.01,
    'Chicago Cubs': 1.04, 'Cincinnati Reds': 1.05, 'Milwaukee Brewers': 0.97,
    'Pittsburgh Pirates': 0.98, 'St. Louis Cardinals': 0.99,
    'Arizona Diamondbacks': 1.02, 'Colorado Rockies': 1.28, 'Los Angeles Dodgers': 0.98,
    'San Diego Padres': 0.93, 'San Francisco Giants': 0.92,
}

# Same conf thresholds as hbScoreOU's `if(finalScore < 0.55)return null`.
MIN_PICK_CONFIDENCE = 0.55

# Standard -110 juice: bet 1.10 to win 1.00. Push = stake returned. Break-even
# hit rate is 52.38%. Tracked as units won (1 unit = stake at risk = 1.10).
def units_for(outcome: str) -> float:
    # outcome: 'win' / 'loss' / 'push'
    if outcome == 'win':  return 1.00 / 1.10   # ≈ +0.9091
    if outcome == 'loss': return -1.00
    return 0.0


def hist_signal(team_records: dict, home: str, away: str) -> Optional[float]:
    """
    Bayesian-shrunk over-probability from the two teams' cumulative O/U
    records UP TO (but not including) the current game. Returns None when
    either team has < 5 graded over/under games (matches hub's gate).

    Identical math to hbScoreOU's histSignal branch.
    """
    h = team_records.get(home, {"ov": 0, "un": 0})
    a = team_records.get(away, {"ov": 0, "un": 0})
    hov, hun = h["ov"], h["un"]
    aov, aun = a["ov"], a["un"]
    if (hov + hun) < 5 or (aov + aun) < 5:
        return None
    hop = hov / (hov + hun)
    aop = aov / (aov + aun)
    h_conf = min(1.0, (hov + hun) / 20.0)
    a_conf = min(1.0, (aov + aun) / 20.0)
    h_shrunk = hop * h_conf + 0.5 * (1 - h_conf)
    a_shrunk = aop * a_conf + 0.5 * (1 - a_conf)
    return (h_shrunk + a_shrunk) / 2


def park_signal(home: str) -> Optional[float]:
    """
    Park factor → Over-probability nudge. Returns None when park isn't
    in the table (shouldn't happen for any of the 30 MLB teams).
    Matches hub's hbScoreOU parkSignal branch:
      ≥1.06 → 0.62 · ≥1.03 → 0.55 · ≤0.94 → 0.38 · ≤0.96 → 0.45
    """
    pf = MLB_PARK_FACTORS.get(home)
    if pf is None:
        return None
    if pf >= 1.06: return 0.62
    if pf >= 1.03: return 0.55
    if pf <= 0.94: return 0.38
    if pf <= 0.96: return 0.45
    return None  # mid-range parks contribute no signal


# ── Pitcher signal helpers ────────────────────────────────────────────────
# Loaded once from data/pitcher_data.json (produced by backfill_pitcher_data.py).
# Game logs are per-start raw stats; we compute cumulative ERA/WHIP/FIP
# up to (but not including) a given date for each pitcher.

PITCHER_DATA: dict = {}   # populated by load_pitcher_data()
# Lookup index: {(date, home_team, away_team): {home_id, away_id, ...}}
# Built once on load because the MLB Stats API uses gamePk while ESPN
# results files use a different game_id, so we can't join on IDs. We
# build an index keyed by (date, team names) which IS stable across
# both data sources after normalizing team names.
PITCHER_INDEX: dict = {}

# ESPN team name → MLB Stats API team name (when they differ)
_TEAM_NAME_FIXES = {
    "Athletics": "Athletics",
    "Oakland Athletics": "Athletics",
    "Sacramento Athletics": "Athletics",
    # Add more if a future audit surfaces them
}


def _norm_team(name: str) -> str:
    """Normalize team name for cross-source matching."""
    return _TEAM_NAME_FIXES.get(name, name)


def load_pitcher_data():
    """Load the historical pitcher cache + build the (date, teams) index."""
    global PITCHER_DATA, PITCHER_INDEX
    if PITCHER_DATA: return
    try:
        PITCHER_DATA = json.loads(Path("data/pitcher_data.json").read_text())
    except FileNotFoundError:
        print("⚠ No pitcher data — run scripts/backfill_pitcher_data.py first")
        PITCHER_DATA = {"starters_by_gamePk": {}, "game_logs": {}}
        return
    # Rebuild the (date, home, away) → starters index. Date comes from the
    # game log we have — but the starter entry itself doesn't carry the
    # date. We'll fill date in from the first appearance of each gamePk
    # found in pitcher game logs.
    starter_dates: dict = {}
    for pid, starts in PITCHER_DATA.get("game_logs", {}).items():
        for s in starts:
            # Each start has 'date' but not gamePk in our schema. We can
            # join by (date, team) since pitcher game logs include team.
            # Actually we just need the starters_by_gamePk entries indexed
            # by (date, home_team, away_team) — date must come from the
            # results files we'll be walking. Build the team-only index
            # here and look up date at query time.
            pass
    # The starters_by_gamePk entries don't have a date field — they were
    # fetched per-date but we discarded the date. So we add a fallback
    # path that searches for any entry matching (home_team, away_team)
    # whose starter pitched on or near the query date.
    by_team = {}
    for pk, entry in PITCHER_DATA.get("starters_by_gamePk", {}).items():
        ht = _norm_team(entry.get("home_team") or "")
        at_ = _norm_team(entry.get("away_team") or "")
        if not ht or not at_: continue
        # Map (home, away) → list of (gamePk, starters) — most matchups
        # repeat across the season (series of 3 games).
        by_team.setdefault((ht, at_), []).append((pk, entry))
    PITCHER_INDEX = by_team


def _cumulative_stats(starts: list, before_date: str) -> Optional[dict]:
    """
    Compute cumulative ERA + WHIP + FIP from all starts STRICTLY BEFORE
    `before_date`. Returns None when the pitcher has 0 prior starts (no
    signal possible — they're a fresh callup or first start of season).

    FIP = ((13*HR + 3*BB - 2*K) / IP) + constant ~3.10
      Defense-independent — measures only what the pitcher controls.
      Lower FIP = better. We use it preferentially over ERA when
      sample is decent (≥3 starts), because ERA on small samples is
      heavily skewed by BABIP / sequencing luck.
    """
    prior = [s for s in (starts or []) if s.get("date") and s["date"] < before_date]
    if not prior:
        return None
    total_ip = sum(s.get("ip", 0) for s in prior)
    if total_ip < 1: return None
    total_er = sum(s.get("er", 0) for s in prior)
    total_h  = sum(s.get("h", 0) for s in prior)
    total_bb = sum(s.get("bb", 0) for s in prior)
    total_k  = sum(s.get("k", 0) for s in prior)
    total_hr = sum(s.get("hr", 0) for s in prior)
    era  = (9 * total_er) / total_ip
    whip = (total_h + total_bb) / total_ip
    fip  = ((13 * total_hr + 3 * total_bb - 2 * total_k) / total_ip) + 3.10
    return {
        "era":   round(era, 2),
        "whip":  round(whip, 2),
        "fip":   round(fip, 2),
        "ip":    round(total_ip, 1),
        "starts": len(prior),
    }


def pitcher_signal(home: str, away: str, date: str) -> Optional[float]:
    """
    Returns the pitcher-matchup O/U signal for one game, or None when
    we don't have data for either starter.

    Looks up starters by (home_team, away_team, date) since ESPN
    game_id ≠ MLB Stats API gamePk. For each matchup we find candidate
    starter pairs and pick the one whose game log dates contain `date`
    (the actual game we're scoring). When multiple matches exist (e.g.
    doubleheaders), we accept any — the same starters typically pitch
    both games anyway, and even if not, the pitcher quality tier is
    usually the same.

    Mirrors hub's hbScoreOU pitcher branch:
      Tier: elite ≤3.25 · quality ≤4.00 · average ≤4.75 · weak >4.75
      Both aces (avg ≤3.25)   → 0.25 (strong Under)
      Both elite (avg ≤3.75)  → 0.32 (Under lean)
      Both quality+ (avg >3.75) → 0.40 (slight Under)
      Both weak  (avg ≥5.25)  → 0.75 (strong Over)
      Both weak  (avg ≥4.75)  → 0.65 (Over lean)
      Ace vs liability → None (cancels — no O/U signal)
    """
    ht, at_ = _norm_team(home), _norm_team(away)
    candidates = PITCHER_INDEX.get((ht, at_), [])
    if not candidates: return None

    # Find the candidate whose home OR away starter pitched on `date`.
    # That's our matchup. If none match exactly (rare — usually a name
    # mismatch in normalization), fall back to the first candidate.
    h_id = a_id = None
    for _pk, entry in candidates:
        cand_h = entry.get("home_id")
        cand_a = entry.get("away_id")
        if not cand_h or not cand_a: continue
        h_log = PITCHER_DATA.get("game_logs", {}).get(str(cand_h), [])
        a_log = PITCHER_DATA.get("game_logs", {}).get(str(cand_a), [])
        h_pitched_today = any(s.get("date") == date for s in h_log)
        a_pitched_today = any(s.get("date") == date for s in a_log)
        if h_pitched_today and a_pitched_today:
            h_id, a_id = cand_h, cand_a
            break
    if h_id is None or a_id is None:
        # No exact match — skip (was probably an early-season game where
        # we don't have the probable pitcher data, or a doubleheader edge case)
        return None

    h_log = PITCHER_DATA.get("game_logs", {}).get(str(h_id), [])
    a_log = PITCHER_DATA.get("game_logs", {}).get(str(a_id), [])
    h_stats = _cumulative_stats(h_log, date)
    a_stats = _cumulative_stats(a_log, date)
    if not h_stats or not a_stats: return None

    # Prefer FIP when both pitchers have ≥3 starts (FIP needs the
    # sample to be stable — on 1-2 starts it's basically the HR rate
    # of two outings). Else fall back to ERA.
    use_fip = h_stats["starts"] >= 3 and a_stats["starts"] >= 3
    h_val = h_stats["fip"] if use_fip else h_stats["era"]
    a_val = a_stats["fip"] if use_fip else a_stats["era"]

    def tier(v: float) -> str:
        if v <= 3.25: return "e"
        if v <= 4.00: return "q"
        if v <= 4.75: return "a"
        return "w"

    h_t, a_t = tier(h_val), tier(a_val)
    avg = (h_val + a_val) / 2
    both_good = h_t in ("e", "q") and a_t in ("e", "q")
    both_weak = h_t == "w" and a_t == "w"

    if both_good:
        if avg <= 3.25: return 0.25
        if avg <= 3.75: return 0.32
        return 0.40
    if both_weak:
        if avg >= 5.25: return 0.75
        return 0.65
    # Ace vs liability: cancels, no signal
    return None


def model_prediction(model: str, home: str, away: str,
                     team_records: dict,
                     game_date: Optional[str] = None) -> Optional[float]:
    """
    Returns the predicted P(Over) for the given model, or None if the model
    can't produce a pick on this game (insufficient data, no signal, etc.).
    """
    if model == "always_over":
        return 0.99
    if model == "always_under":
        return 0.01
    if model == "hist_only":
        return hist_signal(team_records, home, away)
    if model == "park_only":
        return park_signal(home)
    if model == "pitcher_only":
        if game_date is None: return None
        return pitcher_signal(home, away, game_date)
    if model == "hist_plus_park":
        h = hist_signal(team_records, home, away)
        p = park_signal(home)
        wsum, vsum = 0.0, 0.0
        if h is not None: vsum += h * 0.55; wsum += 0.55
        if p is not None: vsum += p * 0.15; wsum += 0.15
        if wsum == 0: return None
        if h is not None and p is not None and (h > 0.5) != (p > 0.5):
            return None
        return vsum / wsum
    if model == "full_blend":
        # Mirrors the live hub formula: hist 0.55, park 0.15, pitcher 0.20.
        # All three signals must agree in direction OR only one fires
        # (single-signal mode falls back to that signal alone).
        h = hist_signal(team_records, home, away)
        p = park_signal(home)
        pit = pitcher_signal(home, away, game_date) if game_date else None
        signals = [(h, 0.55), (p, 0.15), (pit, 0.20)]
        firing = [(s, w) for s, w in signals if s is not None]
        if not firing: return None
        # Direction agreement check — when ≥2 signals fire, they must all
        # point the same way (over vs under). Disagreement = no pick.
        # This is the hub's "no contradictory signals" rule.
        dirs = [s > 0.5 for s, _ in firing]
        if len(set(dirs)) > 1: return None
        vsum = sum(s * w for s, w in firing)
        wsum = sum(w for _, w in firing)
        return vsum / wsum
    raise ValueError(f"Unknown model: {model}")


def pick_from_prob(over_prob: Optional[float], threshold: float = MIN_PICK_CONFIDENCE) -> Optional[str]:
    """Returns 'over', 'under', or None (no pick)."""
    if over_prob is None:
        return None
    conf = max(over_prob, 1 - over_prob)
    if conf < threshold:
        return None
    return "over" if over_prob > 0.5 else "under"


def grade_pick(pick: Optional[str], actual_total: int, line: float) -> Optional[str]:
    """Grade a pick against the actual outcome. Returns win/loss/push/None."""
    if pick is None:
        return None
    if actual_total == line:  # exact push
        return "push"
    actual_over = actual_total > line
    if pick == "over":
        return "win" if actual_over else "loss"
    return "win" if not actual_over else "loss"


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--year", type=int, help="Filter to one calendar year")
    ap.add_argument("--csv", help="Dump per-game results to CSV")
    ap.add_argument("--threshold", type=float, default=MIN_PICK_CONFIDENCE,
                    help=f"Confidence threshold to make a pick (default {MIN_PICK_CONFIDENCE})")
    ap.add_argument("--sweep", action="store_true",
                    help="Run a sweep across confidence thresholds 0.52, 0.55, 0.60, 0.65, 0.70")
    args = ap.parse_args()

    if args.sweep:
        # Run the full backtest at multiple confidence thresholds so we can
        # see whether high-conf picks are profitable even if mid-conf picks
        # aren't. This is the most useful diagnostic: maybe the signal IS
        # real at the top of the distribution but gets diluted by noisy
        # mid-confidence picks.
        for thresh in [0.52, 0.55, 0.60, 0.65, 0.70]:
            print(f"\n{'='*72}\n  Confidence threshold: {thresh}\n{'='*72}")
            _run_backtest(args, thresh)
        return
    _run_backtest(args, args.threshold)


def _run_backtest(args, threshold: float):

    # Walk all results files chronologically. The archive's filename IS the
    # ET date, so sorting by filename gives correct chronological order.
    files = sorted(glob.glob("data/results/*.json"))
    files = [f for f in files if "/index.json" not in f]
    if args.year:
        files = [f for f in files if f"/{args.year}-" in f]
    print(f"Loaded {len(files)} result files "
          f"({files[0].split('/')[-1] if files else '?'} → "
          f"{files[-1].split('/')[-1] if files else '?'})")

    # Load historical pitcher data for the pitcher-aware models. Cheap —
    # one JSON read. Safe — if file doesn't exist, pitcher signal silently
    # returns None and those models just produce 0 picks (rather than
    # crashing).
    load_pitcher_data()
    pitchers_loaded = sum(1 for v in PITCHER_DATA.get("game_logs", {}).values() if v)
    print(f"  Pitcher data: {len(PITCHER_DATA.get('starters_by_gamePk', {}))} game→starter "
          f"mappings, {pitchers_loaded} pitchers with logs")

    # Mutable team O/U records — incremented AFTER each game is scored.
    # Must walk in chronological order so any model that reads `team_records`
    # at game N only sees games 1..N-1.
    team_records: dict = defaultdict(lambda: {"ov": 0, "un": 0})

    MODELS = ["always_over", "always_under", "hist_only", "park_only",
              "pitcher_only", "hist_plus_park", "full_blend"]
    # Per-model running tally: {model: {picks, wins, losses, pushes, units}}
    tallies = {m: {"picks": 0, "wins": 0, "losses": 0, "pushes": 0, "units": 0.0}
               for m in MODELS}

    csv_rows = []
    games_seen = 0

    for fpath in files:
        try:
            data = json.loads(Path(fpath).read_text())
        except Exception as e:
            print(f"  ⚠ Could not parse {fpath}: {e}")
            continue
        date = data.get("date", Path(fpath).stem)

        mlb = data.get("sports", {}).get("mlb", [])
        if not isinstance(mlb, list):
            continue

        for g in mlb:
            line = g.get("total")
            hs   = g.get("home_score")
            as_  = g.get("away_score")
            home = g.get("home_db") or g.get("home")
            away = g.get("away_db") or g.get("away")
            game_pk = g.get("game_id")
            if line is None or hs is None or as_ is None or not home or not away:
                continue   # not gradeable
            games_seen += 1
            actual_total = hs + as_

            # Run each model and grade
            game_picks = {}
            for m in MODELS:
                over_prob = model_prediction(m, home, away, team_records,
                                             game_date=date)
                pick = pick_from_prob(over_prob, threshold)
                outcome = grade_pick(pick, actual_total, line)
                game_picks[m] = (pick, outcome)
                if outcome:
                    tallies[m]["picks"] += 1
                    if outcome == "win":   tallies[m]["wins"] += 1
                    elif outcome == "loss": tallies[m]["losses"] += 1
                    elif outcome == "push": tallies[m]["pushes"] += 1
                    tallies[m]["units"] += units_for(outcome)

            if args.csv:
                row = {"date": date, "home": home, "away": away,
                       "line": line, "actual_total": actual_total,
                       "result": "over" if actual_total > line else ("under" if actual_total < line else "push")}
                for m in MODELS:
                    p, o = game_picks[m]
                    row[f"{m}_pick"] = p or ""
                    row[f"{m}_result"] = o or ""
                csv_rows.append(row)

            # ── After grading, increment running records for next game ──
            if actual_total > line:
                team_records[home]["ov"] += 1
                team_records[away]["ov"] += 1
            elif actual_total < line:
                team_records[home]["un"] += 1
                team_records[away]["un"] += 1
            # exact push: don't increment either bucket (matches hub)

    # ── Report ──
    print(f"\nGames graded: {games_seen}")
    print(f"\n{'model':<20} {'picks':>6} {'W-L-P':>10} {'hit%':>8} {'units':>9} {'ROI':>8}")
    print("-" * 70)
    for m in MODELS:
        t = tallies[m]
        n = t["picks"]
        wl = t["wins"] + t["losses"]
        hit = (100 * t["wins"] / wl) if wl else 0
        roi = (100 * t["units"] / (n * 1.10)) if n else 0   # n bets × $1.10 risked each
        print(f"{m:<20} {n:>6} {t['wins']}-{t['losses']}-{t['pushes']:<5} "
              f"{hit:>7.1f}% {t['units']:>+8.2f}u {roi:>+7.1f}%")

    print(f"\nBreak-even hit rate at -110 juice: 52.38%")
    print(f"Break-even ROI: 0% (anything positive is profitable)")

    if args.csv:
        with open(args.csv, "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=list(csv_rows[0].keys()))
            writer.writeheader()
            writer.writerows(csv_rows)
        print(f"\nPer-game CSV dumped → {args.csv}")


if __name__ == "__main__":
    main()
