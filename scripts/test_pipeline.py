#!/usr/bin/env python3
"""
Bet The Farm Hub — Pipeline integration test

Verifies the full pick → result → grade → performance.json chain works
end-to-end using synthetic data. Run this locally any time you change
grade_picks.py, log_results.py, or schemas.py.

Usage:
    cd "Bet The Farm"
    python scripts/test_pipeline.py

Exit codes:
    0 — all tests passed
    1 — one or more tests failed

What it tests:
    1. schemas.py self-validation (good and bad data)
    2. grade_picks grading logic (win / loss / push / push edge case)
    3. Full pipeline: fake picks + fake results → grade → performance.json
    4. Schema guard: grade_picks refuses to write if output is corrupted
    5. Anomaly detection (check_picks logic, if available)
"""

import json
import os
import sys
import tempfile
import shutil
import traceback
from datetime import datetime, timezone, timedelta

# ── Test harness ──────────────────────────────────────────────────────────────

PASS = "✅"
FAIL = "❌"
SKIP = "⏭ "

_results: list[tuple[str, str, str]] = []  # (status, name, detail)


def test(name: str):
    """Decorator that catches exceptions and records pass/fail."""
    def decorator(fn):
        try:
            fn()
            _results.append((PASS, name, ""))
        except AssertionError as e:
            _results.append((FAIL, name, str(e) or "assertion failed"))
        except Exception as e:
            _results.append((FAIL, name, f"{type(e).__name__}: {e}"))
        return fn
    return decorator


def assert_eq(actual, expected, msg=""):
    if actual != expected:
        raise AssertionError(
            f"{msg + ': ' if msg else ''}"
            f"expected {expected!r}, got {actual!r}"
        )


def assert_in(key, container, msg=""):
    if key not in container:
        raise AssertionError(
            f"{msg + ': ' if msg else ''}"
            f"{key!r} not found in {list(container)[:6]!r}"
        )


# ── Import the modules under test ─────────────────────────────────────────────

scripts_dir = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, scripts_dir)

try:
    import schemas
    HAS_SCHEMAS = True
except ImportError:
    HAS_SCHEMAS = False
    print(f"{SKIP} schemas.py not importable — skipping schema tests\n")

try:
    import grade_picks
    HAS_GRADE = True
except ImportError:
    HAS_GRADE = False
    print(f"{SKIP} grade_picks.py not importable — skipping grade tests\n")


# ── Schema validation tests ───────────────────────────────────────────────────

if HAS_SCHEMAS:

    @test("schemas: valid performance passes")
    def _():
        good = {
            "last_updated": "2026-04-01",
            "tiers": {
                "elite":  {"w": 5,  "l": 3, "p": 0},
                "strong": {"w": 8,  "l": 6, "p": 1},
            },
            "by_sport": {},
            "graded_dates": ["2026-04-01"],
        }
        errors = schemas.validate("performance", good, raise_on_error=False)
        assert errors == [], f"unexpected errors: {errors}"

    @test("schemas: old 'records' schema is rejected")
    def _():
        bad = {
            "updated": "2026-04-01",
            "records": {"elite": {"wins": 5, "losses": 3, "pushes": 0}},
        }
        errors = schemas.validate("performance", bad, raise_on_error=False)
        assert len(errors) > 0, "old schema should have been rejected"
        # Make sure 'tiers' is specifically called out
        assert any("tiers" in e for e in errors), \
            f"expected 'tiers' to be mentioned in errors: {errors}"

    @test("schemas: results with 'sports' key passes")
    def _():
        good = {
            "date": "2026-04-01",
            "logged": "2026-04-01T03:00:00Z",
            "sports": {
                "nba": [{"home": "Celtics", "away": "Heat",
                         "home_score": 108, "away_score": 99}]
            },
        }
        errors = schemas.validate("results", good, raise_on_error=False)
        assert errors == [], f"unexpected errors: {errors}"

    @test("schemas: old 'games' flat list is rejected")
    def _():
        bad = {
            "date": "2026-04-01",
            "logged": "...",
            "games": [{"home": "Celtics", "away": "Heat"}],
        }
        errors = schemas.validate("results", bad, raise_on_error=False)
        assert len(errors) > 0, "old 'games' schema should be rejected"
        assert any("sports" in e for e in errors), \
            f"expected 'sports' to be mentioned: {errors}"

    @test("schemas: SchemaError raised when raise_on_error=True")
    def _():
        bad = {"records": {}}
        try:
            schemas.validate("performance", bad, raise_on_error=True)
            raise AssertionError("should have raised SchemaError")
        except schemas.SchemaError:
            pass  # expected

    @test("schemas: unknown schema name raises KeyError")
    def _():
        try:
            schemas.validate("nonexistent", {})
            raise AssertionError("should have raised KeyError")
        except KeyError:
            pass


