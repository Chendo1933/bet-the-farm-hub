"""
Kalshi v2 REST API client — Phase 1 (read-only).

Phase 1 surface area:
  - get_balance()           — current portfolio cash balance
  - list_events(...)        — discover sports/event groupings
  - list_markets(...)       — markets within an event or by series ticker
  - get_market(ticker)      — full market detail (orderbook, etc.)
  - search_markets(query)   — name/ticker substring match across active markets

Phase 2/3 will add:
  - place_order(...)        — limit/market orders
  - cancel_order(...)
  - get_orders()            — open + filled orders
  - get_positions()         — current positions

Environment selection:
  KALSHI_ENVIRONMENT=demo   → https://demo-api.kalshi.co/trade-api/v2  (default)
  KALSHI_ENVIRONMENT=live   → https://api.elections.kalshi.com/trade-api/v2

Demo accounts get $1k of fake money. Validate everything in demo first.
"""
from __future__ import annotations

import os
import time
from typing import Iterator
from urllib.parse import urlencode

try:
    import requests
except ImportError as e:
    raise ImportError("requests library required: pip install requests") from e

from . import auth as _auth

# Kalshi v2 endpoints. Verified against docs at
# https://trading-api.readme.io/reference/test-in-the-demo-environment on 2026-05-09.
# Demo also supports https://demo-api.kalshi.co/trade-api/v2 as fallback.
DEMO_BASE = "https://external-api.demo.kalshi.co/trade-api/v2"
LIVE_BASE = "https://api.elections.kalshi.com/trade-api/v2"


class KalshiAPIError(Exception):
    """Raised on non-2xx responses or unexpected payloads."""
    def __init__(self, status: int, body: str, path: str):
        super().__init__(f"Kalshi API {status} on {path}: {body[:300]}")
        self.status = status
        self.body = body
        self.path = path


class KalshiClient:
    def __init__(self, environment: str | None = None, timeout: int = 15):
        env = (environment or os.environ.get("KALSHI_ENVIRONMENT", "demo")).lower()
        if env not in ("demo", "live"):
            raise ValueError(f"KALSHI_ENVIRONMENT must be 'demo' or 'live' (got {env!r})")
        self.environment = env
        self.base_url = DEMO_BASE if env == "demo" else LIVE_BASE
        self.timeout = timeout
        # Lazily load — don't fail on import if user hasn't set up keys yet.
        self._private_key = None
        self._key_id = None

    def _ensure_auth(self):
        if self._private_key is None:
            self._private_key = _auth.load_private_key()
            self._key_id = _auth.get_api_key_id()

    def _request(self, method: str, path: str, params: dict | None = None,
                 require_auth: bool = True, max_retries: int = 3) -> dict:
        # Path used in signing must match what we send (including query string).
        full_path = path
        if params:
            full_path = f"{path}?{urlencode(params)}"
        url = f"{self.base_url}{full_path}"
        # Kalshi signs the path including the /trade-api/v2 prefix exactly
        # as sent. Re-sign each retry so the timestamp stays fresh.
        sign_path = f"/trade-api/v2{full_path}"

        for attempt in range(max_retries + 1):
            headers = {"Accept": "application/json"}
            if require_auth:
                self._ensure_auth()
                headers.update(_auth.auth_headers(self._private_key, self._key_id, method, sign_path))
            resp = requests.request(method.upper(), url, headers=headers, timeout=self.timeout)

            # Retry on 429 (rate limit) with backoff. Kalshi may include a
            # Retry-After header indicating seconds to wait; if not, fall back
            # to exponential 1s/2s/4s schedule.
            if resp.status_code == 429 and attempt < max_retries:
                ra = resp.headers.get("Retry-After", "")
                try:
                    wait = float(ra) if ra else (2 ** attempt)
                except ValueError:
                    wait = 2 ** attempt
                print(f"  [kalshi rate limit] sleeping {wait}s (attempt {attempt+1}/{max_retries})")
                import time as _time
                _time.sleep(wait)
                continue

            if resp.status_code >= 400:
                raise KalshiAPIError(resp.status_code, resp.text, full_path)
            if not resp.text:
                return {}
            return resp.json()
        # Should not reach here, but defensive
        raise KalshiAPIError(429, "Rate limit exceeded after retries", full_path)

    # ── Account ─────────────────────────────────────────────────────────────
    def get_balance(self) -> dict:
        """Returns {balance: int_cents}. Convert to dollars by /100."""
        return self._request("GET", "/portfolio/balance")

    # ── Discovery ───────────────────────────────────────────────────────────
    def list_events(self, status: str = "open", limit: int = 100,
                    series_ticker: str | None = None,
                    cursor: str | None = None) -> dict:
        """
        List events (groupings of related markets). Status: 'open' | 'closed' | 'settled'.
        Sports series tickers (current set on Kalshi varies — verify):
          KXMLBGAME       MLB single-game contracts
          KXNBAGAME       NBA single-game
          KXNHLGAME       NHL single-game
          KXNFLGAME       NFL single-game (in season)
        """
        params: dict = {"status": status, "limit": min(max(limit, 1), 200)}
        if series_ticker: params["series_ticker"] = series_ticker
        if cursor: params["cursor"] = cursor
        return self._request("GET", "/events", params=params)

    def list_markets(self, status: str = "open", limit: int = 100,
                     event_ticker: str | None = None,
                     series_ticker: str | None = None,
                     tickers: list[str] | None = None,
                     cursor: str | None = None) -> dict:
        """List markets — filterable by event or series. Returns {markets: [...], cursor: '...'}."""
        params: dict = {"status": status, "limit": min(max(limit, 1), 1000)}
        if event_ticker: params["event_ticker"] = event_ticker
        if series_ticker: params["series_ticker"] = series_ticker
        if tickers: params["tickers"] = ",".join(tickers)
        if cursor: params["cursor"] = cursor
        return self._request("GET", "/markets", params=params)

    def iter_markets(self, **filters) -> Iterator[dict]:
        """Auto-paginate through all matching markets. Yields one market dict at a time."""
        cursor = None
        while True:
            resp = self.list_markets(cursor=cursor, **filters)
            for m in resp.get("markets", []):
                yield m
            cursor = resp.get("cursor")
            if not cursor:
                return
            time.sleep(0.1)  # courtesy rate-limit

    def get_market(self, ticker: str) -> dict:
        """Full detail for one market — orderbook, last trade, settlement source."""
        return self._request("GET", f"/markets/{ticker}")

    # ── Convenience search ──────────────────────────────────────────────────
    def search_markets_by_text(self, query: str, status: str = "open",
                               max_scan: int = 500) -> list[dict]:
        """
        Substring match across market titles + tickers. Useful for finding
        markets that match our pick (e.g., 'Yankees @ Red Sox'). Scans up to
        max_scan markets to bound API usage.
        """
        q = query.lower()
        matches = []
        scanned = 0
        for m in self.iter_markets(status=status):
            scanned += 1
            title = (m.get("title", "") + " " + m.get("subtitle", "") + " " + m.get("ticker", "")).lower()
            if q in title:
                matches.append(m)
            if scanned >= max_scan:
                break
        return matches
