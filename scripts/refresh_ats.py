#!/usr/bin/env python3
"""
Bet The Farm Hub — One-shot ATS/O-U manual refresh
Reads a JSON file containing current ATS/O-U records for all teams and
patches those values into the hub HTML (index.html / Bet The Farm Hub.html).

Usage:
  python scripts/refresh_ats.py                          # uses data/ats_refresh.json
  python scripts/refresh_ats.py data/ats_refresh.json   # explicit path
  python scripts/refresh_ats.py --dry-run               # show what would change, no writes

HOW TO GET THE DATA:
  1. Open bettingpros.com/nba/against-the-spread-standings in your browser
  2. Copy ATS win/loss/home/away/OU numbers for each team
  3. Fill them into data/ats_refresh.json (copy from data/ats_refresh_TEMPLATE.json)
  4. Run this script from the repo root
  5. Double-click push-update.command to commit and push

JSON FORMAT  (see data/ats_refresh_TEMPLATE.json for full example):
  {
    "source":  "bettingpros.com — 2026-03-28",
    "as_of":   "2026-03-28",
    "sports": {
      "nba": [
        {"team":"Boston Celtics","aw":32,"al":20,"haw":18,"hal":8,"aaw":14,"aal":12,"ov":24,"un":28}
      ],
      "nhl": [
        {"team":"Boston Bruins","plw":35,"pll":28}
      ],
      "mlb": [
        {"team":"Los Angeles Dodgers","aw":4,"al":3,"haw":2,"hal":1,"aaw":2,"aal":2}
      ],
      "nfl": [
        {"team":"Kansas City Chiefs","aw":11,"al":7,"haw":7,"hal":3,"aaw":4,"aal":4,"ov":9,"un":9}
      ]
    }
  }

ATS index map per sport (0-based, from const SIDX in hub):
  NBA:  aw=5  al=6  haw=8  hal=9  aaw=10 aal=11  ov=12 un=13
  NHL:  plw=6 pll=7  (haw/hal/aaw/aal alias plw/pll)  ov=13 un=14
  MLB:  aw=5  al=6  haw=8  hal=9  aaw=10 aal=11  ov=12 un=13
  NFL:  aw=5  al=6  haw=8  hal=9  aaw=10 aal=11  ov=12 un=13
"""

import sys
import json
import re

# Which HTML files to patch (run from repo root)
HUB_FILES = ["index.html", "Bet The Farm Hub.html"]

ATS_INDICES = {
    # Sourced from const SIDX in Bet The Farm Hub.html (0-based)
    "nba": {"aw":5,  "al":6,  "haw":8,  "hal":9,  "aaw":10, "aal":11, "ov":12, "un":13},
    "nhl": {"aw":6,  "al":7,  "haw":6,  "hal":7,  "aaw":6,  "aal":7,  "ov":13, "un":14},
    "mlb": {"aw":5,  "al":6,  "haw":8,  "hal":9,  "aaw":10, "aal":11, "ov":12, "un":13},
    "nfl": {"aw":5,  "al":6,  "haw":8,  "hal":9,  "aaw":10, "aal":11, "ov":12, "un":13},
}

# NHL uses "plw" / "pll" as field names in JSON instead of aw/al
NHL_FIELD_MAP = {"plw": "aw", "pll": "al"}


def _parse_js_row(line):
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


def patch_rows(html_lines, team_name, idx_vals, dry_run=False):
    for i, line in enumerate(html_lines):
        s = line.strip()
        if not s.startswith(f'["{team_name}"'):
            continue
        parts = _parse_js_row(s)
        if not parts:
            continue
        before = parts[:]
        for idx, val in idx_vals.items():
            if idx is not None and idx < len(parts):
                parts[idx] = str(int(val))
        if parts == before:
            return 0
        if not dry_run:
            indent   = len(line) - len(line.lstrip())
            trailing = "," if s.endswith(",") else ""
            html_lines[i] = " " * indent + "[" + ",".join(parts) + "]" + trailing + "\n"
        return 1
    return 0


def refresh_file(hub_file, data, dry_run=False):
    try:
        with open(hub_file, encoding="utf-8") as f:
            html_lines = f.read().splitlines(keepends=True)
    except FileNotFoundError:
        print(f"  · {hub_file} not found, skipping")
        return

    total = 0
    for sport, entries in data.get("sports", {}).items():
        if sport not in ATS_INDICES:
            print(f"  ⚠  Unknown sport '{sport}' — skipping")
            continue
        idx = ATS_INDICES[sport]
        sport_total = 0
        for entry in entries:
            team = entry.get("team", "").strip()
            if not team:
                continue
            updates = {}
            for field, arr_idx in idx.items():
                if arr_idx is None:
                    continue
                # NHL accepts both "plw"/"pll" and "aw"/"al" as field names
                json_key = field
                if sport == "nhl":
                    # plw → aw, pll → al; haw/hal/aaw/aal all alias to plw/pll
                    if field in ("haw", "hal", "aaw", "aal"):
                        continue   # skip — plw/pll cover these
                    rev = {v: k for k, v in NHL_FIELD_MAP.items()}
                    json_key = rev.get(field, field)
                if json_key in entry:
                    updates[arr_idx] = int(entry[json_key])
            if not updates:
                continue
            n = patch_rows(html_lines, team, updates, dry_run=dry_run)
            if n:
                sport_total += 1
                if dry_run:
                    print(f"  [DRY] {sport.upper()}: {team} would be updated → {updates}")
        print(f"  ✓ {sport.upper()}: {sport_total}/{len(entries)} team(s) patched in {hub_file}")
        total += sport_total

    if not dry_run and total:
        with open(hub_file, "w", encoding="utf-8") as f:
            f.writelines(html_lines)
        print(f"  💾 {hub_file} written ({total} row(s) updated)")


def main():
    args = sys.argv[1:]
    dry_run   = "--dry-run" in args
    json_args = [a for a in args if not a.startswith("--")]
    json_path = json_args[0] if json_args else "data/ats_refresh.json"

    if dry_run:
        print("[refresh_ats] DRY RUN — no files will be written\n")

    try:
        with open(json_path) as f:
            data = json.load(f)
    except FileNotFoundError:
        print(f"✗ Input file not found: {json_path}")
        print(f"  Copy data/ats_refresh_TEMPLATE.json → {json_path} and fill in the values.")
        sys.exit(1)

    source = data.get("source", "unknown source")
    as_of  = data.get("as_of",  "unknown date")
    print(f"[refresh_ats] Source: {source}  |  As of: {as_of}\n")

    for hub_file in HUB_FILES:
        print(f"── {hub_file} ─────────────────────────────────────────────")
        refresh_file(hub_file, data, dry_run=dry_run)
        print()

    if not dry_run:
        print("✅ Done. Run push-update.command to commit and push to GitHub.")


if __name__ == "__main__":
    main()
