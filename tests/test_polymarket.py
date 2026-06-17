"""Tests for Polymarket parsing, matching, and cross-platform arb."""

from __future__ import annotations

import json

import pytest

from kalshi_analyzer.models import Market
from kalshi_analyzer.polymarket import (
    PolymarketMarket,
    find_cross_arbs,
    match_kalshi_to_polymarket,
    title_similarity,
)


def make_pm(
    question: str,
    yes_price: float,
    *,
    slug: str = "",
    condition_id: str = "",
    best_ask: float | None = None,
    best_bid: float | None = None,
    active: bool = True,
    closed: bool = False,
) -> PolymarketMarket:
    return PolymarketMarket.from_api(
        {
            "id": condition_id or "cond-1",
            "conditionId": condition_id or "cond-1",
            "slug": slug or "slug-1",
            "question": question,
            "outcomes": json.dumps(["Yes", "No"]),
            "outcomePrices": json.dumps([f"{yes_price}", f"{1 - yes_price}"]),
            "bestAsk": best_ask if best_ask is not None else yes_price,
            "bestBid": best_bid if best_bid is not None else yes_price - 0.01,
            "volume24hr": 5000,
            "liquidityNum": 20000,
            "active": active,
            "closed": closed,
            "acceptingOrders": True,
        }
    )


def make_km(
    ticker: str, title: str, yes_bid: int, yes_ask: int, no_ask: int | None = None
) -> Market:
    return Market(
        ticker=ticker,
        event_ticker="EV",
        title=title,
        yes_bid=yes_bid,
        yes_ask=yes_ask,
        no_bid=100 - yes_ask - 1,
        no_ask=no_ask if no_ask is not None else 100 - yes_bid + 1,
        last_price=(yes_bid + yes_ask) // 2,
        liquidity=50000,
        volume_24h=2000,
    )


def test_polymarket_parses_stringified_arrays():
    pm = make_pm("Will X happen?", 0.62)
    assert pm.outcomes == ["Yes", "No"]
    assert pm.outcome_prices == [0.62, 0.38]
    assert pm.yes_price == 0.62
    assert pm.no_price == 0.38


def test_polymarket_yes_no_lookup_by_name_not_index():
    raw = {
        "id": "x",
        "slug": "x",
        "question": "x?",
        "outcomes": json.dumps(["No", "Yes"]),
        "outcomePrices": json.dumps(["0.30", "0.70"]),
        "bestAsk": "0.71",
        "bestBid": "0.69",
        "active": True,
        "closed": False,
    }
    pm = PolymarketMarket.from_api(raw)
    assert pm.yes_price == 0.70
    assert pm.no_price == 0.30


def test_title_similarity_finds_obvious_matches():
    assert title_similarity(
        "Fed cuts rates in June 2026",
        "Will the Fed cut rates in June 2026?",
    ) > 0.5
    assert title_similarity("Lakers win NBA championship", "Mets win World Series") < 0.4


def test_manual_map_overrides_title_match():
    km = make_km("KX-FOO", "Lakers win the championship", 40, 45)
    pm_a = make_pm("Bulls win the championship", 0.50, slug="bulls-win")
    pm_b = make_pm("Lakers win NBA championship", 0.50, slug="lakers-win")
    manual = {"KX-FOO": "bulls-win"}
    matched = match_kalshi_to_polymarket(km, [pm_a, pm_b], manual_map=manual)
    assert matched is pm_a


def test_title_match_picks_best_score():
    km = make_km("KX-FED", "Fed cuts rates 25bps in June", 40, 45)
    pm_a = make_pm("Fed cuts rates 25bps in June 2026", 0.45)
    pm_b = make_pm("Bitcoin above $200k by EOY", 0.20)
    matched = match_kalshi_to_polymarket(km, [pm_a, pm_b])
    assert matched is pm_a


def test_no_match_when_similarity_below_threshold():
    km = make_km("KX-NBA", "Lakers beat Celtics", 55, 58)
    pm = make_pm("Crypto bull market in 2027", 0.30)
    assert match_kalshi_to_polymarket(km, [pm]) is None


def test_cross_arb_detected_when_sum_below_one():
    """Kalshi YES at 30¢ + Polymarket NO at 60¢ = 90¢ < $1 → arb."""

    km = make_km("KX-X", "Will X happen by year-end?", 28, 30, no_ask=70)
    pm = make_pm("Will X happen by year-end?", 0.40)
    arbs = find_cross_arbs([km], [pm], min_net_edge_pct=0.5)
    assert any(o.strategy == "cross_platform_arbitrage" for o in arbs)
    arb = arbs[0]
    assert arb.net_edge > 0
    assert "Kalshi" in arb.rationale and "Polymarket" in arb.rationale
    assert arb.extra["polymarket_slug"] == "slug-1"


def test_cross_arb_not_detected_when_sum_above_one():
    """Both legs must clear $1 + buffer. With Kalshi 55-58 (NO ask=46)
    and Polymarket YES 0.55 (NO ≈ 0.45): YES+NO = 0.58 + 0.45 = 1.03 and
    NO+YES = 0.46 + 0.55 = 1.01 — both above 1.005, no arb."""

    km = make_km("KX-Y", "Q?", 55, 58)
    pm = make_pm("Q?", 0.55)
    arbs = find_cross_arbs([km], [pm])
    assert not arbs


def test_cross_arb_rejects_when_fees_eat_edge():
    km = make_km("KX-T", "Will it rain tomorrow?", 47, 49)
    pm = make_pm("Will it rain tomorrow?", 0.50, best_ask=0.50)
    arbs = find_cross_arbs([km], [pm], min_net_edge_pct=5.0)
    assert not arbs
