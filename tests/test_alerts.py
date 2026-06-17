"""Alert dispatcher unit tests."""

from __future__ import annotations

import asyncio
import time
from typing import Any

import pytest

from kalshi_analyzer.alerts import (
    AlertDispatcher,
    _embed_for_discord,
    _format_opportunity_text,
)
from kalshi_analyzer.config import settings


def run(coro):
    return asyncio.run(coro)


def make_op(net_edge_pct: float = 8.0, **extra: Any) -> dict[str, Any]:
    return {
        "title": "Will the Fed cut rates in June?",
        "ticker": "KXFED-26JUN-CUT25",
        "side": "YES",
        "strategy": "fair_value_yes",
        "signal_types": ["fair_value_yes"],
        "entry_price": 0.40,
        "fair_price": 0.55,
        "edge_pct": 37.5,
        "net_edge_pct": net_edge_pct,
        "fees_per_contract": 0.0168,
        "kelly_fraction": 0.05,
        "suggested_stake": 25.0,
        "generated_at": "2026-06-17T12:00:00Z",
        **extra,
    }


def make_dispatcher(monkeypatch, **overrides):
    for k, v in overrides.items():
        monkeypatch.setattr(settings, k, v, raising=False)
    return AlertDispatcher.from_settings()


def test_disabled_when_no_credentials(monkeypatch):
    d = make_dispatcher(
        monkeypatch,
        telegram_bot_token="",
        telegram_chat_id="",
        discord_webhook_url="",
    )
    assert not d.enabled
    n = run(d.dispatch({"opportunities": [make_op()]}))
    assert n == 0
    run(d.close())


def test_enabled_with_telegram(monkeypatch):
    d = make_dispatcher(
        monkeypatch,
        telegram_bot_token="tg-token",
        telegram_chat_id="123",
        discord_webhook_url="",
    )
    assert d.enabled
    run(d.close())


def test_min_edge_filter(monkeypatch):
    d = make_dispatcher(
        monkeypatch,
        telegram_bot_token="tok",
        telegram_chat_id="c",
        discord_webhook_url="",
        alert_min_edge_pct=10.0,
        alert_cooldown_seconds=0,
    )

    sent: list[dict] = []

    async def fake_send(self, op):
        sent.append(op)

    monkeypatch.setattr(AlertDispatcher, "_send_telegram", fake_send)
    monkeypatch.setattr(AlertDispatcher, "_send_discord", fake_send)

    low = make_op(net_edge_pct=5.0, ticker="LOW")
    high = make_op(net_edge_pct=12.0, ticker="HIGH")
    run(d.dispatch({"opportunities": [low, high]}))
    assert [o["ticker"] for o in sent] == ["HIGH"]
    run(d.close())


def test_cooldown_blocks_repeat(monkeypatch):
    d = make_dispatcher(
        monkeypatch,
        telegram_bot_token="tok",
        telegram_chat_id="c",
        discord_webhook_url="",
        alert_min_edge_pct=2.0,
        alert_cooldown_seconds=300,
    )
    sent: list[dict] = []

    async def fake_send(self, op):
        sent.append(op)

    monkeypatch.setattr(AlertDispatcher, "_send_telegram", fake_send)

    op = make_op()
    run(d.dispatch({"opportunities": [op]}))
    run(d.dispatch({"opportunities": [op]}))
    assert len(sent) == 1
    run(d.close())


def test_cooldown_clears_after_window(monkeypatch):
    d = make_dispatcher(
        monkeypatch,
        telegram_bot_token="tok",
        telegram_chat_id="c",
        discord_webhook_url="",
        alert_min_edge_pct=2.0,
        alert_cooldown_seconds=0.01,
    )
    sent: list[dict] = []

    async def fake_send(self, op):
        sent.append(op)

    monkeypatch.setattr(AlertDispatcher, "_send_telegram", fake_send)

    op = make_op()
    run(d.dispatch({"opportunities": [op]}))
    time.sleep(0.02)
    run(d.dispatch({"opportunities": [op]}))
    assert len(sent) == 2
    run(d.close())


def test_telegram_message_includes_key_fields():
    text = _format_opportunity_text(make_op())
    assert "KXFED" in text
    assert "fair_value_yes" in text
    assert "edge: 37.50%" in text
    assert "Kelly" in text


def test_discord_embed_structure():
    embed = _embed_for_discord(make_op())
    assert embed["embeds"][0]["title"]
    assert embed["embeds"][0]["color"]
    assert "kalshi.com" in embed["embeds"][0]["url"]
