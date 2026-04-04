"""
Kalshi REST API v2 synchronous client for PolyEdge v5.

Authentication
--------------
Every authenticated request is signed with RSA-PSS (SHA-256).
The signed message is:  str(timestamp_ms) + METHOD.upper() + path
Headers added:          KALSHI-ACCESS-KEY, KALSHI-ACCESS-TIMESTAMP,
                        KALSHI-ACCESS-SIGNATURE

Public endpoints (/markets, /events, /series, /orderbook) require no auth.
Portfolio endpoints (/portfolio/*) require auth.

Rate limiting
-------------
Two token-bucket rate limiters enforce the Basic-tier limits:
  reads  — 20 requests / second
  writes — 10 requests / second

Retry behaviour
---------------
HTTP 429 → exponential backoff: 1 s, 2 s, 4 s, 8 s, 16 s (5 attempts max).
HTTP 5xx → same backoff schedule.
All other 4xx → raise immediately (no retry).

Price convention
----------------
Internally prices are floats in dollars: 0.52 = 52 ¢.
The Kalshi API v2 uses integer cents (1–99).
Conversion happens at the API boundary inside this module.
"""

import base64
import logging
import random
import threading
import time
from pathlib import Path
from typing import Any

import requests
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPrivateKey

from config import constants as C

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------

class KalshiAPIError(Exception):
    """Raised when the Kalshi API returns an error response."""

    def __init__(self, status_code: int, path: str, body: str) -> None:
        self.status_code = status_code
        self.path = path
        self.body = body
        super().__init__(f"HTTP {status_code} on {path}: {body[:200]}")


class KalshiRateLimitError(KalshiAPIError):
    """Raised when all backoff retries are exhausted on a 429."""


class KalshiAuthError(KalshiAPIError):
    """Raised on 401 / 403 — bad credentials or missing signature."""


# ---------------------------------------------------------------------------
# Token-bucket rate limiter
# ---------------------------------------------------------------------------

class _TokenBucket:
    """Thread-safe token-bucket rate limiter.

    Blocks the calling thread (time.sleep) until a token is available.
    """

    def __init__(self, rate: float) -> None:
        self._rate = rate          # tokens per second
        self._tokens: float = rate
        self._last_refill = time.monotonic()
        self._lock = threading.Lock()

    def consume(self) -> None:
        with self._lock:
            now = time.monotonic()
            elapsed = now - self._last_refill
            self._tokens = min(self._rate, self._tokens + elapsed * self._rate)
            self._last_refill = now

            if self._tokens < 1.0:
                sleep_for = (1.0 - self._tokens) / self._rate
                time.sleep(sleep_for)
                self._tokens = 0.0
            else:
                self._tokens -= 1.0


# ---------------------------------------------------------------------------
# KalshiClient
# ---------------------------------------------------------------------------

_WRITE_METHODS = frozenset({"POST", "DELETE", "PUT", "PATCH"})
_RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})
_MAX_RETRIES = 5