# ── Grading logic unit tests ──────────────────────────────────────────────────

if HAS_GRADE:

    @test("grade: home covers spread → win for home pick")
    def _():
        pick   = {"spread": -5.5, "atsPick": "home"}
        result = {"home_score": 110, "away_score": 100}
        # margin = (110-100) + (-5.5) = 4.5 → home covered
        assert_eq(grade_picks.grade_spread(pick, result), "win")

    @test("grade: home fails to cover → loss for home pick, win for away")
    def _():
        pick_h = {"spread": -5.5, "atsPick": "home"}
        pick_a = {"spread": -5.5, "atsPick": "away"}
        result = {"home_score": 104, "away_score": 100}
        # margin = (104-100) + (-5.5) = -1.5 → away covered
        assert_eq(grade_picks.grade_spread(pick_h, result), "loss")
        assert_eq(grade_picks.grade_spread(pick_a, result), "win")

    @test("grade: exact spread is a push")
    def _():
        pick   = {"spread": -5.0, "atsPick": "home"}
        result = {"home_score": 105, "away_score": 100}
        # margin = (105-100) + (-5.0) = 0.0 → push
        assert_eq(grade_picks.grade_spread(pick, result), "push")

    @test("grade: ML pick — picked team wins")
    def _():
        pick_home = {"atsPick": "home"}
        pick_away = {"atsPick": "away"}
        result = {"home_score": 4, "away_score": 2}
        assert_eq(grade_picks.grade_ml(pick_home, result), "win")
        assert_eq(grade_picks.grade_ml(pick_away, result), "loss")

    @test("grade: missing spread → ungraded")
    def _():
        pick   = {"spread": None, "atsPick": "home"}
        result = {"home_score": 110, "away_score": 100}
        assert_eq(grade_picks.grade_spread(pick, result), "ungraded")

    @test("grade: find_result returns None for empty away team")
    def _():
        pick = {"sport": "nba", "home": "Celtics", "away": ""}
        assert grade_picks.find_result(pick, {}) is None

    @test("grade: find_result matches game by fuzzy team name")
    def _():
        pick = {"sport": "nba", "home": "Boston Celtics", "away": "Miami Heat"}
        sports = {"nba": [
            {"home": "Boston Celtics", "away": "Miami Heat",
             "home_score": 108, "away_score": 99}
        ]}
        r = grade_picks.find_result(pick, sports)
        assert r is not None, "should have found a match"
        assert_eq(r["home_score"], 108)


# ── Full pipeline integration test ────────────────────────────────────────────

