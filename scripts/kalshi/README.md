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
