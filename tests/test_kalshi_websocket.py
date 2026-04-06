"""Tests for KalshiWebSocket — no live network connection required."""
import sys
import types
import asyncio
import base64
from collections import defaultdict
from urllib.parse import urlparse

# --- stub settings so module loads without a .env ---
fake = types.ModuleType("config.settings")
fake.KALSHI_WS_URL            = "wss://demo-api.kalshi.co/trade-api/ws/v2"
fake.KALSHI_API_KEY_ID        = "test-key-id"
fake.KALSHI_PRIVATE_KEY_PATH  = "/nonexistent/key.pem"
fake.KALSHI_ENV               = "demo"
sys.modules["config.settings"] = fake

from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding as cp, rsa

from data.kalshi_websocket import (
    KalshiWebSocket,
    OrderbookDelta,
    OrderbookSnapshot,
    TickerUpdate,
    TradeUpdate,
    CHANNEL_ORDERBOOK,
    CHANNEL_TICKER,
    CHANNEL_TRADE,
    _MAX_BACKOFF_S,
)

_PRIV = rsa.generate_private_key(65537, 2048, default_backend())


def make_client() -> KalshiWebSocket:
    ws = object.__new__(KalshiWebSocket)
    ws._ws_url         = "wss://demo-api.kalshi.co/trade-api/ws/v2"
    ws._api_key_id     = "test-key-id"
    ws._env            = "demo"
    ws._private_key    = _PRIV
    ws._desired        = defaultdict(set)
    ws._callbacks      = defaultdict(list)
    ws._cmd_id         = 0
    ws._ws             = None
    ws._stopped        = False
    ws._last_msg_ts    = 0.0
    ws._reconnect_count = 0
    return ws


# ---------------------------------------------------------------------------

def test_auth_headers():
    client = make_client()
    h = client._auth_headers()

    assert set(h) == {"KALSHI-ACCESS-KEY", "KALSHI-ACCESS-TIMESTAMP", "KALSHI-ACCESS-SIGNATURE"}

    path = urlparse(client._ws_url).path
    assert path == "/trade-api/ws/v2"

    sig = base64.b64decode(h["KALSHI-ACCESS-SIGNATURE"])
    assert len(sig) == 256

    msg = (h["KALSHI-ACCESS-TIMESTAMP"] + "GET" + path).encode()
    _PRIV.public_key().verify(
        sig, msg,
        cp.PSS(mgf=cp.MGF1(hashes.SHA256()), salt_length=cp.PSS.DIGEST_LENGTH),
        hashes.SHA256(),
    )
    print("  auth_headers: RSA-PSS verifies  OK")


def test_backoff_schedule():
    for i in range(7):
        b = KalshiWebSocket._backoff(i)
        expected_min = min(2.0 ** i, _MAX_BACKOFF_S)
        assert b >= expected_min, f"attempt {i}: {b} < {expected_min}"
    assert KalshiWebSocket._backoff(100) <= _MAX_BACKOFF_S + 1.0
    print("  backoff: schedule and cap  OK")


def test_subscribe_unsubscribe():
    client = make_client()
    client.subscribe(["TICK1", "TICK2"], [CHANNEL_ORDERBOOK, CHANNEL_TICKER])
    client.subscribe(["TICK3"], [CHANNEL_TRADE])

    assert client.subscription_count == 3
    assert CHANNEL_ORDERBOOK in client._desired["TICK1"]
    assert CHANNEL_TICKER    in client._desired["TICK2"]
    assert CHANNEL_TRADE     in client._desired["TICK3"]

    client.unsubscribe(["TICK2"])
    assert "TICK2" not in client._desired
    assert client.subscription_count == 2
    print("  subscribe/unsubscribe  OK")


