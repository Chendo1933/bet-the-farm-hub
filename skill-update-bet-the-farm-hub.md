---
name: bet-the-farm-hub
description: >
  Master reference for the Bet The Farm Hub — Cole's single-file sports betting intelligence
  dashboard (Bet The Farm Hub.html). Use this skill at the start of EVERY session that touches
  the hub: adding features, fixing bugs, changing scoring logic, updating team data, modifying
  the UI, or debugging pick generation. Contains the exact file location, key function map,
  factor weights, confidence system, simulation architecture, and design decisions so you can
  jump straight into code without a lengthy warmup. Trigger on: "hub", "Bet The Farm", "hot bets",
  "best bets", "parlay", "matchup analyzer", "hbScore", "hbSimulate", "confidence", "pick",
  "spread scorer", "ATS", "factor weights", "Poisson", "BTF_PICKS", "performance panel",
  "pitcher", "MLB pitcher", "starting pitcher", "ERA", "pitching matchup",
  or any request to modify the hub HTML file.
---

# Bet The Farm Hub — Master Reference

## File Location

```
/sessions/ecstatic-loving-johnson/mnt/Bet The Farm/Bet The Farm Hub.html
```

Single self-contained HTML file (~8,500+ lines). All JS, CSS, and data live inline.
No build step, no dependencies — open directly in browser.

---

## Architecture Overview

```
Inline DB Arrays (NFL/CFB/NBA/CBB/NHL/MLB)
        ↓
TEAM_IDX (O(1) lookup index built at page load)
        ↓
refreshAllOdds() → TODAY_GAMES[] (live API data)
  + fetchMLBProbablePitchers() → MLB_PITCHERS{} (free MLB Stats API, no key needed)
        ↓
hbGeneratePicks() → scored pick objects
hbGenerateParlays() → multi-leg parlay objects
        ↓
renderHotBets() / renderBestBets() → UI cards
        ↓
window.BTF_PICKS export (machine-readable, for automation)
```

