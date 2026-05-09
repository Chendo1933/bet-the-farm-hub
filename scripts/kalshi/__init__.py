"""
Kalshi integration for Bet The Farm Hub.

Phased buildout:
  Phase 1 (current): read-only client + pick→market mapper + setup checker.
                     No orders placed. Verifies API connectivity, account
                     access, and that our picks have matching Kalshi markets.
  Phase 2: dry-run order simulation against logged picks files.
  Phase 3: live order placement with risk caps + kill switches.
  Phase 4: workflow integration + reconciliation with grading.

Auth model: Kalshi v2 API uses RSA-PSS-SHA256 signed requests.
  - KALSHI_API_KEY_ID    — public key ID (UUID)
  - KALSHI_PRIVATE_KEY   — RSA private key (PEM format, env var or file)
  - KALSHI_ENVIRONMENT   — 'demo' (default) or 'live'

See scripts/kalshi/README.md for setup instructions.
"""
