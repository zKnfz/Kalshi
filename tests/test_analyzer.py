"""Smoke / property tests for the analyzer math.

Run with:  python -m pytest -q tests
"""

from __future__ import annotations

import math

import pytest

from kalshi_analyzer.analyzer import (
    analyze_event_dutch_book,
    analyze_market_fair_value,
    analyze_yes_no_arbitrage,
    consensus_fair_price,
    evaluate_markets,
    is_tradable,
    kelly_fraction_for_yes,
)
from kalshi_analyzer.models import Event, Market


def mk(
    ticker: str = "T",
    event: str = "E",
    yes_bid: int = 40,
    yes_ask: int = 45,
    no_bid: int | None = None,
    no_ask: int | None = None,
    liquidity: int = 100_000,
    volume_24h: int = 5_000,
    last: int | None = None,
    status: str = "active",
) -> Market:
    if no_bid is None:
        no_bid = 100 - yes_ask - 1
    if no_ask is None:
        no_ask = 100 - yes_bid + 1
    return Market(
        ticker=ticker,
        event_ticker=event,
        title=f"market {ticker}",
        yes_bid=yes_bid,
        yes_ask=yes_ask,
        no_bid=no_bid,
        no_ask=min(99, max(1, no_ask)),
        last_price=last if last is not None else (yes_bid + yes_ask) // 2,
        previous_price=(yes_bid + yes_ask) // 2,
        liquidity=liquidity,
        volume_24h=volume_24h,
        status=status,
    )


def test_kelly_no_edge_is_zero():
    assert kelly_fraction_for_yes(0.5, 0.5) == 0.0


def test_kelly_positive_when_fair_above_price():
    f = kelly_fraction_for_yes(0.40, 0.55)
    assert 0 < f <= 1


def test_kelly_zero_when_fair_below_price():
    assert kelly_fraction_for_yes(0.6, 0.4) == 0.0


def test_yes_no_arb_detected():
    m = mk(yes_ask=40, no_ask=55)
    op = analyze_yes_no_arbitrage(m)
    assert op is not None
    assert op.strategy == "yes_no_arbitrage"
    assert math.isclose(op.edge, 1 - 0.95, abs_tol=1e-9)


def test_yes_no_arb_not_detected_when_no_gap():
    m = mk(yes_ask=50, no_ask=50)
    assert analyze_yes_no_arbitrage(m) is None


def test_fair_value_finds_yes_when_ask_below_blend():
    m = mk(yes_bid=58, yes_ask=60, last=70)
    ops = analyze_market_fair_value(m)
    assert any(o.side == "YES" and o.entry_price < o.fair_price for o in ops)


def test_dutch_book_arbitrage_detected():
    e = Event(
        event_ticker="EV",
        title="event",
        mutually_exclusive=True,
        markets=[
            mk("A", yes_bid=18, yes_ask=20),
            mk("B", yes_bid=28, yes_ask=30),
            mk("C", yes_bid=38, yes_ask=40),
        ],
    )
    ops = analyze_event_dutch_book(e)
    assert ops, "expected arbitrage opportunities"
    assert all(o.strategy == "dutch_book_arbitrage" for o in ops)


def test_is_tradable_filters_status_and_liquidity():
    assert not is_tradable(mk(status="closed"))
    illiquid = Market(
        ticker="x",
        event_ticker="e",
        title="t",
        yes_bid=10,
        yes_ask=12,
        liquidity=0,
        volume_24h=0,
        open_interest=0,
    )
    assert not is_tradable(illiquid)


def test_consensus_returns_none_for_empty_market():
    m = Market(ticker="x", event_ticker="e", title="t")
    assert consensus_fair_price(m) is None


def test_evaluate_markets_orders_by_score():
    arb_event = Event(
        event_ticker="ARB",
        title="arb",
        mutually_exclusive=True,
        markets=[
            mk("A", yes_bid=10, yes_ask=12),
            mk("B", yes_bid=20, yes_ask=22),
            mk("C", yes_bid=30, yes_ask=32),
        ],
    )
    plain = Event(
        event_ticker="P",
        title="plain",
        markets=[mk("P1", yes_bid=55, yes_ask=58, last=65)],
    )
    ops = evaluate_markets([arb_event, plain])
    assert ops
    scores = [o.score for o in ops]
    assert scores == sorted(scores, reverse=True)
    assert any(o.strategy == "dutch_book_arbitrage" for o in ops)


def test_market_parser_handles_dollar_string_format():
    raw = {
        "ticker": "T1",
        "event_ticker": "E1",
        "title": "test",
        "status": "active",
        "yes_bid_dollars": "0.4200",
        "yes_ask_dollars": "0.4500",
        "no_bid_dollars": "0.5400",
        "no_ask_dollars": "0.5700",
        "last_price_dollars": "0.4400",
        "previous_price_dollars": "0.4300",
        "liquidity_dollars": "125.5000",
        "open_interest_fp": "1500",
        "volume_24h": 800,
    }
    m = Market.from_api(raw)
    assert m.yes_bid == 42
    assert m.yes_ask == 45
    assert m.no_bid == 54
    assert m.no_ask == 57
    assert m.last_price == 44
    assert m.liquidity == 12550
    assert m.open_interest == 1500
    assert m.volume_24h == 800


def test_market_parser_handles_legacy_int_format():
    raw = {
        "ticker": "T2",
        "event_ticker": "E2",
        "title": "legacy",
        "yes_bid": 18,
        "yes_ask": 20,
        "no_bid": 79,
        "no_ask": 82,
        "last_price": 19,
        "liquidity": 4500,
        "open_interest": 200,
        "volume_24h": 50,
    }
    m = Market.from_api(raw)
    assert (m.yes_bid, m.yes_ask, m.no_bid, m.no_ask) == (18, 20, 79, 82)
    assert m.liquidity == 4500
    assert m.volume_24h == 50


if __name__ == "__main__":
    pytest.main([__file__, "-q"])
