"""
Kalshi WebSocket API v2 async client for PolyEdge v5.

Protocol summary
----------------
- Connect to wss://api.kalshi.com/trade-api/ws/v2 (or demo URL).
- Authenticate via the same RSA-PSS signed headers used by REST, passed as
  extra HTTP headers on the WebSocket handshake.
- Subscribe to channels per market ticker:
    {"id": N, "cmd": "subscribe",
     "params": {"channels": ["orderbook_delta", "ticker", "trade"],
                "market_tickers": ["INXW-26APR04-T5300"]}}
- Incoming messages have "type" and "msg" fields.
- Reconnect with exponential backoff on any disconnect; replay all
  subscriptions automatically.

Channels
--------
  ORDERBOOK  "orderbook_delta"  — incremental order-book updates (+ snapshot on first sub)
  TICKER     "ticker"           — best bid/ask, last price, volume
  TRADE      "trade"            — executed trades

Heartbeat
---------
Two layers:
  1. Protocol-level: websockets library sends RFC-6455 ping frames every
     PING_INTERVAL_S seconds; drops connection if no pong within PING_TIMEOUT_S.
  2. Application-level: _heartbeat_loop() independently tracks the last
     received message timestamp and triggers reconnect if the connection
     goes silent beyond STALE_TIMEOUT_S.

Usage
-----
    ws = KalshiWebSocket()

    @ws.on("orderbook_snapshot")
    async def handle_snapshot(msg: OrderbookSnapshot) -> None:
        print(msg.market_ticker, msg.yes[:3])

    @ws.on("ticker")
    async def handle_ticker(msg: TickerUpdate) -> None:
        print(msg.market_ticker, msg.yes_bid, msg.yes_ask)

    ws.subscribe(["INXW-26APR04-T5300"], [KalshiWebSocket.ORDERBOOK, KalshiWebSocket.TICKER])

    await ws.run_forever()   # blocks; call ws.stop() from another task to exit
"""

import asyncio
import base64
import dataclasses
import json
import logging
import random
import time
from collections import defaultdict
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import websockets
import websockets.exceptions
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPrivateKey

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Timing constants
# ---------------------------------------------------------------------------
PING_INTERVAL_S: float = 20.0     # RFC-6455 ping frame interval
PING_TIMEOUT_S: float  = 10.0     # Drop connection if no pong within this
STALE_TIMEOUT_S: float = 60.0     # App-level: reconnect if no message received
MAX_RECONNECT_ATTEMPTS: int = 0   # 0 = unlimited
_MAX_BACKOFF_S: float = 64.0

# ---------------------------------------------------------------------------
# Channel name constants
# ---------------------------------------------------------------------------
CHANNEL_ORDERBOOK: str = "orderbook_delta"
CHANNEL_TICKER:    str = "ticker"
CHANNEL_TRADE:     str = "trade"

# All channels that require auth (portfolio channels would go here too).
_AUTH_REQUIRED_CHANNELS: frozenset[str] = frozenset()  # market channels are public

# ---------------------------------------------------------------------------
# Structured message types
# ---------------------------------------------------------------------------

@dataclasses.dataclass(slots=True)
class OrderbookSnapshot:
    """Full orderbook snapshot sent on first subscription to orderbook_delta."""
    market_ticker: str
    yes: list[list[int]]   # [[price_cents, size], ...]  sorted best-ask first
    no:  list[list[int]]   # [[price_cents, size], ...]


@dataclasses.dataclass(slots=True)
class OrderbookDelta:
    """Incremental orderbook change after initial snapshot."""
    market_ticker: str
    side:  str   # "yes" | "no"
    price: int   # cents (1–99)
    delta: int   # signed: +N adds, -N removes contracts at this level
    ts:    int   # unix milliseconds


@dataclasses.dataclass(slots=True)
class TickerUpdate:
    """Best-bid/ask and last-price snapshot from the ticker channel."""
    market_ticker: str
    yes_bid:    int | None   # cents
    yes_ask:    int | None   # cents
    no_bid:     int | None   # cents
    no_ask:     int | None   # cents
    last_price: int | None   # cents
    volume:     int | None   # contracts
    open_interest: int | None
    ts:         int          # unix milliseconds