class KalshiClient:
    """Synchronous Kalshi REST API v2 client.

    Instantiate once and reuse; the underlying requests.Session is kept alive.

    Usage::

        client = KalshiClient()
        client.authenticate()          # verify credentials
        markets = client.get_markets(status="open")
        ob      = client.get_orderbook("INXW-26APR04-T5300")
        order   = client.place_order(
            ticker="INXW-26APR04-T5300",
            side="yes",
            action="buy",
            count=10,
            order_type="limit",
            price=0.52,
        )
    """

    def __init__(
        self,
        api_key_id: str | None = None,
        private_key_path: str | None = None,
        base_url: str | None = None,
    ) -> None:
        """
        Args:
            api_key_id:        Kalshi API key ID (defaults to settings.KALSHI_API_KEY_ID).
            private_key_path:  Path to PEM private key file
                               (defaults to settings.KALSHI_PRIVATE_KEY_PATH).
            base_url:          Override base URL (defaults to settings.KALSHI_BASE_URL,
                               which respects KALSHI_ENV).
        """
        # Import here so the module is importable even if .env is incomplete.
        from config import settings as S

        self._api_key_id: str = api_key_id or S.KALSHI_API_KEY_ID
        self._base_url: str = (base_url or S.KALSHI_BASE_URL).rstrip("/")
        self._env: str = S.KALSHI_ENV

        key_path = Path(private_key_path or S.KALSHI_PRIVATE_KEY_PATH)
        self._private_key: RSAPrivateKey = self._load_private_key(key_path)

        self._session = requests.Session()
        self._session.headers.update({"Content-Type": "application/json"})

        self._read_limiter = _TokenBucket(C.KALSHI_MAX_READS_PER_SEC)
        self._write_limiter = _TokenBucket(C.KALSHI_MAX_WRITES_PER_SEC)

        logger.info(
            "KalshiClient initialised  env=%s  base_url=%s",
            self._env,
            self._base_url,
        )

    # ------------------------------------------------------------------
    # Authentication helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _load_private_key(path: Path) -> RSAPrivateKey:
        if not path.exists():
            raise FileNotFoundError(
                f"Kalshi private key not found at '{path}'. "
                "Set KALSHI_PRIVATE_KEY_PATH in .env to the correct location."
            )
        pem_bytes = path.read_bytes()
        key = serialization.load_pem_private_key(pem_bytes, password=None)
        if not isinstance(key, RSAPrivateKey):
            raise TypeError(f"Expected RSA private key; got {type(key).__name__}")
        return key

    def _auth_headers(self, method: str, path: str) -> dict[str, str]:
        """Generate the three Kalshi auth headers for a single request.

        The signed message is: timestamp_ms_str + METHOD.upper() + path
        where `path` is the URL path without query parameters.
        """
        timestamp_ms = str(int(time.time() * 1000))
        message = (timestamp_ms + method.upper() + path).encode("utf-8")

        signature_bytes = self._private_key.sign(
            message,
            padding.PSS(
                mgf=padding.MGF1(hashes.SHA256()),
                salt_length=padding.PSS.DIGEST_LENGTH,
            ),
            hashes.SHA256(),
        )

        return {
            "KALSHI-ACCESS-KEY": self._api_key_id,
            "KALSHI-ACCESS-TIMESTAMP": timestamp_ms,
            "KALSHI-ACCESS-SIGNATURE": base64.b64encode(signature_bytes).decode(),
        }

    def authenticate(self) -> dict[str, Any]:
        """Verify credentials by fetching the portfolio balance.

        Returns the balance response dict on success.
        Raises KalshiAuthError if credentials are invalid.
        """
        logger.info("Verifying Kalshi credentials (env=%s)", self._env)
        return self.get_balance()

    # ------------------------------------------------------------------
    # Core HTTP layer
    # ------------------------------------------------------------------

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
        authenticated: bool = True,
    ) -> dict[str, Any]:
        """Make an HTTP request with rate-limiting, auth, and retry logic.

        Args:
            method:        HTTP verb (GET, POST, DELETE …).
            path:          URL path, e.g. "/markets/INXW-26APR04-T5300".
            params:        Query parameters.
            json:          Request body (serialised as JSON).
            authenticated: If True, attach RSA-PSS auth headers.

        Returns:
            Parsed JSON response body as a dict.

        Raises:
            KalshiAuthError:      401 / 403.
            KalshiRateLimitError: 429 after all retries exhausted.
            KalshiAPIError:       Any other non-2xx after retries exhausted.
        """
        is_write = method.upper() in _WRITE_METHODS
        limiter = self._write_limiter if is_write else self._read_limiter
        url = self._base_url + path

        last_exc: Exception | None = None

        for attempt in range(_MAX_RETRIES):
            limiter.consume()

            headers: dict[str, str] = {}
            if authenticated:
                headers = self._auth_headers(method, path)

            t0 = time.monotonic()
            try:
                resp = self._session.request(
                    method,
                    url,
                    params=params,
                    json=json,
                    headers=headers,
                    timeout=10,
                )
            except requests.exceptions.RequestException as exc:
                logger.warning(
                    "kalshi_request  method=%s path=%s attempt=%d network_error=%s",
                    method, path, attempt + 1, exc,
                )
                last_exc = exc
                time.sleep(self._backoff_seconds(attempt))
                continue

            latency_ms = int((time.monotonic() - t0) * 1000)

            logger.info(
                "kalshi_request  method=%s path=%s status=%d latency_ms=%d attempt=%d env=%s",
                method, path, resp.status_code, latency_ms, attempt + 1, self._env,
            )

            # ---- 2xx success ----------------------------------------
            if resp.status_code < 300:
                return resp.json() if resp.content else {}

            # ---- auth errors — do not retry -------------------------
            if resp.status_code in (401, 403):
                raise KalshiAuthError(resp.status_code, path, resp.text)

            # ---- client errors (non-429) — do not retry -------------
            if resp.status_code < 500 and resp.status_code != 429:
                raise KalshiAPIError(resp.status_code, path, resp.text)

            # ---- retryable (429 or 5xx) -----------------------------
            wait = self._backoff_seconds(attempt)
            logger.warning(
                "kalshi_retryable  method=%s path=%s status=%d attempt=%d wait_s=%.2f",
                method, path, resp.status_code, attempt + 1, wait,
            )
            last_exc = KalshiAPIError(resp.status_code, path, resp.text)
            time.sleep(wait)

        # All retries exhausted
        if isinstance(last_exc, KalshiAPIError) and last_exc.status_code == 429:
            raise KalshiRateLimitError(429, path, str(last_exc)) from last_exc
        raise last_exc  # type: ignore[misc]

    @staticmethod
    def _backoff_seconds(attempt: int) -> float:
        """Full-jitter exponential backoff: base 2^attempt seconds ± jitter."""
        cap = 30.0
        base = min(cap, 2.0 ** attempt)
        return base + random.uniform(0.0, 1.0)

    def _get(
        self,
        path: str,
        params: dict[str, Any] | None = None,
        authenticated: bool = False,
    ) -> dict[str, Any]:
        return self._request("GET", path, params=params, authenticated=authenticated)

    def _post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        return self._request("POST", path, json=body, authenticated=True)

    def _delete(self, path: str) -> dict[str, Any]:
        return self._request("DELETE", path, authenticated=True)

    # ------------------------------------------------------------------
    # Pagination helper
    # ------------------------------------------------------------------

    def _paginate(
        self,
        path: str,
        result_key: str,
        params: dict[str, Any] | None = None,
        authenticated: bool = False,
        page_limit: int = 200,
        max_pages: int = 50,
    ) -> list[dict[str, Any]]:
        """Fetch all pages for a cursor-paginated endpoint.

        Args:
            path:        API path.
            result_key:  Key in the response dict that holds the list.
            params:      Base query parameters (cursor added automatically).
            page_limit:  Items per page (passed as `limit`).
            max_pages:   Hard ceiling to prevent infinite loops.
        """
        base_params: dict[str, Any] = dict(params or {})
        base_params["limit"] = page_limit
        all_items: list[dict[str, Any]] = []

        for _ in range(max_pages):
            data = self._get(path, params=base_params, authenticated=authenticated)
            items = data.get(result_key, [])
            all_items.extend(items)

            cursor = data.get("cursor")
            if not cursor or not items:
                break
            base_params["cursor"] = cursor

        return all_items

    # ------------------------------------------------------------------
    # Public endpoints (no auth required)
    # ------------------------------------------------------------------

    def get_markets(
        self,
        status: str = "open",
        series_ticker: str | None = None,
        event_ticker: str | None = None,
        tickers: list[str] | None = None,
        min_close_ts: int | None = None,
        max_close_ts: int | None = None,
        fetch_all: bool = True,
    ) -> list[dict[str, Any]]:
        """List Kalshi markets.

        Args:
            status:         "open" | "closed" | "settled".
            series_ticker:  Filter to a specific series.
            event_ticker:   Filter to a specific event.
            tickers:        Comma-separated list of specific tickers.
            min_close_ts:   Unix timestamp — only markets closing after this.
            max_close_ts:   Unix timestamp — only markets closing before this.
            fetch_all:      If True, follow cursor pagination to retrieve all pages.

        Returns:
            List of market dicts.
        """
        params: dict[str, Any] = {"status": status}
        if series_ticker:
            params["series_ticker"] = series_ticker
        if event_ticker:
            params["event_ticker"] = event_ticker
        if tickers:
            params["tickers"] = ",".join(tickers)
        if min_close_ts is not None:
            params["min_close_ts"] = min_close_ts
        if max_close_ts is not None:
            params["max_close_ts"] = max_close_ts

        if fetch_all:
            return self._paginate("/markets", "markets", params=params)

        data = self._get("/markets", params=params)
        return data.get("markets", [])

    def get_market(self, ticker: str) -> dict[str, Any]:
        """Fetch a single market by ticker.

        Returns the market dict (unwrapped from {"market": {...}}).
        """
        data = self._get(f"/markets/{ticker}")
        return data.get("market", data)

    def get_orderbook(self, ticker: str, depth: int = 10) -> dict[str, Any]:
        """Fetch the order book for a market.

        Returns {"yes": [[price_cents, size], ...], "no": [[price_cents, size], ...]}
        where price_cents is an integer (1–99).
        """
        data = self._get(
            f"/markets/{ticker}/orderbook",
            params={"depth": depth},
        )
        return data.get("orderbook", data)

    def get_events(
        self,
        status: str = "open",
        series_ticker: str | None = None,
        with_nested_markets: bool = False,
        fetch_all: bool = True,
    ) -> list[dict[str, Any]]:
        """List Kalshi events.

        Args:
            status:                "open" | "closed" | "settled".
            series_ticker:         Filter to a specific series.
            with_nested_markets:   If True, include nested market objects.
            fetch_all:             If True, follow cursor pagination.
        """
        params: dict[str, Any] = {
            "status": status,
            "with_nested_markets": str(with_nested_markets).lower(),
        }
        if series_ticker:
            params["series_ticker"] = series_ticker

        if fetch_all:
            return self._paginate("/events", "events", params=params)

        data = self._get("/events", params=params)
        return data.get("events", [])

    def get_event(self, event_ticker: str) -> dict[str, Any]:
        """Fetch a single event by its ticker."""
        data = self._get(f"/events/{event_ticker}")
        return data.get("event", data)

    def get_series(self, fetch_all: bool = True) -> list[dict[str, Any]]:
        """List all Kalshi series."""
        if fetch_all:
            return self._paginate("/series", "series")
        data = self._get("/series")
        return data.get("series", [])

    def get_series_detail(self, series_ticker: str) -> dict[str, Any]:
        """Fetch a single series by ticker."""
        data = self._get(f"/series/{series_ticker}")
        return data.get("series", data)

    # ------------------------------------------------------------------
    # Authenticated portfolio endpoints
    # ------------------------------------------------------------------

    def get_balance(self) -> dict[str, Any]:
        """Return portfolio balance.

        Response includes: balance (cents), payout (cents), and other fields.
        We convert balance and payout to dollars before returning.
        """
        data = self._request("GET", "/portfolio/balance", authenticated=True)
        balance = data.get("balance", {})
        # Convert cent fields to dollars for internal consistency
        for field in ("balance", "payout"):
            if field in balance and isinstance(balance[field], (int, float)):
                balance[field] = balance[field] / 100
        return balance

    def get_positions(
        self,
        settlement_status: str | None = None,
        ticker: str | None = None,
        event_ticker: str | None = None,
        fetch_all: bool = True,
    ) -> list[dict[str, Any]]:
        """Return open (or settled) positions.

        Args:
            settlement_status: "unsettled" | "settled" | "all".
            ticker:            Filter to a specific market ticker.
            event_ticker:      Filter to a specific event.
            fetch_all:         Follow cursor pagination.
        """
        params: dict[str, Any] = {}
        if settlement_status:
            params["settlement_status"] = settlement_status
        if ticker:
            params["ticker"] = ticker
        if event_ticker:
            params["event_ticker"] = event_ticker

        if fetch_all:
            return self._paginate(
                "/portfolio/positions",
                "market_positions",
                params=params,
                authenticated=True,
            )

        data = self._request("GET", "/portfolio/positions", params=params, authenticated=True)
        return data.get("market_positions", [])

    def get_orders(
        self,
        ticker: str | None = None,
        event_ticker: str | None = None,
        status: str | None = None,
        fetch_all: bool = True,
    ) -> list[dict[str, Any]]:
        """Return portfolio orders.

        Args:
            ticker:       Filter by market ticker.
            event_ticker: Filter by event ticker.
            status:       "resting" | "canceled" | "executed" | "all".
            fetch_all:    Follow cursor pagination.
        """
        params: dict[str, Any] = {}
        if ticker:
            params["ticker"] = ticker
        if event_ticker:
            params["event_ticker"] = event_ticker
        if status:
            params["status"] = status

        if fetch_all:
            return self._paginate(
                "/portfolio/orders",
                "orders",
                params=params,
                authenticated=True,
            )

        data = self._request("GET", "/portfolio/orders", params=params, authenticated=True)
        return data.get("orders", [])

    def get_settlements(
        self,
        fetch_all: bool = True,
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        """Return settlement history for the portfolio."""
        if fetch_all:
            return self._paginate(
                "/portfolio/settlements",
                "settlements",
                authenticated=True,
            )

        data = self._request(
            "GET",
            "/portfolio/settlements",
            params={"limit": limit},
            authenticated=True,
        )
        return data.get("settlements", [])

    # ------------------------------------------------------------------
    # Order management (authenticated, write operations)
    # ------------------------------------------------------------------

    def place_order(
        self,
        ticker: str,
        side: str,
        action: str,
        count: int,
        order_type: str,
        price: float | None = None,
        client_order_id: str | None = None,
    ) -> dict[str, Any]:
        """Place a limit or market order.

        Args:
            ticker:           Market ticker (e.g. "INXW-26APR04-T5300").
            side:             "yes" or "no".
            action:           "buy" or "sell".
            count:            Number of contracts (≥ 5 for $5 minimum).
            order_type:       "limit" or "market".
            price:            Contract price in dollars (0.01–0.99).
                              Required for limit orders; ignored for market orders.
            client_order_id:  Optional idempotency key.

        Returns:
            The order dict from the API response.

        Raises:
            ValueError:      If a limit order is placed without a price, or
                             if price is outside the valid 0.01–0.99 range.
            KalshiAPIError:  On API error.
        """
        side = side.lower()
        action = action.lower()
        order_type = order_type.lower()

        if side not in ("yes", "no"):
            raise ValueError(f"side must be 'yes' or 'no', got '{side}'")
        if action not in ("buy", "sell"):
            raise ValueError(f"action must be 'buy' or 'sell', got '{action}'")
        if order_type not in ("limit", "market"):
            raise ValueError(f"order_type must be 'limit' or 'market', got '{order_type}'")
        if count < 1:
            raise ValueError(f"count must be >= 1, got {count}")

        body: dict[str, Any] = {
            "ticker": ticker,
            "action": action,
            "side": side,
            "type": order_type,
            "count": count,
        }

        if order_type == "limit":
            if price is None:
                raise ValueError("price is required for limit orders")
            if not (0.01 <= price <= 0.99):
                raise ValueError(f"price must be between 0.01 and 0.99, got {price}")
            price_cents = round(price * 100)
            # Kalshi uses yes_price/no_price depending on which side you're pricing
            if side == "yes":
                body["yes_price"] = price_cents
            else:
                body["no_price"] = price_cents

        if client_order_id:
            body["client_order_id"] = client_order_id

        logger.info(
            "place_order  ticker=%s side=%s action=%s type=%s count=%d price=%s",
            ticker, side, action, order_type, count,
            f"{price:.2f}" if price else "market",
        )

        data = self._post("/portfolio/orders", body)
        return data.get("order", data)

    def cancel_order(self, order_id: str) -> dict[str, Any]:
        """Cancel a resting order by its order ID.

        Returns the cancelled order dict.
        """
        logger.info("cancel_order  order_id=%s", order_id)
        data = self._delete(f"/portfolio/orders/{order_id}")
        return data.get("order", data)

    # ------------------------------------------------------------------
    # Convenience helpers
    # ------------------------------------------------------------------

    def get_yes_price(self, ticker: str) -> float | None:
        """Return the best YES ask price in dollars, or None if no liquidity."""
        market = self.get_market(ticker)
        yes_ask = market.get("yes_ask")
        if yes_ask is None:
            return None
        return yes_ask / 100  # cents → dollars

    def get_spread_cents(self, ticker: str) -> float | None:
        """Return the bid-ask spread in cents, or None if not calculable."""
        market = self.get_market(ticker)
        yes_ask = market.get("yes_ask")
        yes_bid = market.get("yes_bid")
        if yes_ask is None or yes_bid is None:
            return None
        return float(yes_ask - yes_bid)  # already in cents

    def is_market_tradeable(self, ticker: str) -> bool:
        """Quick check: is the market open and has acceptable spread?"""
        try:
            market = self.get_market(ticker)
        except KalshiAPIError:
            return False
        if market.get("status") != "open":
            return False
        spread = self.get_spread_cents(ticker)
        # MAX_SPREAD is in dollars; convert to cents for comparison
        max_spread_cents = C.MAX_SPREAD * 100
        if spread is None or spread > max_spread_cents:
            return False
        return True
