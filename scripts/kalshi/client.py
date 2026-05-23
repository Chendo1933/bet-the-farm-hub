"""
Kalshi v2 REST API client.

Read-only surface (Phase 1):
  - get_balance()           — current portfolio cash balance
  - list_events(...)        — discover sports/event groupings
  - list_markets(...)       — markets within an event or by series ticker
  - get_market(ticker)      — full market detail (orderbook, etc.)
  - search_markets(query)   — name/ticker substring match across active markets

Trading surface (Phase 3 — requires Trade-permission API key):
  - create_order(...)       — place limit/market orders (POST /portfolio/orders)
  - get_orders(...)         — list open + recent orders
  - get_order(order_id)     — single order detail
  - cancel_order(order_id)  — cancel an open order
  - get_positions(...)      — current positions (open contracts)

Environment selection:
  KALSHI_ENVIRONMENT=demo   → https://external-api.demo.kalshi.co/trade-api/v2
  KALSHI_ENVIRONMENT=live   → https://api.elections.kalshi.com/trade-api/v2

Always validate Phase 3 changes in demo first — Kalshi demo gives $1k fake
funds and supports the full trading surface.
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
                 json_body: dict | None = None,
                 require_auth: bool = True, max_retries: int = 3) -> dict:
        full_path = path
        if params:
            full_path = f"{path}?{urlencode(params)}"
        url = f"{self.base_url}{full_path}"
        # Kalshi signs timestamp + METHOD + path, where `path` is the route
        # WITHOUT the query string. Including the query string here produced
        # `INCORRECT_API_KEY_SIGNATURE` (401) on strict-auth endpoints with
        # params — e.g. GET /portfolio/positions?limit=200 — while no-query
        # routes like /portfolio/balance worked, and public market endpoints
        # (/events, /markets) aren't strictly auth-checked so they slipped by.
        # Sign the bare path (no query); the query still rides in the URL.
        # Re-sign each retry so the timestamp stays fresh.
        sign_path = f"/trade-api/v2{path}"

        for attempt in range(max_retries + 1):
            headers = {"Accept": "application/json"}
            if json_body is not None:
                headers["Content-Type"] = "application/json"
            if require_auth:
                self._ensure_auth()
                headers.update(_auth.auth_headers(self._private_key, self._key_id, method, sign_path))
            resp = requests.request(method.upper(), url, headers=headers,
                                    json=json_body, timeout=self.timeout)

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

    # ── Trading endpoints (Phase 3) ─────────────────────────────────────────
    # These call the order-placement and position endpoints. They REQUIRE
    # an API key with Trade permission (the Phase-1 read-only key will return
    # 403 from Kalshi). All write methods are idempotent via client_order_id
    # where supported, so retries don't double-place.

    def create_order(self, *,
                     ticker: str,
                     side: str,            # 'yes' or 'no'
                     action: str,          # 'buy' or 'sell'
                     count: int,           # number of contracts
                     order_type: str = "limit",   # 'limit' or 'market'
                     yes_price_cents: int | None = None,  # required for limit YES
                     no_price_cents: int | None = None,   # required for limit NO
                     client_order_id: str | None = None,  # idempotency key
                     time_in_force: str | None = None) -> dict:
        """
        Place a single order. Returns the order dict with order_id, status,
        filled_count, etc.

        Defensive checks (raise ValueError on bad input rather than letting
        Kalshi reject with an unclear message):
          - count must be positive
          - side must be 'yes' or 'no' (lowercase)
          - action must be 'buy' or 'sell'
          - limit orders require the matching-side price in (1, 99) cents
        """
        if count <= 0:
            raise ValueError(f"order count must be positive (got {count})")
        side = (side or "").lower()
        action = (action or "").lower()
        if side not in ("yes", "no"):
            raise ValueError(f"side must be 'yes' or 'no' (got {side!r})")
        if action not in ("buy", "sell"):
            raise ValueError(f"action must be 'buy' or 'sell' (got {action!r})")
        if order_type == "limit":
            if side == "yes" and (yes_price_cents is None or not (1 <= yes_price_cents <= 99)):
                raise ValueError(f"limit YES order needs yes_price_cents in 1..99 (got {yes_price_cents})")
            if side == "no" and (no_price_cents is None or not (1 <= no_price_cents <= 99)):
                raise ValueError(f"limit NO order needs no_price_cents in 1..99 (got {no_price_cents})")

        body = {
            "ticker":     ticker,
            "side":       side,
            "action":     action,
            "count":      count,
            "type":       order_type,
        }
        if yes_price_cents is not None: body["yes_price"] = int(yes_price_cents)
        if no_price_cents  is not None: body["no_price"]  = int(no_price_cents)
        if client_order_id is not None: body["client_order_id"] = client_order_id
        if time_in_force   is not None: body["time_in_force"]   = time_in_force

        return self._request("POST", "/portfolio/orders", json_body=body)

    def get_orders(self, *,
                   ticker: str | None = None,
                   status: str | None = None,    # 'resting' | 'canceled' | 'executed'
                   limit: int = 100,
                   cursor: str | None = None) -> dict:
        """List your orders. Filterable by ticker + status."""
        params = {"limit": min(max(limit, 1), 1000)}
        if ticker: params["ticker"] = ticker
        if status: params["status"] = status
        if cursor: params["cursor"] = cursor
        return self._request("GET", "/portfolio/orders", params=params)

    def get_order(self, order_id: str) -> dict:
        """Fetch a single order by ID."""
        return self._request("GET", f"/portfolio/orders/{order_id}")

    def cancel_order(self, order_id: str) -> dict:
        """Cancel an open order. Returns the order with status='canceled'."""
        return self._request("DELETE", f"/portfolio/orders/{order_id}")

    def get_positions(self, *,
                      ticker: str | None = None,
                      event_ticker: str | None = None,
                      limit: int = 100,
                      cursor: str | None = None) -> dict:
        """Current open positions."""
        params = {"limit": min(max(limit, 1), 1000)}
        if ticker:       params["ticker"] = ticker
        if event_ticker: params["event_ticker"] = event_ticker
        if cursor:       params["cursor"] = cursor
        return self._request("GET", "/portfolio/positions", params=params)

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
