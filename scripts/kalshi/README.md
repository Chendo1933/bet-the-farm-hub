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
