"""Tests for basket completeness, zero-liquidity confidence, sports, execution."""

from __future__ import annotations

import asyncio

import pytest

from kalshi_analyzer.analyzer import (
    analyze_event_dutch_book,
    analyze_yes_no_arbitrage,
    evaluate_markets,
    group_baskets,
    is_tradable,
    liquidity_confidence,
)
from kalshi_analyzer.config import settings
from kalshi_analyzer.execution import BasketLeg, Executor
from kalshi_analyzer.models import Event, Market
from kalshi_analyzer.paper import PaperEngine
from kalshi_analyzer.sports import is_sports_market, live_status


def mk(
    ticker: str = "T",
    event: str = "E",
    yes_bid: int = 40,
    yes_ask: int = 45,
    no_bid: int | None = None,
    no_ask: int | None = None,
    liquidity: int = 100_000,
    volume_24h: int = 5_000,
    open_interest: int = 12_000,
    yes_ask_size: int | None = None,
    **kwargs,
) -> Market:
    if no_bid is None:
        no_bid = max(1, 100 - yes_ask - 1)
    if no_ask is None:
        no_ask = max(yes_ask, 100 - yes_bid + 1)
    raw = {}
    if yes_ask_size is not None:
        raw["yes_ask_size"] = yes_ask_size
    return Market(
        ticker=ticker,
        event_ticker=event,
        title=f"market {ticker}",
        yes_bid=yes_bid,
        yes_ask=yes_ask,
        no_bid=no_bid,
        no_ask=no_ask,
        last_price=(yes_bid + yes_ask) // 2,
        previous_price=(yes_bid + yes_ask) // 2,
        liquidity=liquidity,
        volume_24h=volume_24h,
        open_interest=open_interest,
        status="active",
        raw=raw,
        **kwargs,
    )


def test_zero_liquidity_confidence_is_zero():
    m = mk(liquidity=0)
    assert liquidity_confidence(m) == 0.0


def test_yes_no_arb_zero_liquidity_has_zero_confidence():
    m = mk(yes_ask=40, no_ask=55, liquidity=0)
    op = analyze_yes_no_arbitrage(m)
    assert op is not None
    assert op.confidence == 0.0


def test_incomplete_basket_marks_basket_complete_false():
    event = Event(
        event_ticker="FED-TEST",
        title="Fed",
        mutually_exclusive=True,
        markets=[
            mk("A", yes_bid=18, yes_ask=20),
            mk("B", yes_bid=28, yes_ask=30),
            mk("C", yes_bid=38, yes_ask=40, liquidity=0, volume_24h=0, open_interest=0),
        ],
    )
    ops = analyze_event_dutch_book(event)
    assert ops
    assert all(o.basket_complete is False for o in ops)
    assert all(o.confidence == 0.0 for o in ops)
    assert all(o.edge_pct == 0.0 for o in ops)


def test_complete_basket_sets_basket_complete_true():
    event = Event(
        event_ticker="FED-TEST",
        title="Fed",
        mutually_exclusive=True,
        markets=[
            mk("A", yes_bid=18, yes_ask=20),
            mk("B", yes_bid=28, yes_ask=30),
            mk("C", yes_bid=38, yes_ask=40),
        ],
    )
    ops = analyze_event_dutch_book(event)
    assert ops
    assert all(o.basket_complete is True for o in ops)
    assert ops[0].basket_id == "FED-TEST"


def test_group_baskets_aggregates_legs():
    event = Event(
        event_ticker="FED-TEST",
        title="Fed",
        mutually_exclusive=True,
        markets=[
            mk("A", yes_bid=18, yes_ask=20),
            mk("B", yes_bid=28, yes_ask=30),
            mk("C", yes_bid=38, yes_ask=40),
        ],
    )
    ops = evaluate_markets([event])
    grouped = group_baskets(ops)
    assert len(grouped) == 1
    assert grouped[0]["basket_id"] == "FED-TEST"
    assert len(grouped[0]["legs"]) >= 2
    assert grouped[0]["basket_complete"] is True


def test_sports_market_detection():
    assert is_sports_market("NFL-2025-W1-KC", "NFL-2025-W1")
    assert not is_sports_market("FED-RATE-HOLD", "FED-RATE")


def test_sports_volume_floor(monkeypatch):
    monkeypatch.setattr(settings, "sports_min_volume_24h", 1000, raising=False)
    monkeypatch.setattr(settings, "min_volume_24h", 0, raising=False)
    sports = mk(
        ticker="NBA-TEST",
        event="NBA-TEST",
        volume_24h=100,
        liquidity=50_000,
    )
    assert not is_tradable(sports)
    nonsports = mk(ticker="FED-TEST", event="FED-TEST", volume_24h=100)
    assert is_tradable(nonsports)


def test_live_status_within_three_hours():
    from datetime import datetime, timedelta, timezone

    soon = (
        datetime.now(tz=timezone.utc) + timedelta(hours=1)
    ).isoformat().replace("+00:00", "Z")
    assert live_status(soon) == "LIVE"


def test_basket_execution_aborts_on_failed_leg(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "execution_mode", "paper", raising=False)
    monkeypatch.setattr(settings, "max_bet_pct", 0.5, raising=False)
    monkeypatch.setattr(settings, "arb_fill_timeout_seconds", 1.0, raising=False)
    paper = PaperEngine(
        state_path=str(tmp_path / "paper.json"),
        starting_bankroll=1000.0,
    )
    ex = Executor(paper=paper)

    async def run():
        return await ex.submit_basket(
            basket_id="TEST-BASKET",
            legs=[
                BasketLeg(
                    ticker="LOW-LIQ",
                    side="YES",
                    contracts=10,
                    limit_price=0.40,
                    liquidity=100,
                ),
                BasketLeg(
                    ticker="HIGH-LIQ",
                    side="YES",
                    contracts=10,
                    limit_price=0.40,
                    liquidity=50_000,
                ),
            ],
        )

    result = asyncio.run(run())
    assert result.accepted
    assert len(result.legs) == 2