The hub has four main tabs: **Analyzer** (matchup deep-dive), **Hot Bets** (tonight's top picks),
**Parlay Builder** (multi-leg combos), and **Angles** (situational edges reference).

---

## Key Functions — Quick Map

| Function | Purpose | Location |
|----------|---------|----------|
| `hbScoreMatchup(g, ht, at, ix, mlHint, simHint)` | Spread confidence scorer | ~line 6579 |
| `hbScoreML(g, ht, at, ix)` | Moneyline confidence scorer | ~line 6508 |
| `hbScoreOU(g, ht, at, ix)` | Over/Under confidence scorer | ~line 6389 |
| `hbSimulateGame(g, ht, at, ix)` | Projected scores + win prob (display only) | ~line 6274 |
| `hbGetTier(score100)` | Map 0–100 probability → confidence tier | ~line 7101 |
| `hbScoreRing(score100, tier)` | Render SVG confidence ring | ~line 6156 |
| `hbGeneratePicks()` | Generate all tonight's picks | ~line 6948 |
| `hbGenerateParlays()` | Build parlay combinations | after picks |
| `fuzzyTeam(sport, name)` | Resolve API name → DB row | ~line 2300+ |
| `normalizeTeamName(name, sport)` | Pre-process API name | ~line 2200+ |
| `refreshAllOdds()` | Fetch live odds from API | ~line 4000+ |
| `hbAnalyzeMatchup(g)` | Deep analysis for Analyzer tab | ~line 3600+ |
| `simSamplePoisson(lambda)` | Poisson integer sampler | after simMonteCarlo |
| `simMonteCarloInt(lambda, n)` | Poisson percentile simulation | after simSamplePoisson |
| `simScoreChance(lambda)` | "29% chance" probability string | after simMonteCarloInt |
| `fetchMLBProbablePitchers()` | Fetch today's MLB starters from MLB Stats API (no key) | before refreshAllOdds |
| `getMLBPitcher(name, rawName)` | Lookup helper: DB name + raw name + fuzzy match | after fetchMLBProbablePitchers |
| `getScorePerGame(t, ix, sport)` | Per-game PPG/PAPG with early-season smoothing | ~line 4820 |
| `hbAltLineSuggestions(p)` | Projection-driven alt line cards (spread + O/U) | ~line 7121 |

All scoring functions share the signature `(g, ht, at, ix)`:
- `g` — game object from TODAY_GAMES
- `ht` — home team row array from DB
- `at` — away team row array from DB
- `ix` — `SIDX[g.sport]` index map

---

## ⚠️ UNIFIED SCORING ARCHITECTURE (changed 2026-04-05)

**The confidence ring number IS the cover/win probability. No extremizer.**

Previously the hub ran two independent systems (factor scorer + simulation) that could
contradict each other on the same card. Now there is one output per pick:

- Score of 63 = model estimates **63% probability** of covering. Not a compressed index.
- The "Score Model" card section shows projected scores and market-implied win% only.
- `hbSimulateGame` is now **display-only** — it computes projected scores and O/U for
  context but does NOT feed into the confidence score.
- The simHint ATS anchor was removed from `hbScoreMatchup` — ATS data is already the
  primary input via F1/F5; blending with sim.coverProb (also ATS-based) would double-weight it.
- This makes Brier score calibration meaningful: over time, 65% picks should win ~65%.

---

## Confidence Scoring System — `hbScoreMatchup`

### Factor Weights (Current — as of 2025-26 season)

| Factor | Weight | Notes |
|--------|--------|-------|
| F1: ATS split (home/away or overall) | 0.32 | Primary signal; Bayesian-shrunk |
| F2: Implied probability edge | 0.20 | Uses atsScore (shrunk), not raw atsPct |
| F3: Rest edge | 0.12 | Skipped for NBA when B2B (F8b) fires |
| F4: Projected total vs line | **0** | Removed — always scored 0.5, zero signal |
| F5: Season ATS overall | 0.15 | Skipped for CBB (double-counts F1) |
| F6: Efficiency differential | 0.20 | NBA: Net RTG; CBB: AdjOE-AdjDE; MLB: Run diff/g |
| F7: Injury signal | 0.18 | Penalizes picked team's outs, rewards opp's |
| F8a: Home dog angle | 0.06 | NFL/CFB/NBA/MLB; disabled for CBB |
| F8b: B2B road fav fade | 0.09 | NBA only; exclusive with F3 |
| F9: ML underdog floor | 0.12 | Only for underdog picks with ML data |
| F10: Starting pitcher ERA (MLB only) | 0.20 | ERA diff between today's scheduled starters |

**Max total weight** ≈ 1.56 (not all factors fire every game; F10 only fires for MLB with pitcher data)
**ATS signals combined** (F1+F2+F5): 0.67 = ~43% of max
**MLB-specific**: F6 (run diff) + F10 (pitcher ERA) = 0.40 combined when both fire

### Scoring Formula

```javascript
// Each factor: score += signalValue * factorWeight; weight += factorWeight;
const rawScore = weight > 0 ? (score / weight) : 0.5;
// NO EXTREMIZER — rawScore IS the probability (e.g. 0.63 = 63% cover probability)
const finalScore = Math.max(0.01, Math.min(0.99, rawScore));
```

**Minimum thresholds:** spread ≥ 0.54, O/U ≥ 0.55, ML ≥ 0.54

### Score Anchoring (ML picks only, post-scoring in `hbGeneratePicks`)

ML score is a direct 60/40 blend with market-implied win probability.
No de-extremize/re-extremize step — scores are already raw probabilities.

```javascript
// Direct blend (no de-extremize needed — score is already a probability)
const mlWinProb = mlResult.atsPick === 'home' ? (sim.homeWinProb ?? 0.5) : (sim.awayWinProb ?? 0.5);
const mlAnchored = mlResult.score * 0.60 + mlWinProb * 0.40;
mlResult.score = Math.max(0.01, Math.min(0.99, mlAnchored));
mlResult.score100 = Math.round(mlResult.score * 100);
```

### Bayesian Shrinkage

```javascript
const conf = Math.min(1, n / 20);  // confidence: 0 at n=0, 1.0 at n≥20
const shrunk = rawPct * conf + 0.5 * (1 - conf);
```

### B2B Exclusivity (F3 vs F8b)

When an NBA game triggers the B2B road fav angle (F8b), Factor 3 is skipped entirely.
Both factors model the same rest situation — F8b is more specific so it takes priority.

---

## Confidence Tiers — `hbGetTier`

Calibrated to real cover/win probabilities (score = probability %):

| Tier | Score | Color | Badge | Notes |
|------|-------|-------|-------|-------|
| Elite | ≥68 | Red `#ef4444` | 🔥 Elite | Exceptional — multiple strong signals aligned |
| Strong | ≥62 | Gold `var(--gold)` | ⚡ Strong | Clear model edge |
| Good | ≥57 | Green `#22c55e` | ✅ Good | Meaningful edge, worth logging |
| Lean | <57 | Gray `var(--muted)` | 📊 Lean | Below threshold, context only |

Breakeven for -110 juice = 52.4%. Any tier ≥ Good has a real betting edge.

---

## Score Model Card (formerly "Game Simulation")

The card section is now labeled **"📊 Score Model"** and shows:
- 🏆 `TeamName wins X%` **(mkt-implied)** — market-implied win probability from ML odds
- 🎯 `Proj: Team A 114 – Team B 109 (covers by N)` — score projection + cushion vs spread
- 📈/📉 `Over/Under X · proj Y (Z%)` — **only on O/U pick cards**, not on spread/ML cards

**Removed from card:**
- ~~`covers X%`~~ — redundant; the ring already shows the cover probability
- ~~`⚠️ Models split` conflict warning~~ — ring score is now the single authoritative signal
- ~~O/U % on spread cards~~ — was noisy secondary signal; now only shows on O/U picks

---

## `getScorePerGame(t, ix, sport)` — Per-Game Scoring with Early-Season Smoothing

Returns `{ppg, papg}` or `null`. Handles each sport's storage format and applies
Bayesian smoothing toward league average during small-sample early seasons.

### Sport Branches

**NHL** — always divides season totals by gp:
```javascript
const gp = (W + L + OTL) || 0;
return gp > 0 ? { ppg: GF/gp, papg: GA/gp } : null;
```

**MLB** — season RS/RA totals with prior-season detection + early-season smoothing:
```javascript
const gpActual = (W + L) || 0;
const gp = gpActual || 162;
const rs = t[ix.rs] || 0, ra = t[ix.ra] || 0;
// Prior-season detection: if RS > gpActual * 20, it's a full-season total → use 162
const priorGP = (gpActual > 0 && rs > gpActual * 20) ? 162 : gp;
const rawPPG = rs / priorGP, rawPAPG = ra / priorGP;
if (gp >= 30) return { ppg: rawPPG, papg: rawPAPG };
const MLB_LG_AVG = 4.5, conf = gp / 30;
return { ppg: rawPPG*conf + MLB_LG_AVG*(1-conf), papg: rawPAPG*conf + MLB_LG_AVG*(1-conf) };
```
*Why*: prevents the RS=874 / gp=3 = 291 RPG bug at season start. If RS >> gpActual*20,
it's a prior-season total → divide by 162 not gpActual.

**NBA/CBB** — per-game stats stored directly, with early-season smoothing:
```javascript
const ppg = t[ix.ppg], papg = t[ix.papg];
if (ppg === 0 && papg === 0) return null;
const SMOOTH = { nba: { min: 15, avg: 115 }, cbb: { min: 8, avg: 73 } };
const sm = SMOOTH[sport];
if (sm) {
  const gp = t[ix.w] + t[ix.l];
  if (gp > 0 && gp < sm.min) {
    const conf = gp / sm.min;
    return { ppg: ppg*conf + sm.avg*(1-conf), papg: papg*conf + sm.avg*(1-conf) };
  }
}
return { ppg, papg };
```

**NFL/CFB** — no smoothing (values are already per-game, no early-season issue due to short seasons).

### League Averages Used
| Sport | Avg PPG | Full Confidence At |
|-------|---------|-------------------|
| MLB   | 4.5 RPG | 30 games |
| NBA   | 115 PPG | 15 games |
| CBB   | 73 PPG  | 8 games  |

### MLB mlbThinData Guard
Suppresses O/U projection signal until 5+ games played (lowered from 15 after the
prior-season RS/RA detection fix made projections reliable from game 1).

---

## `hbAltLineSuggestions(p)` — Projection-Driven Alt Lines

Renders the Alt Lines section on Hot Bets cards. Handles both **spread** and **O/U** picks.

### Spread Picks — Projection Cushion Logic

`projCushion = pickedMargin + pickedSp` (positive = projection covers with room)

| Cushion | Action |
|---------|--------|
| ≥ 4 pts | "📈 Sell a point" — projection has room, take better odds on harder line |
| 0–2 pts | "🛡️ Buy protection" — thin margin, paying for safety is worth it |
| < 0 | "⚠️ Proj short of spread — consider passing" |
| 2–4 pts | No alt line shown — pick is comfortable, no action needed |
| No projection | Falls back to simple ±1 display |

### O/U Picks — Value Alt Lines

When projection differs from posted line by ≥ 1 unit, suggests a **harder** alt line
that pays **better odds** but where the projection still has cushion.

---

## MLB Starting Pitcher System

### Global: `MLB_PITCHERS`
```javascript
// Keyed by team name (e.g. "New York Yankees")
// { name, era, whip, wins, losses, ip }
let MLB_PITCHERS = {};
```
Populated automatically by `fetchMLBProbablePitchers()` when `refreshAllOdds()` runs
during MLB season (months 3–10). No API key required — uses the free MLB Stats API.

### API Endpoint
```
https://statsapi.mlb.com/api/v1/schedule?sportId=1&date=YYYY-MM-DD
  &hydrate=probablePitcher(note,stats[type=[season]])
```

### Lookup Helper
```javascript
// Always call as: getMLBPitcher(g.home, g.rawHome) or (g.away, g.rawAway)
// Tries: direct DB name → raw odds API name → normalized fuzzy match
function getMLBPitcher(name, rawName) { ... }
```

### Factor 10 — Pitcher ERA Differential (spread scoring)
Fires in `hbScoreMatchup` for MLB when both starters have ERA data. Weight: **0.20**.
```
eraDiff = oppERA - pickedERA  (positive = picked team's pitcher is better)
> 1.5 → 0.72,  > 0.75 → 0.64,  > 0 → 0.56,  > -0.75 → 0.44,  > -1.5 → 0.36,  else → 0.28
```

### Pitcher O/U Signal (`hbScoreOU` AND `hbSimulateGame`, MLB only)
```
avgERA ≤ 3.25 → 0.28 (🔒 Under — two aces)
avgERA ≤ 3.75 → 0.38 (slight Under)
avgERA ≥ 4.75 → 0.72 (📈 Over — weak starters)
avgERA ≥ 4.25 → 0.62 (slight Over)
Weight in blend: 0.20
```

### Card Display
MLB Hot Bets cards show: `⚾ Away Starter (ERA) @ Home Starter (ERA)`

---

## CBB Neutral Site Logic

> ⚠️ **Critical naming trap** — `ix.aw` means **overall ATS wins** (NOT "away wins").
> `SIDX.cbb = { aw:4, al:5 (overall ATS), haw:7, hal:8 (home ATS), aaw:9, aal:10 (away ATS) }`

CBB uses overall ATS for both teams (neutral sites), skips F5 (double-count), disables F8a
(no home crowd), and defaults cover probability to 50/50.

---

## Game Simulation — `hbSimulateGame` (display only)

**Role changed**: `hbSimulateGame` now provides data for the Score Model card display only.
It does NOT affect the confidence score (that comes from `hbScoreMatchup`/`hbScoreOU`/`hbScoreML`).

**Win Probability**: Blends historical win rate (55%) with no-vig market implied (45%). Normalized.
**Cover Probability**: Still computed (for internal use) but no longer shown on card — ring shows it.
**O/U Probability**: Blends historical O/U record + projection + pitcher ERA (MLB).
**CBB**: KenPom logit model `P = 1 / (1 + exp(-0.15 * netDiff))` supplements win estimate.

---

## ML→Spread Bridge in `hbGeneratePicks`

When a moneyline pick also has a spread available, the bridge can carry the ML pick's direction
into a spread-bet recommendation ("Underdog wins outright — take the spread too").

**Critical requirement — genuine ML underdog only**:
```javascript
const pickedML = mlSide === 'home' ? g.mlHome : g.mlAway;
const mlIsMLUnderdog = pickedML == null || pickedML > 0;
const mlIsUnderdog = ((mlSide==='home' && gSp>0) || (mlSide==='away' && gSp<0)) && mlIsMLUnderdog;
```

---

## ML Alignment in F1

If ML is confident on an underdog (≥60 score) opposite the spread pick and ATS records
are close (within 0.07), the spread pick defers to ML direction.

---

## Player Prop Simulation — Poisson Integer System

- **Poisson (`simMonteCarloInt`)**: Goals, SOG, Strikeouts, Earned Runs, HR — whole numbers only
- **Gaussian (`simMonteCarlo`)**: NBA points, rebounds, assists — fractional averages are natural
- **`simFmtInt(val)`**: Returns "0" for zero (not "—" — use this, not `simFmtStat`, for count stats)

---

## MLB Season Baseline — `||162` Fallback

```javascript
const gp = ((t[ix.w] || 0) + (t[ix.l] || 0)) || 162;
```
With W=L=0, uses prior season RS/RA as per-game baselines immediately on Opening Day.
**Season reset**: Zero W/L/ATS/OU; preserve RS, RA, ERA, AVG as projection priors.

---

## `window.BTF_PICKS` — Machine-Readable Pick Export

```javascript
window.BTF_PICKS = picks.map(p => { /* {sport,betType,home,away,atsPick,spread,pickLabel,score100,tier,date,time} */ });
window.BTF_PICKS_READY = true;  // Playwright waits for this flag
```

---

## Performance Tracking Panel

**Data source**: `https://raw.githubusercontent.com/Chendo1933/bet-the-farm-hub/main/data/performance.json`
Color codes: ≥55% green · 45–55% gold · <45% red. Hidden until file exists.
Includes `by_conf` breakdown: W/L/P per confidence band (95-99, 90-94, 85-89, 80-84, 75-79, 70-74).

---

## Color Theme

```css
--gold:#c9a84c; --green:#22c55e; --muted:#7a95b4;
--bg:#0d1117; --surface:#161b22; --border:#21262d; --text:#e6edf3; --text2:#8b949e;
```

Good tier badge and ring both use `#22c55e` (unified — previously badge was `#4ade80`).

---

## Common Bugs to Watch For

- **Both teams >50% win prob**: Not normalized — divide by `rawH + rawA`.
- **Cover probs don't sum to 1**: Set `away = 1 - home`, never compute independently.
- **Score capped at low range**: No extremizer now — factor signals are bounded 0.28–0.72.
  A strong pick will score ~0.63-0.68 (63-68%), which is correct for spread betting edge.
  Do NOT re-add the 1.6× extremizer — it was removed intentionally.
- **`ix.aw` misread as "away wins"**: It's overall ATS wins (col 4). Away ATS = `ix.aaw` (col 9).
- **MLB projections wildly wrong in first 1–5 games**: `getScorePerGame` uses prior-season
  detection (`rs > gpActual * 20`) + Bayesian smoothing. Do not remove `||162` fallback or
  the `if(gp>=30)` early-return. mlbThinData guard at 5 games (lowered from 15).
- **ML→Spread bridge mislabeling ML favorites as underdogs**: Guard `pickedML == null || pickedML > 0`
  ensures bridge only fires for genuine ML underdogs (positive odds = paying out on win).
- **MLB_PITCHERS empty during season**: Check console for `[BTF] MLB pitcher fetch failed`.
  Off-season (Nov–Feb) is normal — F10 simply doesn't fire.
- **Poisson showing fractions**: Wrong function — count stats need `simMonteCarloInt`, not `simMonteCarlo`.
- **⚠️ CRITICAL — Duplicate `function showTab()` breaks ALL tabs**: Never re-declare `showTab`.
  Add tab init inside the ONE real `showTab` using `if(id==='viewingroom') vrOnTabOpen();`.
- **Parlay hit probability**: `score100` is now a raw probability (no extremizer), so use it
  directly: `prob = score100 / 100`. No de-extremize step needed.
- **O/U % showing on spread cards**: The O/U line in the Score Model box is gated to
  `p.betType==='ou'` — it must NOT show on spread or ML cards. Check this gate if O/U
  reappears on spread cards after edits.
- **Recurring rebase conflicts on push**: The scheduled task commits BettingPros data locally
  while GitHub Actions commits stats/results remotely. push-update.command uses `git pull --rebase`
  which causes conflicts when both touch the same arrays. Fix: resolve by taking remote's
  data (theirs) via Python regex, then `git add && git rebase --continue`.

---

## Related Skills

- **`sports-data-schema`**: Exact array indices for all six sport arrays + SIDX reference
- **`team-name-registry`**: TEAM_NAME_CORRECTIONS, normalizeTeamName pipeline, debugging mismatches
- **`market-mechanics-betting`**: Edge calculation, Kelly sizing
- **`github-automation`**: CI/CD workflows, pick logging, results logging, grading pipeline, performance.json