@dataclasses.dataclass(slots=True)
class TradeUpdate:
    """An executed trade on the exchange."""
    market_ticker: str
    yes_price:  int    # cents
    no_price:   int    # cents
    count:      int    # contracts
    taker_side: str    # "yes" | "no"
    ts:         int    # unix milliseconds


# Union type for typed callbacks
WsMessage = OrderbookSnapshot | OrderbookDelta | TickerUpdate | TradeUpdate

# Callback type: async fn that receives a structured message
MessageCallback = Callable[[WsMessage], Awaitable[None]]


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------

class WebSocketAuthError(Exception):
    """Raised when the WebSocket handshake is rejected for auth reasons."""


class WebSocketSubscribeError(Exception):
    """Raised when a subscribe command is rejected by the server."""


# ---------------------------------------------------------------------------
# KalshiWebSocket
# ---------------------------------------------------------------------------

class KalshiWebSocket:
    """Async Kalshi WebSocket client with auto-reconnect and heartbeat.

    All callbacks registered via ``on()`` must be ``async def`` functions.
    They are called sequentially per message (not concurrently) to keep
    downstream state mutation safe without extra locking.

    Lifecycle::

        ws = KalshiWebSocket()
        ws.subscribe(tickers, channels)       # queue before connecting
        ws.on("ticker")(my_async_callback)    # register handler

        task = asyncio.create_task(ws.run_forever())
        ...
        await ws.stop()
    """

    # Channel name aliases as class attributes for convenience
    ORDERBOOK = CHANNEL_ORDERBOOK
    TICKER    = CHANNEL_TICKER
    TRADE     = CHANNEL_TRADE

    def __init__(
        self,
        ws_url: str | None = None,
        api_key_id: str | None = None,
        private_key_path: str | None = None,
    ) -> None:
        from config import settings as S

        self._ws_url: str       = (ws_url or S.KALSHI_WS_URL).rstrip("/")
        self._api_key_id: str   = api_key_id or S.KALSHI_API_KEY_ID
        self._env: str          = S.KALSHI_ENV

        key_path = Path(private_key_path or S.KALSHI_PRIVATE_KEY_PATH)
        self._private_key: RSAPrivateKey = self._load_private_key(key_path)

        # Pending and active subscriptions: ticker -> set of channels
        self._desired: dict[str, set[str]] = defaultdict(set)

        # Callbacks: event_type -> list of async callables
        self._callbacks: dict[str, list[MessageCallback]] = defaultdict(list)

        # Internal state
        self._cmd_id: int = 0
        self._ws: websockets.WebSocketClientProtocol | None = None
        self._stopped: bool = False
        self._last_msg_ts: float = 0.0      # monotonic time of last received message
        self._reconnect_count: int = 0

        logger.info(
            "KalshiWebSocket created  env=%s  url=%s",
            self._env, self._ws_url,
        )

    # ------------------------------------------------------------------
    # Public API — subscription management
    # ------------------------------------------------------------------

    def subscribe(self, tickers: list[str], channels: list[str]) -> None:
        """Register interest in ``channels`` for each ticker in ``tickers``.

        If already connected, the subscription is sent immediately via
        ``subscribe_now()``.  Otherwise it is queued and replayed on connect.

        This method is *sync* so it can be called before the event loop starts.
        Use ``await subscribe_now()`` to send to a live connection.
        """
        for ticker in tickers:
            self._desired[ticker].update(channels)
        logger.debug(
            "subscribe queued  tickers=%s channels=%s", tickers, channels
        )

    def unsubscribe(self, tickers: list[str]) -> None:
        """Remove tickers from desired subscriptions.

        Does not send an unsubscribe command to the server (Kalshi WS v2 does
        not support per-ticker unsubscription mid-session; subscriptions are
        cleared automatically on reconnect).  The ticker will simply not be
        re-subscribed after the next reconnect.
        """
        for ticker in tickers:
            self._desired.pop(ticker, None)
        logger.debug("unsubscribe queued  tickers=%s", tickers)

    def on(self, event_type: str) -> Callable[[MessageCallback], MessageCallback]:
        """Decorator to register an async callback for a message type.

        ``event_type`` is one of: "orderbook_snapshot", "orderbook_delta",
        "ticker", "trade", "error", "subscribed".

        Example::

            @ws.on("ticker")
            async def handle(msg: TickerUpdate) -> None:
                ...
        """
        def decorator(fn: MessageCallback) -> MessageCallback:
            self._callbacks[event_type].append(fn)
            return fn
        return decorator

    def add_callback(self, event_type: str, fn: MessageCallback) -> None:
        """Imperative form of ``on()``."""
        self._callbacks[event_type].append(fn)

    # ------------------------------------------------------------------
    # Public API — lifecycle
    # ------------------------------------------------------------------

    async def run_forever(self) -> None:
        """Connect, subscribe, and process messages indefinitely.

        Reconnects with exponential backoff on any disconnect.
        Returns only when ``stop()`` is called.
        """
        self._stopped = False
        attempt = 0

        while not self._stopped:
            try:
                await self._connect_and_run()
                attempt = 0   # successful session; reset backoff
            except WebSocketAuthError:
                logger.error("WebSocket auth failed — check credentials. Stopping.")
                break
            except asyncio.CancelledError:
                break
            except Exception as exc:  # noqa: BLE001
                if self._stopped:
                    break
                wait = self._backoff(attempt)
                self._reconnect_count += 1
                logger.warning(
                    "ws_disconnected  reason=%s  reconnect_in=%.1fs  attempt=%d",
                    exc, wait, self._reconnect_count,
                )
                await asyncio.sleep(wait)
                attempt += 1

        logger.info("KalshiWebSocket stopped.")

    async def stop(self) -> None:
        """Signal the client to stop and close the connection gracefully."""
        self._stopped = True
        if self._ws is not None:
            try:
                await self._ws.close()
            except Exception:  # noqa: BLE001
                pass

    async def subscribe_now(self, tickers: list[str], channels: list[str]) -> None:
        """Send a subscribe command on the live connection.

        Also updates the desired-subscription table so the subscription is
        replayed after any future reconnect.
        """
        self.subscribe(tickers, channels)
        if self._ws is not None:
            await self._send_subscribe(self._ws, tickers, channels)

    # ------------------------------------------------------------------
    # Internal — connection lifecycle
    # ------------------------------------------------------------------

    async def _connect_and_run(self) -> None:
        """Open one WebSocket session and run until it closes."""
        headers = self._auth_headers()

        logger.info("ws_connecting  url=%s  env=%s", self._ws_url, self._env)

        async with websockets.connect(
            self._ws_url,
            extra_headers=headers,
            ping_interval=PING_INTERVAL_S,
            ping_timeout=PING_TIMEOUT_S,
            open_timeout=15,
            close_timeout=5,
            max_size=2**23,   # 8 MB max frame size
        ) as ws:
            self._ws = ws
            self._last_msg_ts = time.monotonic()
            logger.info("ws_connected  env=%s", self._env)

            # Replay all desired subscriptions
            await self._replay_subscriptions(ws)

            # Run message loop and heartbeat monitor concurrently;
            # if either exits the session is considered over.
            await asyncio.gather(
                self._message_loop(ws),
                self._heartbeat_loop(ws),
            )

        self._ws = None

    async def _replay_subscriptions(self, ws: websockets.WebSocketClientProtocol) -> None:
        """Re-subscribe to all desired tickers after a (re)connect."""
        if not self._desired:
            return

        # Group tickers by channel set to minimise command count
        channel_groups: dict[frozenset[str], list[str]] = defaultdict(list)
        for ticker, channels in self._desired.items():
            channel_groups[frozenset(channels)].append(ticker)

        for channels_frozen, tickers in channel_groups.items():
            await self._send_subscribe(ws, tickers, list(channels_frozen))

    async def _send_subscribe(
        self,
        ws: websockets.WebSocketClientProtocol,
        tickers: list[str],
        channels: list[str],
    ) -> None:
        """Send a subscribe command for given tickers and channels."""
        self._cmd_id += 1
        cmd = {
            "id": self._cmd_id,
            "cmd": "subscribe",
            "params": {
                "channels": channels,
                "market_tickers": tickers,
            },
        }
        payload = json.dumps(cmd)
        await ws.send(payload)
        logger.info(
            "ws_subscribe_sent  id=%d  tickers=%s  channels=%s",
            self._cmd_id, tickers, channels,
        )

    # ------------------------------------------------------------------
    # Internal — message loop
    # ------------------------------------------------------------------

    async def _message_loop(self, ws: websockets.WebSocketClientProtocol) -> None:
        """Read and dispatch messages until the connection closes."""
        async for raw in ws:
            self._last_msg_ts = time.monotonic()
            try:
                envelope = json.loads(raw)
            except json.JSONDecodeError:
                logger.warning("ws_bad_json  raw=%s", raw[:200])
                continue

            await self._dispatch(envelope)

    async def _dispatch(self, envelope: dict[str, Any]) -> None:
        """Parse an envelope and invoke registered callbacks."""
        msg_type: str = envelope.get("type", "")
        msg_body: dict[str, Any] = envelope.get("msg", {})

        if msg_type == "error":
            code    = msg_body.get("code", "")
            message = msg_body.get("message", "")
            logger.error("ws_error  code=%s  message=%s", code, message)
            await self._invoke("error", envelope)
            return

        if msg_type == "subscribed":
            cmd_id = envelope.get("id")
            logger.debug("ws_subscribed  id=%d", cmd_id or 0)
            await self._invoke("subscribed", envelope)
            return

        if msg_type in ("heartbeat", "ping"):
            # Application-level heartbeat from server — no action needed;
            # the protocol-level pong is handled by the websockets library.
            logger.debug("ws_heartbeat")
            return

        # ------ Structured message types --------------------------------

        if msg_type == "orderbook_snapshot":
            structured = OrderbookSnapshot(
                market_ticker=msg_body.get("market_ticker", ""),
                yes=msg_body.get("yes", []),
                no=msg_body.get("no", []),
            )
            await self._invoke("orderbook_snapshot", structured)
            return

        if msg_type == "orderbook_delta":
            structured = OrderbookDelta(
                market_ticker=msg_body.get("market_ticker", ""),
                side=msg_body.get("side", ""),
                price=int(msg_body.get("price", 0)),
                delta=int(msg_body.get("delta", 0)),
                ts=int(msg_body.get("ts", 0)),
            )
            await self._invoke("orderbook_delta", structured)
            return

        if msg_type == "ticker":
            def _int_or_none(v: Any) -> int | None:
                return int(v) if v is not None else None

            structured = TickerUpdate(
                market_ticker=msg_body.get("market_ticker", ""),
                yes_bid=_int_or_none(msg_body.get("yes_bid")),
                yes_ask=_int_or_none(msg_body.get("yes_ask")),
                no_bid=_int_or_none(msg_body.get("no_bid")),
                no_ask=_int_or_none(msg_body.get("no_ask")),
                last_price=_int_or_none(msg_body.get("last_price")),
                volume=_int_or_none(msg_body.get("volume")),
                open_interest=_int_or_none(msg_body.get("open_interest")),
                ts=int(msg_body.get("ts", 0)),
            )
            await self._invoke("ticker", structured)
            return

        if msg_type == "trade":
            structured = TradeUpdate(
                market_ticker=msg_body.get("market_ticker", ""),
                yes_price=int(msg_body.get("yes_price", 0)),
                no_price=int(msg_body.get("no_price", 0)),
                count=int(msg_body.get("count", 0)),
                taker_side=msg_body.get("taker_side", ""),
                ts=int(msg_body.get("ts", 0)),
            )
            await self._invoke("trade", structured)
            return

        # Unknown type — log and pass through raw
        logger.debug("ws_unknown_type  type=%s", msg_type)
        await self._invoke(msg_type, envelope)

    async def _invoke(self, event_type: str, payload: Any) -> None:
        """Call all registered callbacks for event_type sequentially."""
        for cb in self._callbacks.get(event_type, []):
            try:
                await cb(payload)
            except Exception as exc:  # noqa: BLE001
                logger.exception(
                    "ws_callback_error  event=%s  cb=%s  error=%s",
                    event_type, getattr(cb, "__name__", repr(cb)), exc,
                )

    # ------------------------------------------------------------------
    # Internal — heartbeat monitor
    # ------------------------------------------------------------------

    async def _heartbeat_loop(self, ws: websockets.WebSocketClientProtocol) -> None:
        """Application-level liveness monitor.

        If no message has been received for STALE_TIMEOUT_S seconds, close the
        connection so run_forever() triggers a reconnect.
        """
        while True:
            await asyncio.sleep(PING_INTERVAL_S)
            if ws.closed:
                return
            silent_for = time.monotonic() - self._last_msg_ts
            if silent_for > STALE_TIMEOUT_S:
                logger.warning(
                    "ws_stale  silent_for=%.1fs  threshold=%.1fs — forcing reconnect",
                    silent_for, STALE_TIMEOUT_S,
                )
                await ws.close(code=1001, reason="stale — no messages")
                return
            logger.debug("ws_heartbeat_ok  silent_for=%.1fs", silent_for)

    # ------------------------------------------------------------------
    # Internal — auth + crypto
    # ------------------------------------------------------------------

    @staticmethod
    def _load_private_key(path: Path) -> RSAPrivateKey:
        if not path.exists():
            raise FileNotFoundError(
                f"Kalshi private key not found at '{path}'. "
                "Set KALSHI_PRIVATE_KEY_PATH in .env."
            )
        key = serialization.load_pem_private_key(path.read_bytes(), password=None)
        if not isinstance(key, RSAPrivateKey):
            raise TypeError(f"Expected RSA private key, got {type(key).__name__}")
        return key

    def _auth_headers(self) -> dict[str, str]:
        """Generate RSA-PSS signed headers for the WebSocket HTTP handshake.

        The signed message follows the same convention as REST:
            str(timestamp_ms) + "GET" + ws_path
        where ws_path is the URL path component, e.g. "/trade-api/ws/v2".
        """
        from urllib.parse import urlparse
        ws_path = urlparse(self._ws_url).path

        timestamp_ms = str(int(time.time() * 1000))
        message = (timestamp_ms + "GET" + ws_path).encode("utf-8")

        signature_bytes = self._private_key.sign(
            message,
            padding.PSS(
                mgf=padding.MGF1(hashes.SHA256()),
                salt_length=padding.PSS.DIGEST_LENGTH,
            ),
            hashes.SHA256(),
        )

        return {
            "KALSHI-ACCESS-KEY":       self._api_key_id,
            "KALSHI-ACCESS-TIMESTAMP": timestamp_ms,
            "KALSHI-ACCESS-SIGNATURE": base64.b64encode(signature_bytes).decode(),
        }

    # ------------------------------------------------------------------
    # Internal — backoff
    # ------------------------------------------------------------------

    @staticmethod
    def _backoff(attempt: int) -> float:
        """Full-jitter exponential backoff capped at _MAX_BACKOFF_S."""
        base = min(_MAX_BACKOFF_S, 2.0 ** attempt)
        return base + random.uniform(0.0, 1.0)

    # ------------------------------------------------------------------
    # Diagnostics
    # ------------------------------------------------------------------

    @property
    def is_connected(self) -> bool:
        return self._ws is not None and not self._ws.closed

    @property
    def subscription_count(self) -> int:
        return len(self._desired)

    def status(self) -> dict[str, Any]:
        return {
            "connected":       self.is_connected,
            "env":             self._env,
            "subscriptions":   dict(self._desired),
            "reconnects":      self._reconnect_count,
            "last_msg_age_s":  round(time.monotonic() - self._last_msg_ts, 1)
                               if self._last_msg_ts else None,
        }
