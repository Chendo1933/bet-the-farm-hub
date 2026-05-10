# Kalshi Auto-Trading Setup

Phase 1 of the Kalshi integration is **read-only**. No orders are placed.
This phase confirms the API works, your auth is set up correctly, and our
picks can be mapped to live Kalshi markets.

## One-time setup

### 1. Create a Kalshi account (demo first)

- Live: https://kalshi.com — full KYC, real money
- Demo: https://demo.kalshi.co — instant, $1k of fake money

**Use demo for the first 1-2 weeks.** Everything below works identically; switching to live is a one-line config change later.

### 2. Generate an RSA key pair

Kalshi v2 API auth uses RSA-PSS-SHA256 signed requests. You need a private key (yours, never share) and an API Key ID (UUID Kalshi generates).

```bash
mkdir -p ~/.config/kalshi
openssl genrsa -out ~/.config/kalshi/demo.pem 2048
chmod 600 ~/.config/kalshi/demo.pem
openssl rsa -in ~/.config/kalshi/demo.pem -pubout -out ~/.config/kalshi/demo.pub
```

That gives you `demo.pem` (private — keep secret) and `demo.pub` (public — upload to Kalshi).

### 3. Register the public key with Kalshi

1. Log into demo.kalshi.co
2. Settings → API Keys → "Create new API key"
3. Paste the contents of `demo.pub`
4. Permissions: start with **Read** only (we'll add Trade in Phase 3)
5. Copy the generated **Key ID** (a UUID)

### 4. Set environment variables

For local testing:
```bash
export KALSHI_ENVIRONMENT=demo
export KALSHI_API_KEY_ID=<your-uuid-from-step-3>
export KALSHI_PRIVATE_KEY_PATH=~/.config/kalshi/demo.pem
```

Stick those in your shell profile (`.zshrc` / `.bashrc`) for persistence.

For GitHub Actions (when we get to Phase 4), they'll go in repo Secrets:
- `KALSHI_API_KEY_ID`
- `KALSHI_PRIVATE_KEY` (paste the entire PEM contents inline — multi-line secrets are supported)
- `KALSHI_ENVIRONMENT` (set as a repo variable, not secret)

### 5. Install Python dependency

```bash
pip install cryptography requests
```

(`requests` is already a dep elsewhere in the project; `cryptography` is new for the RSA signing.)

### 6. Run the setup checker

```bash
python scripts/kalshi/check_setup.py
```

What it verifies:
1. ✓ Env vars are set
2. ✓ Private key parses as RSA (≥2048-bit)
3. ✓ Signed request authenticates (returns your demo balance)
4. ✓ Sports markets are visible (MLB / NBA / NHL / NFL events)

If all four pass, Phase 1 is working.

To also test pick→market mapping against today's slate:

```bash
python scripts/kalshi/check_setup.py --map-today
```

This reads `data/picks/{ET-today}.json` and tries to find a Kalshi market for each ML pick. Output looks like:

```
[matched    ] MLB Cubs ML (-136)         → KXMLB-26MAY09CUBPHI-CUB (yes side: YES, ask: 65¢)
[no_event   ] NHL Avalanche ML (-130)    — No Kalshi event matched ANA @ COL
[ambiguous  ] NBA Knicks ML (+102)       — 2 strong event matches — manual review
```

Statuses:
- `matched` — found exactly one market, ready for Phase 2 dry-run
- `no_event` — Kalshi has no contract for this game (lots of NHL/NBA games aren't covered)
- `no_market` — event exists but no market mentions the picked team
- `ambiguous` — multiple matches; mapping logic needs hardening for that case
- `unsupported` — non-ML pick or sport without a Kalshi series

## What's NOT in Phase 1

- ❌ No orders are placed under any circumstance
- ❌ No money moves
- ❌ No GitHub Actions workflow runs anything against Kalshi yet
- ❌ Spread / total picks aren't mapped (ML only)

Those come in Phase 2 (dry-run simulation) and Phase 3 (live with kill switches).

---

## Phase 2 — Dry-run order simulation (paper trading)

Once Phase 1 is verified working, Phase 2 simulates what auto-placement
would have done WITHOUT any real money moving. After 1-2 weeks of dry-run
data we'll know whether Kalshi's effective ROI matches our sportsbook
results once slippage and the YES/NO price model are factored in.

### How it works

1. **Daily**: `dry_run.py` reads today's picks file, maps each ML pick
   to a Kalshi market, fetches the current YES ask price, computes a
   fractional-Kelly stake against config caps, and writes a "would-have-
   placed" record to `data/kalshi_dryrun/{date}.json`.

2. **Nightly** (after grading runs): `reconcile.py` walks every dry-run
   file, looks up each order's outcome in `data/pick_history.json`, and
   computes theoretical PnL. Annotated orders are written back into the
   dry-run file; aggregate stats land in `data/kalshi_dryrun_perf.json`.

### Run once to test

```bash
# Generate today's dry-run orders (real Kalshi prices, no orders placed)
python3 scripts/kalshi/dry_run.py

# Reconcile any past dry-run files against grading data
python3 scripts/kalshi/reconcile.py
```

You'll see output like:

```
Kalshi dry-run · 2026-05-09 · environment=demo
  4 pick(s) in file
  3 eligible after score≥65 + bet-type filter

── Dry-run summary ──────────────────────────────────────
  Picks: 4 total, 3 eligible, 2 would place
  Total stake: $12.45  ·  daily cap remaining: $37.55

── Would-place orders ───────────────────────────────────
  $ 7.20 on KXNHLGAME-26MAY09CARPHI-CAR  (12 contracts @ 60¢)
        pick: NHL Carolina Hurricanes ML (-192)
  $ 5.25 on KXMLBGAME-26MAY091905CHCTEX-CHC (7 contracts @ 75¢)
        pick: MLB Chicago Cubs ML (-136)
```

After a few days, run reconcile and you'll see daily PnL and a running
aggregate ROI to compare against the +16% you've been seeing on sportsbooks.

### Risk parameters (data/kalshi_config.json)

These bound what Phase 2 will simulate (and Phase 3 will actually place):

- `bankroll_dollars` — used for Kelly sizing math
- `kelly_fraction` — 0.25 = quarter-Kelly (conservative)
- `min_calibrated_score` — only consider picks at this score or above
- `supported_bet_types` — `["ml"]` for now
- `max_stake_per_pick_dollars` — hard cap on a single order
- `max_daily_exposure_dollars` — total stake cap across the day
- `skip_if_yes_ask_above_cents` — skip heavy favorites where edge × cost is bad RoR

Edit the file, re-run `dry_run.py`, see the impact immediately.

### What dry-run won't tell you yet

- **Real fills**: dry-run assumes you'd get filled at the current ask. In
  practice on a thin market, your order may never fill or fill partial.
  Phase 3 handles this with limit orders + fill tracking.
- **NO-side picks**: Phase 2 only places YES on the picked-team's market.
  In rare cases the mapper finds the team on a NO market only — those
  show as `skip_reason: no_side_unsupported`.
- **Spread/total picks**: Phase 2 still ML-only. Spread on Kalshi is
  thinly covered; total contracts exist but mapping needs different logic.

---

## Phase 2 Automation — runs daily without manual action

Two GitHub Actions workflows automate the full Phase 2 cycle so you don't
have to remember to run anything manually.

### kalshi-dry-run.yml (fires after morning picks logger)

Trigger chain:
```
11:50 AM ET — log-picks.yml fires
              ↓ commits data/picks/{date}.json
              ↓ workflow_run trigger on success
~11:51 AM ET — kalshi-dry-run.yml fires
              ↓ runs scripts/kalshi/dry_run.py against live Kalshi
              ↓ commits data/kalshi_dryrun/{date}.json
```

### kalshi-reconcile.yml (fires after nightly grader)

Trigger chain:
```
04:00 AM ET — log-results.yml writes yesterday's results
              ↓ workflow_run on success
~04:01 AM — grade-picks.yml grades, updates pick_history.json
              ↓ workflow_run on success
~04:02 AM — kalshi-reconcile.yml fires
              ↓ runs scripts/kalshi/reconcile.py
              ↓ commits data/kalshi_dryrun_perf.json
```

### Required setup — three secrets/variables in repo settings

Go to **Settings → Secrets and variables → Actions** in GitHub and add:

**Repository secrets:**

1. `KALSHI_API_KEY_ID` — your live Kalshi API key UUID
   - Just paste the UUID Kalshi gave you when you uploaded the public key

2. `KALSHI_PRIVATE_KEY` — the FULL contents of your live PEM file
   ```bash
   cat ~/.config/kalshi/live.pem
   ```
   Copy everything from `-----BEGIN PRIVATE KEY-----` through
   `-----END PRIVATE KEY-----` (inclusive, all lines). Paste as the
   secret value. Multi-line is fine — GitHub handles it.

**Optional repository variable** (Variables tab, not Secrets):

3. `KALSHI_ENVIRONMENT` = `live` (defaults to `live` if not set)

That's it. Once secrets are in, the workflows take over automatically:
- Picks are logged → dry-run runs against live prices → snapshot committed
- Grading runs → reconcile computes PnL → perf file committed
- Either fails → Discord/Slack notification fires (if WEBHOOK_URL set)

You can manually trigger either workflow from the Actions tab using the
"Run workflow" button (workflow_dispatch). Useful for backfilling.

### Monitoring without checking the repo

Once you have a `WEBHOOK_URL` secret configured (any Discord/Slack
webhook), failures auto-notify your phone. Successes are silent — just
check `data/kalshi_dryrun_perf.json` weekly to see how Kalshi PnL is
tracking against your sportsbook ROI.

---

## Phase 3 — Live order placement (real money)

**Don't enable Phase 3 until you've reviewed Phase 2 dry-run data.** The whole
point of Phase 2 was to confirm Kalshi prices are reasonable vs. the venue
you're already on (DK Pred Markets, sportsbook, etc.). Skipping Phase 2
verification means you're flying blind.

### Architecture

```
11:50 AM ET  — log-picks.yml
~11:51 AM ET — kalshi-dry-run.yml          (paper, read-only key)
~11:52 AM ET — kalshi-place-orders.yml     (Phase 3 — REAL ORDERS, trade key)
                ↓ runs scripts/kalshi/place_orders.py
                ↓ checks all hard kill switches before any API call
                ↓ places limit orders on Kalshi
                ↓ commits data/kalshi_orders/{date}.json
04:00 AM ET  — log-results.yml
~04:01 AM ET — grade-picks.yml
~04:02 AM ET — kalshi-reconcile.yml         (paper PnL + LIVE PnL update)
                ↓ writes both data/kalshi_dryrun_perf.json
                ↓ AND   data/kalshi_live_perf.json
```

### Hard kill switches (every one MUST pass before any order fires)

1. `auto_trading_enabled: true` in config (master flag)
2. KALSHI_API_KEY_ID + KALSHI_PRIVATE_KEY env vars present
3. Account balance ≥ today's planned total stake
4. Yesterday's reconciled PnL > `-kill_switch_daily_loss_dollars`
5. No prior orders for this date (idempotency — re-runs are no-ops)

Per-order gates (each order checked individually):

6. `would_place: true` from dry-run
7. stake_dollars > 0 and ≤ `max_stake_per_pick_dollars`
8. cumulative-stake-so-far + this stake ≤ `max_daily_exposure_dollars`
9. We don't already have a position on this market
10. Limit order price in (1, 99) cents (defensive)

### Setup steps for Phase 3

#### 1. Generate a NEW API key with Trade permission

Your current Phase 1+2 key is Read-only. For Phase 3 you need a SECOND key
with Trade permission. Don't replace the existing one — having both lets
the dry-run pipeline keep working with the safer read-only auth.

```bash
openssl genrsa -out ~/.config/kalshi/live_trade.pem 2048
chmod 600 ~/.config/kalshi/live_trade.pem
openssl rsa -in ~/.config/kalshi/live_trade.pem -pubout -out ~/.config/kalshi/live_trade.pub
```

In Kalshi Settings → API Keys → Create new API key:
- Nickname: `BTF Phase 3 Trade`
- Paste contents of `live_trade.pub`
- **Permission: Read/Write** (this time)
- Copy the Key ID UUID

#### 2. Deposit funds on Kalshi

Start small — recommended $50-100. The default config caps single-pick
stake at $5 and daily exposure at $20, so $50 lasts at least 2-3 days
even on a complete blowout.

#### 3. Add the new secrets to repo

Settings → Secrets and variables → Actions:

- `KALSHI_TRADE_KEY_ID` = the new Key ID UUID from step 1
- `KALSHI_TRADE_PRIVATE_KEY` = full PEM contents of `live_trade.pem`

These are SEPARATE from the existing read-only secrets. Both pairs
coexist — read-only powers dry-run; trade key powers live placement.

#### 4. Update config bankroll (optional)

Edit `data/kalshi_config.json`:

```json
"bankroll_dollars": 50    ← match what you actually deposited
```

This is used for Kelly sizing math. The actual cash check happens against
your real Kalshi balance via API.

#### 5. Flip the master flag

Edit `data/kalshi_config.json`:

```json
"auto_trading_enabled": true
```

Commit + push. The next scheduled run (or manual workflow_dispatch) will
attempt placement.

### What you'll see when Phase 3 fires

- **Workflow log** in Actions tab shows each order placed (or skipped reason)
- **Hub panel** turns green and shows "💰 Kalshi LIVE" with placed orders
- **`data/kalshi_orders/{date}.json`** gets the full receipt with order_id
- **Tomorrow morning** after reconcile: outcomes + PnL annotated, live perf
  file updates

### How to ramp up safely

After 5 days of clean live operation with positive PnL, consider:

```json
"max_stake_per_pick_dollars": 5  → 10
"max_daily_exposure_dollars": 20 → 50
"bankroll_dollars": 50           → 200
"kill_switch_daily_loss_dollars": 25 → 50
```

Don't ramp on a single day's good result. Wait for sustained positive
PnL across at least 5+ days and 15+ orders.

### How to pause / kill

Three levels of stop:

1. **Soft pause** — flip `auto_trading_enabled: false` in config, commit, push.
   Workflow runs but exits cleanly without placing.
2. **Hard pause** — disable the `Kalshi Place Orders (Live)` workflow in
   Actions tab. Even won't run.
3. **Emergency stop** — revoke the Trade-permission API key in Kalshi
   settings. Even if our code went rogue, Kalshi rejects every request.

## Common issues

**`401 Unauthorized` on balance check**
→ Your `KALSHI_API_KEY_ID` doesn't match the public key Kalshi has registered for that ID. Re-create the key pair and re-upload.

**`403 Forbidden`**
→ Key permissions are missing. In Kalshi settings, ensure the key has at least Read.

**`No Kalshi event matched X @ Y` for every pick**
→ Kalshi's sports series tickers may have changed. Check `SPORT_TO_SERIES` in `pick_mapper.py` against Kalshi's current list at https://kalshi.com.

**`cryptography` import error**
→ `pip install cryptography` (or `pip3 install` depending on your environment).

## Files in this module

- `auth.py` — RSA-PSS request signing
- `client.py` — REST API client (read-only in Phase 1)
- `pick_mapper.py` — Match BTF picks → Kalshi market tickers
- `check_setup.py` — Run this first to validate your setup
- `__init__.py` — Module init / docstring

## Risk parameters

`data/kalshi_config.json` holds all risk caps and feature flags. Defaults are conservative:
- `auto_trading_enabled: false` (master kill switch)
- `environment: "demo"`
- `max_stake_per_pick_dollars: 10`
- `max_daily_exposure_dollars: 50`
- `kelly_fraction: 0.25`

Edit this file to tune. None of it activates until Phase 3 ships.
