# 2026-04-18 Elite Picks Reference (Hub Screenshot)

**Captured from live hub view on 04-18 at ~16:33 UTC (~12:33 PM ET).**
Use to verify tomorrow's (04-19) grading run captured and graded these picks.

## Elite tier (score ≥ 68)

| # | Score | Sport | Pick | Matchup | Game Time (ET) |
|---|------|-------|------|---------|----------------|
| 1 | 69 | MLB | Atlanta Braves +1.5 | Braves @ Phillies | 7:16 PM |
| 2 | 69 | NBA | Over 208.5 | Rockets @ Lakers | 8:40 PM |
| 3 | 69 | MLB | Under 10.5 | Dodgers @ Rockies | 8:11 PM |
| 4 | 69 | MLB | Under 9.5 | Padres @ Angels | 9:39 PM |
| 5 | 68 | MLB | Under 8 | Royals @ Yankees | 1:36 PM |

## Strong tier (≥ 62) visible on this screen

| Score | Sport | Pick | Matchup |
|------|-------|------|---------|
| 66 | MLB | Under 7 | Rangers @ Mariners |
| 66 | MLB | Los Angeles Dodgers ML (-308) | Dodgers @ Rockies |
| 65 | MLB | Atlanta Braves ML (+109) | Braves @ Phillies |

## What was actually logged (16:36 UTC / 12:36 PM ET — 3 min after screenshot)

Manual log-picks run: https://github.com/Chendo1933/bet-the-farm-hub/actions/runs/24609084143
42 picks logged · 2 elite · 12 strong · 19 good · 9 lean

**Elite tier — what's on deck for grading tomorrow:**

| Score | Sport | Pick | Matchup |
|------|-------|------|---------|
| 69 | NBA | Over 208.5 | Rockets @ Lakers |
| 69 | MLB | Los Angeles Dodgers ML (-308) | Dodgers @ Rockies |

**Dropped below elite in the 3-min window (line movement):**
- MLB Under 10.5 (LAD/COL): 69 → 62 Strong
- MLB Under 9.5 (SD/LAA): 69 → 62 Strong
- MLB Under 8 (KC/NYY): 68 → 61 Good
- MLB Braves +1.5 → flipped to Braves -1.5 at 64 Strong

All three Unders fell ~7 points in that window — consistent with totals
moving up (early lineups / weather). This is exactly why a single 11:50 AM
ET log-time misses picks that existed minutes earlier.

## Verification checklist for 04-19 (after 08:30 UTC grading run)

- [ ] `data/pick_history.json` has 2 entries with `date: 2026-04-18` and `tier: elite`
  — NBA Over 208.5 (Rockets/Lakers) and MLB Dodgers ML (-308)
- [ ] Both have an `outcome` field set (`win` / `loss` / `push`)
- [ ] `data/performance.json` → `tiers.elite` W/L/P incremented by 2
