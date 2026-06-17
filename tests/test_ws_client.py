"""Unit tests for the native Kalshi WS client message handler.

We don't open a real socket — just call ``_handle_message`` directly
with representative payloads to verify normalization."""

from __future__ import annotations

import asyncio

import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from kalshi_analyzer.auth import KalshiAuth
from kalshi_analyzer.ws_client import KalshiWebSocket, _http_to_ws_base


def make_client(on_update):
    priv = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    auth = KalshiAuth(key_id="k", private_key=priv)
    return KalshiWebSocket(
        rest_base_url="https://api.elections.kalshi.com/trade-api/v2",
        auth=auth,
        on_update=on_update,
    )


def run(coro):
    return asyncio.run(coro)


def test_url_rewrites_rest_base_to_ws_path():
    assert _http_to_ws_base(
        "https://api.elections.kalshi.com/trade-api/v2"
    ) == "wss://api.elections.kalshi.com/trade-api/ws/v2"
    assert _http_to_ws_base(
        "http://localhost:8080/trade-api/v2/"
    ) == "ws://localhost:8080/trade-api/ws/v2"


def test_ticker_v2_message_normalized():
    received: list[dict] = []

    async def handler(msg):
        received.append(msg)

    ws = make_client(handler)

    async def run_test():
        await ws._handle_message(
            {
                "type": "ticker_v2",
                "msg": {
                    "market_ticker": "T1",
                    "yes_bid": 42,
                    "yes_ask": 44,
                    "no_bid": 56,
                    "no_ask": 58,
                    "last_price": 43,
                    "volume_24h": 1234,
                },
            }
        )

    run(run_test())
    assert len(received) == 1
    m = received[0]
    assert m["ticker"] == "T1"
    assert m["yes_bid"] == 42
    assert m["yes_ask"] == 44
    assert m["no_bid"] == 56
    assert m["no_ask"] == 58
    assert m["last_price"] == 43
    assert m["volume_24h"] == 1234
    assert m["_source"] == "ws"


def test_orderbook_snapshot_extracts_best_levels():
    received: list[dict] = []

    async def handler(msg):
        received.append(msg)

    ws = make_client(handler)

    async def run_test():
        await ws._handle_message(
            {
                "type": "orderbook_snapshot",
                "msg": {
                    "market_ticker": "T2",
                    "yes": [["0.40", 100], ["0.42", 50], ["0.39", 75]],
                    "no":  [["0.55", 80], ["0.54", 120]],
                },
            }
        )

    run(run_test())
    m = received[0]
    assert m["yes_bid"] == 42
    assert m["no_bid"] == 55


def test_error_message_does_not_propagate(caplog):
    received: list[dict] = []

    async def handler(msg):
        received.append(msg)

    ws = make_client(handler)

    async def run_test():
        await ws._handle_message({"type": "error", "msg": "bad sub"})

    run(run_test())
    assert not received


def test_ack_message_ignored():
    received: list[dict] = []

    async def handler(msg):
        received.append(msg)

    ws = make_client(handler)

    async def run_test():
        await ws._handle_message({"type": "subscribed", "msg": {"id": 1}})

    run(run_test())
    assert not received
