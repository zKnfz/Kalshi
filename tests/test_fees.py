"""Tests for the Kalshi fee model."""

from __future__ import annotations

import math

from kalshi_analyzer.fees import (
    DEFAULT_TAKER_COEFFICIENT,
    SP_NASDAQ_TAKER_COEFFICIENT,
    basket_fee,
    fee_coefficient,
    is_sp_or_nasdaq_ticker,
    net_edge_per_contract,
    per_contract_fee,
    quote_fee,
    total_fee,
)


def test_taker_fee_matches_official_schedule_50c():
    """At P=$0.50 the taker fee is the parabola's maximum.

    0.07 * 0.50 * 0.50 = 0.0175 → ceil to next cent = $0.02 per contract.
    The published Kalshi schedule lists exactly $0.02/contract at $0.50.
    """

    assert per_contract_fee(0.50) == 0.02


def test_taker_fee_at_extremes_is_one_cent_or_zero():
    """Edges of the parabola: at very low/high P the fee rounds up to 1¢
    (or to 0¢ outside (0, 1))."""

    assert per_contract_fee(0.01) == 0.01
    assert per_contract_fee(0.99) == 0.01
    assert per_contract_fee(0.0) == 0.0
    assert per_contract_fee(1.0) == 0.0


def test_maker_fee_is_lower_than_taker():
    """Maker rate (0.0175) ≤ taker rate (0.07) so maker fee ≤ taker fee.

    Note: rounding-up means some prices where the raw maker fee is in
    (0.00, 0.01] and the raw taker fee is in (0.01, 0.02] both ceil to
    0.01, so equality is allowed."""

    for p in (0.10, 0.25, 0.40, 0.55, 0.70, 0.85, 0.95):
        assert per_contract_fee(p, is_taker=False) <= per_contract_fee(p, is_taker=True)


def test_index_market_uses_halved_coefficient():
    assert is_sp_or_nasdaq_ticker("INXD-26JUN17")
    assert is_sp_or_nasdaq_ticker("NASDAQ100M-26JUN")
    assert not is_sp_or_nasdaq_ticker("KXNBA-LAL-W")
    assert (
        fee_coefficient("INXD", is_taker=True) == SP_NASDAQ_TAKER_COEFFICIENT
    )
    assert (
        fee_coefficient("KXNBA-LAL", is_taker=True) == DEFAULT_TAKER_COEFFICIENT
    )


def test_quote_fee_uses_aggregate_ceil():
    """Per Kalshi's formula the ceil is applied to the whole order
    (`ceil(coef * C * P * (1-P))`), so the effective per-contract fee
    decreases with order size (the rounding-up overhead amortizes)."""

    q1 = quote_fee(ticker="X", price=0.50, contracts=1, is_taker=True)
    q100 = quote_fee(ticker="X", price=0.50, contracts=100, is_taker=True)
    assert q1.per_contract == 0.02
    assert q100.total == 1.75
    assert q100.per_contract < q1.per_contract


def test_net_edge_per_contract_subtracts_taker_fee():
    p, q = 0.40, 0.60
    fee = per_contract_fee(p)
    net = net_edge_per_contract(entry_price=p, fair_price=q)
    assert math.isclose(net, (q - p) - fee, abs_tol=1e-9)


def test_basket_fee_sum():
    legs = [("A", 0.10, 100), ("B", 0.20, 100), ("C", 0.30, 100)]
    total = basket_fee(legs)
    expected = sum(
        total_fee(price=p, contracts=c, ticker=t) for t, p, c in legs
    )
    assert math.isclose(total, expected, abs_tol=1e-9)


def test_published_schedule_100_contracts_at_50c_is_175():
    """Schedule line: 100 contracts at $0.50 = $1.75 in fees."""

    assert math.isclose(
        quote_fee(ticker="X", price=0.50, contracts=100, is_taker=True).total,
        1.75,
        abs_tol=1e-9,
    )