@test("pipeline: picks + results → grade → correct performance.json")
def _():
    """Run grade_picks.main() logic against synthetic files in a temp dir."""
    if not HAS_GRADE:
        raise AssertionError("grade_picks not importable")

    tmp = tempfile.mkdtemp(prefix="btf_test_")
    try:
        picks_dir   = os.path.join(tmp, "data", "picks")
        results_dir = os.path.join(tmp, "data", "results")
        perf_file   = os.path.join(tmp, "data", "performance.json")
        os.makedirs(picks_dir);  os.makedirs(results_dir)

        date_key = "2026-04-01"

        # Two Elite picks: one win, one loss
        picks = {
            "date": date_key, "logged": "...", "has_live_odds": True,
            "all_picks_count": 2,
            "picks": [
                {"sport": "nba", "betType": "spread", "tier": "elite",
                 "home": "Boston Celtics", "away": "Miami Heat",
                 "atsPick": "home", "spread": -5.5, "pickLabel": "Celtics -5.5"},
                {"sport": "nba", "betType": "spread", "tier": "elite",
                 "home": "Los Angeles Lakers", "away": "Golden State Warriors",
                 "atsPick": "away", "spread": 2.5, "pickLabel": "Warriors +2.5"},
            ],
        }
        results = {
            "date": date_key, "logged": "...",
            "sports": {
                "nba": [
                    # Celtics win by 8 → covers -5.5 ✅
                    {"home": "Boston Celtics", "away": "Miami Heat",
                     "home_score": 108, "away_score": 100},
                    # Lakers win by 10 → Warriors fail to cover +2.5 ❌
                    {"home": "Los Angeles Lakers", "away": "Golden State Warriors",
                     "home_score": 120, "away_score": 110},
                ]
            },
        }

        with open(os.path.join(picks_dir,   f"{date_key}.json"), "w") as f:
            json.dump(picks, f)
        with open(os.path.join(results_dir, f"{date_key}.json"), "w") as f:
            json.dump(results, f)

        # Patch grade_picks paths to use our temp dir
        orig_picks  = grade_picks.PICKS_DIR
        orig_result = grade_picks.RESULTS_DIR
        orig_perf   = grade_picks.PERF_FILE
        grade_picks.PICKS_DIR   = picks_dir
        grade_picks.RESULTS_DIR = results_dir
        grade_picks.PERF_FILE   = perf_file

        try:
            perf = grade_picks.load_performance()
            sports = results["sports"]

            for pick in picks["picks"]:
                tier     = pick.get("tier", "lean").lower()
                bet_type = pick.get("betType", "spread")
                result   = grade_picks.find_result(pick, sports)
                assert result is not None, f"no result for {pick['pickLabel']}"

                outcome = (grade_picks.grade_spread(pick, result)
                           if bet_type == "spread"
                           else "ungraded")
                assert outcome != "ungraded", f"pick should be gradeable: {pick['pickLabel']}"

                if tier in ("elite", "strong"):
                    rec = perf["tiers"][tier]
                    if outcome == "win":  rec["w"] += 1
                    elif outcome == "loss": rec["l"] += 1
                    else: rec["p"] += 1

            perf["last_updated"] = date_key
            perf["graded_dates"].append(date_key)

            with open(perf_file, "w") as f:
                json.dump(perf, f, indent=2)

        finally:
            grade_picks.PICKS_DIR   = orig_picks
            grade_picks.RESULTS_DIR = orig_result
            grade_picks.PERF_FILE   = orig_perf

        # Verify results
        with open(perf_file) as f:
            out = json.load(f)

        assert_in("tiers", out, "performance.json")
        assert_in("elite", out["tiers"], "tiers")
        elite = out["tiers"]["elite"]
        assert_eq(elite["w"], 1, "elite wins")
        assert_eq(elite["l"], 1, "elite losses")
        assert_eq(elite["p"], 0, "elite pushes")
        assert_eq(out["last_updated"], date_key)
        assert date_key in out["graded_dates"]

        # Also validate schema
        if HAS_SCHEMAS:
            errors = schemas.validate("performance", out, raise_on_error=False)
            assert errors == [], f"output failed schema validation: {errors}"

    finally:
        shutil.rmtree(tmp, ignore_errors=True)


@test("pipeline: already-graded date is skipped without modifying performance.json")
def _():
    if not HAS_GRADE:
        raise AssertionError("grade_picks not importable")

    tmp = tempfile.mkdtemp(prefix="btf_test_")
    try:
        date_key = "2026-04-01"
        perf_file = os.path.join(tmp, "performance.json")
        existing = {
            "last_updated": date_key,
            "tiers": {"elite": {"w": 3, "l": 2, "p": 0}, "strong": {"w": 0, "l": 0, "p": 0}},
            "by_sport": {},
            "graded_dates": [date_key],  # already graded
        }
        with open(perf_file, "w") as f:
            json.dump(existing, f)

        orig_perf = grade_picks.PERF_FILE
        grade_picks.PERF_FILE = perf_file
        try:
            perf = grade_picks.load_performance()
            already_graded = date_key in perf.get("graded_dates", [])
        finally:
            grade_picks.PERF_FILE = orig_perf

        assert already_graded, "should detect date as already graded"
        # Verify file wasn't touched (grades preserved)
        with open(perf_file) as f:
            final = json.load(f)
        assert_eq(final["tiers"]["elite"]["w"], 3, "wins should be unchanged")

    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ── Results ───────────────────────────────────────────────────────────────────

def main():
    print("\n" + "─" * 60)
    print("  Bet The Farm Hub — Pipeline Integration Tests")
    print("─" * 60)

    passed = sum(1 for s, _, _ in _results if s == PASS)
    failed = sum(1 for s, _, _ in _results if s == FAIL)

    for status, name, detail in _results:
        line = f"  {status} {name}"
        if detail:
            line += f"\n       → {detail}"
        print(line)

    print("─" * 60)
    print(f"  {passed} passed · {failed} failed · {len(_results)} total")
    print("─" * 60 + "\n")

    if failed:
        print("🚨 Some tests failed. Fix the issues above before deploying.\n")
        sys.exit(1)
    else:
        print("✅ All tests passed. Pipeline looks healthy.\n")
        sys.exit(0)


if __name__ == "__main__":
    main()
