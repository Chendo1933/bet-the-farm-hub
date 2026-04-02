#!/usr/bin/env python3
"""
Bet The Farm Hub — Data schema definitions and validators.

Every JSON file in data/ has a contract. This module defines those
contracts and provides validate() so any script can check its output
before writing — turning silent data corruption into a loud crash.

Usage:
    from schemas import validate

    data = build_my_output()
    validate("performance", data)   # raises SchemaError if wrong
    with open(path, "w") as f:
        json.dump(data, f, indent=2)

Available schema names:
    "performance"  — data/performance.json
    "results"      — data/results/YYYY-MM-DD.json
    "picks"        — data/picks/YYYY-MM-DD.json
    "schedule"     — data/schedules/YYYY-MM-DD.json
"""

from __future__ import annotations


class SchemaError(Exception):
    """Raised when a data dict doesn't match its expected schema."""
    pass


# ── Schema definitions ────────────────────────────────────────────────────────
# Each entry is a list of (description, test_fn) pairs. All tests must pass.

_SCHEMAS: dict[str, list[tuple[str, callable]]] = {

    "performance": [
        ("must be a dict",
            lambda d: isinstance(d, dict)),
        ("must have key 'tiers' (not 'records')",
            lambda d: "tiers" in d),
        ("'tiers' must be a dict",
            lambda d: isinstance(d.get("tiers"), dict)),
        ("'tiers' must contain 'elite'",
            lambda d: "elite" in d.get("tiers", {})),
        ("'tiers' must contain 'strong'",
            lambda d: "strong" in d.get("tiers", {})),
        ("each tier must have 'w', 'l', 'p' keys (not wins/losses/pushes)",
            lambda d: all(
                all(k in tier for k in ("w", "l", "p"))
                for tier in d.get("tiers", {}).values()
            )),
        ("must have key 'last_updated' (not 'updated')",
            lambda d: "last_updated" in d),
        ("must have key 'graded_dates' as a list",
            lambda d: isinstance(d.get("graded_dates"), list)),
    ],

    "results": [
        ("must be a dict",
            lambda d: isinstance(d, dict)),
        ("must have key 'sports' (not 'games')",
            lambda d: "sports" in d),
        ("'sports' must be a dict keyed by sport name",
            lambda d: isinstance(d.get("sports"), dict)),
        ("'sports' values must be lists",
            lambda d: all(isinstance(v, list) for v in d.get("sports", {}).values())),
        ("must have key 'date'",
            lambda d: "date" in d),
        ("must have key 'logged'",
            lambda d: "logged" in d),
    ],

    "picks": [
        ("must be a dict",
            lambda d: isinstance(d, dict)),
        ("must have key 'picks' as a list",
            lambda d: isinstance(d.get("picks"), list)),
        ("must have key 'date'",
            lambda d: "date" in d),
        ("must have key 'logged'",
            lambda d: "logged" in d),
        ("each pick must have 'sport', 'tier', 'betType', 'home', 'away'",
            lambda d: all(
                all(k in p for k in ("sport", "tier", "betType", "home", "away"))
                for p in d.get("picks", [])
            )),
        ("each pick tier must be 'elite', 'strong', 'good', or 'lean'",
            lambda d: all(
                p.get("tier") in ("elite", "strong", "good", "lean")
                for p in d.get("picks", [])
            )),
    ],

    "schedule": [
        ("must be a dict",
            lambda d: isinstance(d, dict)),
        ("must have key 'games' as a list",
            lambda d: isinstance(d.get("games"), list)),
        ("must have key 'date'",
            lambda d: "date" in d),
        ("must have key 'logged'",
            lambda d: "logged" in d),
    ],
}


def validate(schema_name: str, data: object, *, raise_on_error: bool = True) -> list[str]:
    """
    Validate `data` against the named schema.

    Args:
        schema_name:    One of "performance", "results", "picks", "schedule".
        data:           The Python object to validate (usually a dict).
        raise_on_error: If True (default), raise SchemaError on failure.
                        If False, return a list of error strings (empty = valid).

    Returns:
        List of error strings. Empty list means valid.

    Raises:
        SchemaError: If raise_on_error=True and any check fails.
        KeyError:    If schema_name is not recognised.
    """
    if schema_name not in _SCHEMAS:
        raise KeyError(f"Unknown schema '{schema_name}'. "
                       f"Valid names: {', '.join(_SCHEMAS)}")

    errors: list[str] = []
    for description, test_fn in _SCHEMAS[schema_name]:
        try:
            if not test_fn(data):
                errors.append(description)
        except Exception as exc:
            errors.append(f"{description} [check raised {type(exc).__name__}: {exc}]")

    if errors and raise_on_error:
        bullet_list = "\n  · ".join(errors)
        raise SchemaError(
            f"Schema validation failed for '{schema_name}':\n  · {bullet_list}\n\n"
            f"This means a script is about to write data the hub can't read. "
            f"Fix the output dict before writing to disk."
        )

    return errors


def check(schema_name: str, data: object) -> bool:
    """
    Convenience wrapper — returns True if valid, False if not.
    Never raises; prints warnings to stdout.
    """
    errors = validate(schema_name, data, raise_on_error=False)
    if errors:
        print(f"  ⚠  Schema check FAILED for '{schema_name}':")
        for e in errors:
            print(f"     · {e}")
        return False
    return True


if __name__ == "__main__":
    # Quick self-test
    print("Running schema self-tests...")

    # Valid performance
    good_perf = {
        "last_updated": "2026-04-01",
        "tiers": {"elite": {"w": 5, "l": 3, "p": 0}, "strong": {"w": 8, "l": 6, "p": 1}},
        "by_sport": {},
        "graded_dates": ["2026-04-01"],
    }
    assert validate("performance", good_perf, raise_on_error=False) == [], "good_perf should pass"

    # Old broken performance schema
    bad_perf = {"updated": "2026-04-01", "records": {"elite": {"wins": 5, "losses": 3}}}
    errors = validate("performance", bad_perf, raise_on_error=False)
    assert len(errors) > 0, "bad_perf should fail"
    print(f"  ✓ Old 'records' schema correctly rejected ({len(errors)} error(s))")

    # Valid results
    good_results = {
        "date": "2026-04-01",
        "logged": "2026-04-01T03:00:00Z",
        "sports": {"nba": [{"home": "Boston Celtics", "away": "Miami Heat",
                             "home_score": 108, "away_score": 99}]},
    }
    assert validate("results", good_results, raise_on_error=False) == [], "good_results should pass"

    # Old broken results schema (flat 'games' list)
    bad_results = {"date": "2026-04-01", "logged": "...", "games": [{"home": "Boston"}]}
    errors = validate("results", bad_results, raise_on_error=False)
    assert len(errors) > 0, "bad_results should fail"
    print(f"  ✓ Old 'games' schema correctly rejected ({len(errors)} error(s))")

    print("All self-tests passed ✅")