def test_message_parsing():
    client = make_client()
    received: list = []

    async def capture(msg):
        received.append(msg)

    for evt in ("orderbook_snapshot", "orderbook_delta", "ticker", "trade", "subscribed", "error"):
        client.add_callback(evt, capture)

    async def run():
        # orderbook_snapshot
        await client._dispatch({
            "type": "orderbook_snapshot",
            "msg": {"market_ticker": "T1", "yes": [[52, 100], [51, 50]], "no": [[48, 75]]},
        })
        m = received[-1]
        assert isinstance(m, OrderbookSnapshot)
        assert m.yes == [[52, 100], [51, 50]]

        # orderbook_delta
        await client._dispatch({
            "type": "orderbook_delta",
            "msg": {"market_ticker": "T1", "side": "yes", "price": 52, "delta": -10, "ts": 1712000000000},
        })
        m = received[-1]
        assert isinstance(m, OrderbookDelta)
        assert m.price == 52 and m.delta == -10

        # ticker with a None field
        await client._dispatch({
            "type": "ticker",
            "msg": {
                "market_ticker": "T1", "yes_bid": 51, "yes_ask": 53,
                "no_bid": 47, "no_ask": 49, "last_price": 52,
                "volume": 1000, "open_interest": None, "ts": 1712000001000,
            },
        })
        m = received[-1]
        assert isinstance(m, TickerUpdate)
        assert m.yes_bid == 51 and m.open_interest is None

        # trade
        await client._dispatch({
            "type": "trade",
            "msg": {"market_ticker": "T1", "yes_price": 52, "no_price": 48,
                    "count": 25, "taker_side": "yes", "ts": 1712000002000},
        })
        m = received[-1]
        assert isinstance(m, TradeUpdate)
        assert m.count == 25 and m.taker_side == "yes"

        # subscribed ack passes raw envelope
        await client._dispatch({"id": 1, "type": "subscribed", "msg": {}})
        assert received[-1] == {"id": 1, "type": "subscribed", "msg": {}}

        # error
        await client._dispatch({"type": "error", "msg": {"code": "UNAUTH", "message": "bad"}})
        assert received[-1]["type"] == "error"

        # heartbeat — no callback registered; must not raise
        await client._dispatch({"type": "heartbeat", "msg": {}})

        print("  message_parsing: all types  OK")

    asyncio.run(run())


def test_callback_exception_isolation():
    client = make_client()
    log: list = []

    async def bad_cb(msg):
        raise RuntimeError("intentional")

    async def good_cb(msg):
        log.append(msg)

    client.add_callback("ticker", bad_cb)
    client.add_callback("ticker", good_cb)

    async def run():
        await client._dispatch({
            "type": "ticker",
            "msg": {
                "market_ticker": "T", "yes_bid": 50, "yes_ask": 52,
                "no_bid": 48, "no_ask": 50, "last_price": 51,
                "volume": 100, "open_interest": None, "ts": 0,
            },
        })
        assert len(log) == 1, "good_cb should run even after bad_cb raises"
        print("  callback_isolation: good callback ran after bad raised  OK")

    asyncio.run(run())


def test_on_decorator():
    client = make_client()
    log: list = []

    @client.on("trade")
    async def my_handler(msg):
        log.append(msg)

    async def run():
        await client._dispatch({
            "type": "trade",
            "msg": {"market_ticker": "T", "yes_price": 55, "no_price": 45,
                    "count": 5, "taker_side": "no", "ts": 0},
        })
        assert len(log) == 1 and isinstance(log[0], TradeUpdate)
        assert log[0].taker_side == "no"
        print("  on() decorator  OK")

    asyncio.run(run())


def test_status():
    client = make_client()
    client.subscribe(["T1", "T2"], [CHANNEL_ORDERBOOK])
    s = client.status()
    assert s["connected"] is False
    assert "T1" in s["subscriptions"] and "T2" in s["subscriptions"]
    assert s["reconnects"] == 0
    assert s["last_msg_age_s"] is None
    print(f"  status: {s}")
    print("  status()  OK")


# ---------------------------------------------------------------------------

if __name__ == "__main__":
    print("=== Auth headers ===")
    test_auth_headers()
    print("=== Backoff schedule ===")
    test_backoff_schedule()
    print("=== Subscribe/unsubscribe ===")
    test_subscribe_unsubscribe()
    print("=== Message parsing ===")
    test_message_parsing()
    print("=== Callback exception isolation ===")
    test_callback_exception_isolation()
    print("=== on() decorator ===")
    test_on_decorator()
    print("=== status() ===")
    test_status()
    print()
    print("All tests passed.")
