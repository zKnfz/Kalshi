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
    liquidity_confidence,
    size_position,
)
from kalshi_analyzer.config import settings
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


def test_kelly_matches_alternate_b_q_form_when_q_is_fair_estimate():
    """The closed form (q - p) / (1 - p) and the explicit b*q form

        b = (1 - p) / p
        f* = (b*q - (1 - q)) / b

    must be algebraically equivalent for any p, q in (0, 1). q here is
    the *fair* probability estimate, not 1 - p. This guards against a
    refactor that confuses q with 1 - p (which would give wrong answers
    at zero edge and elsewhere).
    """

    cases = [
        (0.40, 0.60),
        (0.30, 0.50),
        (0.10, 0.40),
        (0.55, 0.60),
        (0.70, 0.85),
    ]
    for p, q in cases:
        b = (1 - p) / p
        explicit = (b * q - (1 - q)) / b
        closed_form = kelly_fraction_for_yes(p, q)
        assert math.isclose(closed_form, max(0.0, min(1.0, explicit)), abs_tol=1e-9), (
            p,
            q,
            explicit,
            closed_form,
        )


def test_kelly_matches_user_supplied_example():
    # User example: YES at $0.40 with fair = 0.60 -> Kelly = 1/3.
    f = kelly_fraction_for_yes(0.40, 0.60)
    assert math.isclose(f, 1 / 3, abs_tol=1e-6)


def test_size_position_caps_at_max_bet_pct():
    raw_kelly = 0.95
    scaled, stake = size_position("fair_value_yes", raw_kelly)
    assert scaled <= settings.max_bet_pct + 1e-9
    assert stake <= settings.bankroll * settings.max_bet_pct + 1e-9


def test_size_position_caps_at_per_strategy_share():
    huge_kelly = 1.0
    _, fv_stake = size_position("fair_value_yes", huge_kelly)
    _, arb_stake = size_position("dutch_book_arbitrage", huge_kelly)
    assert fv_stake <= settings.bankroll * settings.fairvalue_bankroll_share + 1e-9
    assert arb_stake <= settings.bankroll * settings.arb_bankroll_share + 1e-9
    assert arb_stake >= fv_stake


def test_max_spread_filter_rejects_wide_market():
    wide = mk(yes_bid=20, yes_ask=20 + settings.max_spread_cents + 5)
    assert not is_tradable(wide)


def test_min_volume_threshold_filter(monkeypatch):
    monkeypatch.setattr(settings, "min_volume_24h", 100, raising=False)
    quiet = Market(
        ticker="q",
        event_ticker="e",
        title="t",
        yes_bid=40,
        yes_ask=42,
        liquidity=50_000,
        volume_24h=10,
        open_interest=10,
    )
    assert not is_tradable(quiet)


def test_stale_last_reduces_confidence():
    """A market with a very old last-trade should have lower confidence
    than the same market with a fresh print (all else equal)."""

    fresh = mk(yes_bid=58, yes_ask=60, last=70)
    fresh.last_trade_age_seconds = 5
    stale = mk(yes_bid=58, yes_ask=60, last=70)
    stale.last_trade_age_seconds = 60 * 30
    assert liquidity_confidence(stale) < liquidity_confidence(fresh)


def test_stale_last_reduces_fairvalue_blend_weight():
    """When the last trade is very stale, its weight in the consensus
    fair-value blend should drop materially relative to a fresh print."""

    fresh = mk(yes_bid=58, yes_ask=60, last=80)
    fresh.last_trade_age_seconds = 5
    stale = mk(yes_bid=58, yes_ask=60, last=80)
    stale.last_trade_age_seconds = 60 * 30

    fresh_fair = consensus_fair_price(fresh)
    stale_fair = consensus_fair_price(stale)
    mid = (58 + 60) / 200.0
    assert fresh_fair is not None and stale_fair is not None
    assert abs(stale_fair - mid) < abs(fresh_fair - mid)


def test_evaluate_markets_dedupes_and_merges_signal_types():
    """A market that lights up under both fair_value_yes and
    dutch_book_arbitrage should produce a single (ticker, YES) row whose
    signal_types lists both."""

    event = Event(
        event_ticker="DUP",
        title="dup",
        mutually_exclusive=True,
        markets=[
            mk("A", yes_bid=18, yes_ask=20, last=40),
            mk("B", yes_bid=28, yes_ask=30),
            mk("C", yes_bid=38, yes_ask=40),
        ],
    )
    ops = evaluate_markets([event])
    a_row = next((o for o in ops if o.ticker == "A" and o.side == "YES"), None)
    assert a_row is not None
    assert "dutch_book_arbitrage" in a_row.signal_types
    assert "fair_value_yes" in a_row.signal_types
    keys = [(o.ticker, o.side) for o in ops]
    assert len(keys) == len(set(keys))


def test_demo_mode_fires_all_signal_types():
    from kalshi_analyzer.engine import _build_demo_events

    ops = evaluate_markets(_build_demo_events())
    fired = set()
    for o in ops:
        fired.update(o.signal_types)
    for expected in {
        "yes_no_arbitrage",
        "dutch_book_arbitrage",
        "dutch_book_mispricing",
        "fair_value_yes",
        "fair_value_no",
    }:
        assert expected in fired, f"DEMO_MODE should fire {expected}; fired={fired}"


def test_engine_tick_emits_delta_and_first_seen(monkeypatch):
    """Drive AnalyzerEngine through three simulated ticks and verify that:

      * the first tick populates added[] and leaves updated/removed empty
      * an identical second tick produces an empty diff (heartbeat)
      * a third tick with a mutated price emits updated[] or added[]
      * first_seen stays constant across ticks for an unchanged op
    """

    import asyncio
    from kalshi_analyzer.engine import AnalyzerEngine, _build_demo_events
    from kalshi_analyzer import config as cfg_module

    monkeypatch.setattr(cfg_module.settings, "demo_mode", False, raising=False)

    static_events = _build_demo_events()

    async def _stub_load():
        return static_events

    engine = AnalyzerEngine()
    engine._load_events_from_kalshi = _stub_load  # type: ignore[assignment]

    async def run() -> tuple[dict, dict, dict, dict, dict, dict]:
        s1, d1 = await engine._tick()
        s2, d2 = await engine._tick()

        if static_events and static_events[0].markets:
            target = static_events[0].markets[0]
            target.yes_ask = max(2, (target.yes_ask or 50) - 5)
            target.yes_bid = max(1, (target.yes_bid or 40) - 5)

        s3, d3 = await engine._tick()
        return s1, d1, s2, d2, s3, d3

    s1, d1, s2, d2, s3, d3 = asyncio.run(run())

    assert d1["added"], "first tick should add opportunities"
    assert not d1["updated"]
    assert not d1["removed"]

    assert not d2["added"]
    assert not d2["removed"]

    sample_op = s2["opportunities"][0]
    first_seen_before = sample_op["first_seen"]

    assert d3["added"] or d3["updated"]

    same_key = next(
        (
            o
            for o in s3["opportunities"]
            if o["ticker"] == sample_op["ticker"]
            and o["side"] == sample_op["side"]
        ),
        None,
    )
    if same_key is not None:
        assert same_key["first_seen"] == first_seen_before


if __name__ == "__main__":
    pytest.main([__file__, "-q"])
