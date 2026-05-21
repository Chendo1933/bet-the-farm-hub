#!/usr/bin/env python3
"""
Bet The Farm Hub — NFL season reset (one-shot, manual)

Zeros out stale prior-season NFL stats in index.html so the hub starts the
2026 regular season with a clean slate. The daily auto-pipeline
(update_stats.py, scrape_ats.py) will then begin filling these back in once
real games are played.

WHEN TO RUN
  ~3 weeks before NFL Week 1 (e.g. mid-to-late August 2026). Running earlier
  just clears values that no one has time to see refilled — it confuses anyone
  who opens the hub in the meantime. Running later means the first few
  preseason/Week-1 games will be scored against last season's W/L deltas.

  As a guardrail the script refuses to run before Aug 15 of the current year
  unless you pass --force.

WHAT IT TOUCHES
  For every team row in `const NFL=[ ... ];` (index.html starting around line
  1513), the following array indices are set to 0:

    [3]  W                [9]  Home ATS L
    [4]  L                [10] Away ATS W
    [5]  ATS W            [11] Away ATS L
    [6]  ATS L            [12] Over
    [7]  ATS Push         [13] Under
    [8]  Home ATS W

  PPG ([15]) and PAPG ([16]) are LEFT ALONE by default — they're useful as
  preseason priors until you have ~3 games of real data. Pass --reset-scoring
  if you'd rather start them at 0 too.

  After the rows are patched, BTF_BASELINE_VER is bumped (default:
  "<today>-nfl-reset") so any localStorage W/L deltas on visitors' machines
  get cleared on next page load.

USAGE
  python scripts/reset_nfl_season.py --dry-run         # preview, no writes
  python scripts/reset_nfl_season.py                   # run (after Aug 15)
  python scripts/reset_nfl_season.py --reset-scoring   # also zero PPG/PAPG
  python scripts/reset_nfl_season.py --force           # bypass date guard
  python scripts/reset_nfl_season.py --version-tag 2026-08-20-nfl-reset

FOLLOW-UPS (not automated here — do manually after running this)
  1. Confirm scrape-ats.yml is pulling teamrankings.com 2026 NFL numbers
     once they start publishing (usually Week 1).
  2. Spot-check update_stats.py:update_nfl() against ESPN once Week 1 ends.
  3. Run `python scripts/update_injuries.py` to refresh training-camp injuries.
"""
import re
import sys
from datetime import date
from pathlib import Path

HUB_FILE = Path(__file__).resolve().parent.parent / "index.html"

# Always reset (W/L + ATS + home/away ATS + O/U)
DEFAULT_RESET_INDICES = [3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13]

# Additionally reset with --reset-scoring (PPG / PAPG)
SCORING_RESET_INDICES = [15, 16]

NFL_BLOCK_START = re.compile(r'^\s*const\s+NFL\s*=\s*\[')
NFL_BLOCK_END   = re.compile(r'^\s*\]\s*;')
TEAM_ROW        = re.compile(r'^\s*\["[^"]+"')
BASELINE_VER    = re.compile(r"(const\s+BTF_BASELINE_VER\s*=\s*')([^']+)(';)")


def parse_js_row(line):
    """Split a JS array row like `["Foo","AFC","West",6,11,...]` into its parts."""
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


def reset_row(line, indices):
    parts = parse_js_row(line)
    if not parts:
        return line, False
    before = parts[:]
    for idx in indices:
        if idx < len(parts):
            # Preserve int vs float formatting: 28.4 → 0.0, 11 → 0
            v = parts[idx].strip()
            parts[idx] = "0.0" if "." in v else "0"
    if parts == before:
        return line, False
    stripped = line.rstrip("\n")
    indent = len(stripped) - len(stripped.lstrip())
    trailing = "," if stripped.rstrip().endswith(",") else ""
    return " " * indent + "[" + ",".join(parts) + "]" + trailing + "\n", True


def reset_nfl_block(html_lines, indices):
    in_block = False
    changed = 0
    for i, line in enumerate(html_lines):
        if not in_block:
            if NFL_BLOCK_START.match(line):
                in_block = True
            continue
        if NFL_BLOCK_END.match(line):
            in_block = False
            break
        if not TEAM_ROW.match(line):
            continue
        new_line, did = reset_row(line, indices)
        if did:
            html_lines[i] = new_line
            changed += 1
    return changed


def bump_baseline(html_lines, new_tag):
    for i, line in enumerate(html_lines):
        m = BASELINE_VER.search(line)
        if not m:
            continue
        old_tag = m.group(2)
        if old_tag == new_tag:
            return None  # no-op
        html_lines[i] = BASELINE_VER.sub(rf"\g<1>{new_tag}\g<3>", line)
        return old_tag
    return False  # marker not found


def date_guard(force):
    today = date.today()
    earliest = date(today.year, 8, 15)
    if today < earliest and not force:
        print(f"✗ Refusing to run on {today.isoformat()}.")
        print(f"  NFL reset should fire after {earliest.isoformat()} (~3 weeks")
        print(f"  before Week 1). Pass --force to override.")
        sys.exit(2)


def main():
    args = sys.argv[1:]
    dry_run        = "--dry-run" in args
    reset_scoring  = "--reset-scoring" in args
    force          = "--force" in args

    version_tag = None
    for i, a in enumerate(args):
        if a == "--version-tag" and i + 1 < len(args):
            version_tag = args[i + 1]
    if version_tag is None:
        version_tag = f"{date.today().isoformat()}-nfl-reset"

    date_guard(force)

    if not HUB_FILE.exists():
        print(f"✗ Hub file not found: {HUB_FILE}")
        sys.exit(1)

    indices = list(DEFAULT_RESET_INDICES)
    if reset_scoring:
        indices += SCORING_RESET_INDICES

    if dry_run:
        print("[reset_nfl_season] DRY RUN — no files will be written\n")
    print(f"Hub file:        {HUB_FILE}")
    print(f"Reset indices:   {sorted(indices)}")
    print(f"New baseline:    {version_tag}")
    print(f"Reset scoring:   {'YES (PPG/PAPG → 0)' if reset_scoring else 'no (PPG/PAPG kept as priors)'}")
    print()

    html_lines = HUB_FILE.read_text(encoding="utf-8").splitlines(keepends=True)

    rows_changed = reset_nfl_block(html_lines, indices)
    baseline_result = bump_baseline(html_lines, version_tag)

    if rows_changed == 0:
        print("· No NFL rows needed changes (already zeroed).")
    else:
        print(f"✓ {rows_changed} NFL row(s) reset.")

    if baseline_result is False:
        print("⚠  BTF_BASELINE_VER line not found — skipped baseline bump.")
    elif baseline_result is None:
        print(f"· BTF_BASELINE_VER already '{version_tag}' — no bump needed.")
    else:
        print(f"✓ BTF_BASELINE_VER bumped: '{baseline_result}' → '{version_tag}'")

    if dry_run:
        print("\n(dry run — no file written)")
        return

    if rows_changed or baseline_result not in (False, None):
        HUB_FILE.write_text("".join(html_lines), encoding="utf-8")
        print(f"\n💾 {HUB_FILE.name} written.")
    else:
        print("\n(no changes — file untouched)")


if __name__ == "__main__":
    main()
