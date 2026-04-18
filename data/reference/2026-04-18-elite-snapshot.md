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

## Verification checklist for 04-19 (after 08:30 UTC grading run)

- [ ] `data/picks/2026-04-18.json` exists and contains all 5 elite picks above
- [ ] `data/pick_history.json` has 5 graded entries with `date: 2026-04-18` and `tier: elite`
- [ ] `data/performance.json` → `tiers.elite` incremented by 5 with appropriate W/L/P split
- [ ] Each elite pick in pick_history has an `outcome` of `win` / `loss` / `push`
