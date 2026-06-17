"""Kalshi fee model.

Source: the official Kalshi fee schedule
(https://kalshi.com/docs/kalshi-fee-schedule.pdf) and the parallel CFTC
filing. Trading fees are charged only when an order is **executed**;
resting orders that get cancelled cost nothing.

Per-contract fee, in dollars, rounded **up** to the next cent::

    taker_fee(P) = ceil_cents( 0.07   * P * (1 - P) )
    maker_fee(P) = ceil_cents( 0.0175 * P * (1 - P) )

S&P 500 and Nasdaq-100 contracts use a halved coefficient (0.035 for
takers, ~0.00875 for makers); detected via the ``INX`` / ``NASDAQ100``
ticker prefix.

The fee curve is parabolic: ~$0.0175/contract at the worst (P=$0.50),
and a few hundredths of a cent at P→0 or P→1.

This module is pure-Python and side-effect free so the analyzer and the
backtest module can both depend on it.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Iterable


DEFAULT_TAKER_COEFFICIENT = 0.07
DEFAULT_MAKER_COEFFICIENT = 0.0175
SP_NASDAQ_TAKER_COEFFICIENT = 0.035
SP_NASDAQ_MAKER_COEFFICIENT = 0.00875


def _ceil_cents(amount_dollars: float) -> float:
    """Round a dollar amount **up** to the nearest cent."""

    if amount_dollars <= 0:
        return 0.0
    return math.ceil(amount_dollars * 100 - 1e-9) / 100.0


def is_sp_or_nasdaq_ticker(ticker: str) -> bool:
    if not ticker:
        return False
    t = ticker.upper()
    return t.startswith("INX") or t.startswith("NASDAQ100")


@dataclass(frozen=True)
class FeeQuote:
    """Per-contract and per-trade fee breakdown."""

    ticker: str
    price: float
    contracts: int
    side: str
    is_taker: bool
    coefficient: float
    per_contract: float
    total: float

    def to_dict(self) -> dict:
        return {
            "ticker": self.ticker,
            "price": round(self.price, 4),
            "contracts": self.contracts,
            "side": self.side,
            "is_taker": self.is_taker,
            "coefficient": self.coefficient,
            "per_contract": round(self.per_contract, 4),
            "total": round(self.total, 4),
        }


def fee_coefficient(ticker: str, is_taker: bool) -> float:
    if is_sp_or_nasdaq_ticker(ticker):
        return SP_NASDAQ_TAKER_COEFFICIENT if is_taker else SP_NASDAQ_MAKER_COEFFICIENT
    return DEFAULT_TAKER_COEFFICIENT if is_taker else DEFAULT_MAKER_COEFFICIENT


def total_fee(
    *,
    price: float,
    contracts: int,
    ticker: str = "",
    is_taker: bool = True,
) -> float:
    """Aggregate fee for an order, matching Kalshi's published formula::

        fees = ceil_cents( coefficient * contracts * price * (1 - price) )

    The single ``ceil`` is done over the whole order, **not** per
    contract. This means an N-contract order pays slightly less than N
    times a 1-contract order at the same price (Kalshi's edge to the
    user from rounding-once vs rounding-N-times).
    """

    if price <= 0 or price >= 1 or contracts <= 0:
        return 0.0
    coeff = fee_coefficient(ticker, is_taker)
    return _ceil_cents(coeff * contracts * price * (1 - price))


def per_contract_fee(price: float, ticker: str = "", is_taker: bool = True) -> float:
    """Single-contract fee in dollars (ceil to next cent).

    Equivalent to ``total_fee(contracts=1, ...)``. Useful for risk
    calculations where you want the marginal cost of one more contract;
    note this is the **upper bound** on the per-contract effective fee
    (large orders amortize the ceil and pay strictly less).
    """

    return total_fee(price=price, contracts=1, ticker=ticker, is_taker=is_taker)


def quote_fee(
    *,
    ticker: str,
    price: float,
    contracts: int,
    side: str = "YES",
    is_taker: bool = True,
) -> FeeQuote:
    coeff = fee_coefficient(ticker, is_taker)
    total = total_fee(
        price=price, contracts=contracts, ticker=ticker, is_taker=is_taker
    )
    per_c = total / contracts if contracts > 0 else 0.0
    return FeeQuote(
        ticker=ticker,
        price=price,
        contracts=max(0, contracts),
        side=side,
        is_taker=is_taker,
        coefficient=coeff,
        per_contract=per_c,
        total=total,
    )


def basket_fee(
    legs: Iterable[tuple[str, float, int]],
    *,
    is_taker: bool = True,
) -> float:
    """Total fee for an arbitrage basket. ``legs`` is an iterable of
    ``(ticker, price, contracts)`` tuples. Each leg is ceil-rounded
    independently — Kalshi charges per fill, not per basket."""

    return sum(
        total_fee(price=p, contracts=c, ticker=t, is_taker=is_taker)
        for t, p, c in legs
    )


def net_edge_per_contract(
    *,
    entry_price: float,
    fair_price: float,
    ticker: str = "",
    is_taker: bool = True,
) -> float:
    """Edge per contract after subtracting fees.

    The gross edge is the difference between your fair value estimate
    and the entry price (in $/contract). Fees are paid per contract
    irrespective of outcome (Kalshi charges on execution, not on
    settlement), so we just subtract the per-contract fee.
    """

    gross = max(0.0, fair_price - entry_price)
    fee = per_contract_fee(entry_price, ticker, is_taker)
    return gross - fee
