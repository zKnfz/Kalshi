"""Polymarket integration: read-only market discovery + cross-platform
arbitrage detection vs. Kalshi.

The Gamma API at ``gamma-api.polymarket.com`` is unauthenticated and
returns market metadata with embedded best-bid / best-ask quotes.
Two quirks worth remembering when reading the responses:

  * ``outcomes``, ``outcomePrices`` and ``clobTokenIds`` are
    JSON-encoded strings *inside* the JSON response — every consumer
    has to ``json.loads`` them a second time.
  * The order of entries inside those arrays is not guaranteed to be
    ``["Yes", "No"]`` — always map by name from the ``outcomes`` array.

Cross-platform arbitrage (the user's #1 ask): when a market exists on
both Kalshi and Polymarket, the YES + NO total across the two venues
should sum to >= $1.00. If ``kalshi_yes_ask + polymarket_no_ask < 1``
(or vice versa) you can lock in profit by holding the cheaper YES on
one venue and the cheaper NO on the other. ``find_cross_arbs`` does
that scan; matching uses a hand-maintained ticker mapping
(``POLYMARKET_MATCH_PATH``) plus a title-similarity fallback.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Iterable

import httpx

from .config import settings
from .fees import per_contract_fee
from .models import Market, Opportunity

log = logging.getLogger(__name__)


@dataclass
class PolymarketMarket:
    condition_id: str
    slug: str
    question: str
    outcomes: list[str]
    outcome_prices: list[float]
    best_bid: float | None
    best_ask: float | None
    volume_24h: float
    liquidity_usd: float
    active: bool
    closed: bool
    accepting_orders: bool
    end_date: str | None
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def yes_price(self) -> float | None:
        for name, price in zip(self.outcomes, self.outcome_prices):
            if name.lower() == "yes":
                return price
        if self.outcome_prices:
            return self.outcome_prices[0]
        return None

    @property
    def no_price(self) -> float | None:
        for name, price in zip(self.outcomes, self.outcome_prices):
            if name.lower() == "no":
                return price
        if len(self.outcome_prices) >= 2:
            return self.outcome_prices[1]
        if self.outcome_prices:
            return 1.0 - self.outcome_prices[0]
        return None

    @classmethod
    def from_api(cls, data: dict[str, Any]) -> "PolymarketMarket":
        outcomes_raw = data.get("outcomes") or "[]"
        prices_raw = data.get("outcomePrices") or "[]"
        if isinstance(outcomes_raw, str):
            try:
                outcomes = json.loads(outcomes_raw)
            except json.JSONDecodeError:
                outcomes = []
        else:
            outcomes = list(outcomes_raw or [])
        if isinstance(prices_raw, str):
            try:
                prices = [float(p) for p in json.loads(prices_raw)]
            except (json.JSONDecodeError, ValueError, TypeError):
                prices = []
        else:
            prices = [float(p) for p in (prices_raw or [])]

        def _f(value: Any) -> float | None:
            if value is None or value == "":
                return None
            try:
                return float(value)
            except (TypeError, ValueError):
                return None

        return cls(
            condition_id=str(data.get("conditionId") or data.get("id") or ""),
            slug=str(data.get("slug") or ""),
            question=str(data.get("question") or ""),
            outcomes=outcomes,
            outcome_prices=prices,
            best_bid=_f(data.get("bestBid")),
            best_ask=_f(data.get("bestAsk")),
            volume_24h=float(data.get("volume24hr") or 0.0),
            liquidity_usd=float(
                data.get("liquidityNum") or data.get("liquidityClob") or 0.0
            ),
            active=bool(data.get("active")),
            closed=bool(data.get("closed")),
            accepting_orders=bool(data.get("acceptingOrders")),
            end_date=data.get("endDate"),
            raw=data,
        )


class PolymarketClient:
    """Async client for the Gamma REST API."""

    def __init__(
        self,
        base_url: str | None = None,
        timeout: float = 20.0,
    ) -> None:
        self.base_url = (base_url or settings.polymarket_base_url).rstrip("/")
        self._client = httpx.AsyncClient(
            base_url=self.base_url,
            timeout=timeout,
            headers={"User-Agent": "kalshi-edge-analyzer/0.1"},
        )

    async def close(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> "PolymarketClient":
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.close()

    async def list_markets(
        self,
        *,
        active: bool = True,
        closed: bool = False,
        limit: int = 500,
        offset: int = 0,
    ) -> list[PolymarketMarket]:
        params = {
            "active": "true" if active else "false",
            "closed": "true" if closed else "false",
            "limit": limit,
            "offset": offset,
        }
        resp = await self._client.get("/markets", params=params)
        resp.raise_for_status()
        data = resp.json()
        if isinstance(data, dict):
            items = data.get("markets") or data.get("data") or []
        else:
            items = data
        return [PolymarketMarket.from_api(m) for m in items]

    async def search(self, query: str, limit: int = 20) -> list[PolymarketMarket]:
        resp = await self._client.get(
            "/public-search", params={"q": query, "limit": limit}
        )
        resp.raise_for_status()
        data = resp.json() or {}
        items = []
        for bucket in ("markets", "events"):
            for m in (data.get(bucket) or []):
                if "outcomes" in m:
                    items.append(m)
                for child in m.get("markets") or []:
                    items.append(child)
        return [PolymarketMarket.from_api(m) for m in items]


def _normalize(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]+", " ", text.lower())).strip()


def title_similarity(a: str, b: str) -> float:
    """Simple bag-of-words Jaccard + sequence ratio average."""

    na, nb = _normalize(a), _normalize(b)
    if not na or not nb:
        return 0.0
    set_a = set(na.split())
    set_b = set(nb.split())
    jacc = (
        len(set_a & set_b) / len(set_a | set_b) if (set_a | set_b) else 0.0
    )
    ratio = SequenceMatcher(None, na, nb).ratio()
    return 0.5 * jacc + 0.5 * ratio


def load_manual_match_map(path: str | None = None) -> dict[str, str]:
    """Load a JSON ``{kalshi_ticker: polymarket_slug_or_conditionId}`` map.

    Manual mappings always win over automatic title matching.
    """

    p = path or settings.polymarket_match_path
    if not p or not Path(p).exists():
        return {}
    try:
        with open(p) as f:
            return json.load(f)
    except Exception as exc:
        log.warning("polymarket match map %s unreadable: %s", p, exc)
        return {}


def match_kalshi_to_polymarket(
    kalshi_market: Market,
    polymarket_markets: Iterable[PolymarketMarket],
    *,
    manual_map: dict[str, str] | None = None,
    min_similarity: float = 0.55,
) -> PolymarketMarket | None:
    """Return the best Polymarket counterpart for a Kalshi market."""

    manual = manual_map or {}
    target = manual.get(kalshi_market.ticker)
    if target:
        for pm in polymarket_markets:
            if pm.slug == target or pm.condition_id == target:
                return pm

    if not kalshi_market.title:
        return None
    best: PolymarketMarket | None = None
    best_score = 0.0
    for pm in polymarket_markets:
        if not pm.active or pm.closed:
            continue
        score = title_similarity(kalshi_market.title, pm.question)
        if score > best_score:
            best = pm
            best_score = score
    return best if best_score >= min_similarity else None


def find_cross_arbs(
    kalshi_markets: Iterable[Market],
    polymarket_markets: Iterable[PolymarketMarket],
    *,
    manual_map: dict[str, str] | None = None,
    min_net_edge_pct: float | None = None,
) -> list[Opportunity]:
    """Detect cross-platform two-leg arbitrage opportunities.

    For every Kalshi market we can pair with a Polymarket counterpart,
    consider both legs:

      * **YES on Kalshi + NO on Polymarket** — costs
        ``kalshi_yes_ask + polymarket_no_ask`` (per pair of contracts).
        Net of Kalshi fees + assumed 2% Polymarket fee buffer.
      * **NO on Kalshi + YES on Polymarket** — symmetric.

    Either leg ``total < $1.00`` is potential arb; we emit an
    ``Opportunity`` with ``strategy=cross_platform_arbitrage`` whose
    rationale spells out the per-pair profit.
    """

    poly_list = list(polymarket_markets)
    threshold = (
        min_net_edge_pct
        if min_net_edge_pct is not None
        else settings.min_net_edge_pct
    )

    out: list[Opportunity] = []
    for km in kalshi_markets:
        if km.yes_ask is None and km.no_ask is None:
            continue
        pm = match_kalshi_to_polymarket(km, poly_list, manual_map=manual_map)
        if pm is None:
            continue

        pm_yes = pm.best_ask if pm.best_ask else pm.yes_price
        pm_no = (1.0 - pm.yes_price) if pm.yes_price else pm.no_price
        if pm_yes is None or pm_no is None:
            continue

        for k_side, k_ask_cents, p_other in (
            ("YES", km.yes_ask, pm_no),
            ("NO", km.no_ask, pm_yes),
        ):
            if k_ask_cents is None or k_ask_cents <= 0:
                continue
            k_ask = k_ask_cents / 100.0
            total = k_ask + p_other
            if total >= 1.0 - 0.005:
                continue
            gross = 1.0 - total
            k_fee = per_contract_fee(
                k_ask, ticker=km.ticker, is_taker=settings.assume_taker_fees
            )
            p_fee = max(0.005, p_other * 0.02)
            net = gross - k_fee - p_fee
            if net <= 0:
                continue
            net_pct = net / total * 100.0
            if net_pct < threshold:
                continue
            rationale = (
                f"Kalshi {k_side} ask {k_ask:.2f} + Polymarket "
                f"{'NO' if k_side == 'YES' else 'YES'} ask {p_other:.2f} = "
                f"{total:.3f}. Net edge after \u224830% fees + 2% PM buffer: "
                f"{net*100:.1f}¢/pair ({net_pct:.1f}%). "
                f"Matched '{km.title}' ↔ '{pm.question}'."
            )
            out.append(
                Opportunity(
                    ticker=km.ticker,
                    event_ticker=km.event_ticker,
                    title=f"{km.title} (Kalshi×Polymarket)",
                    side=f"K-{k_side}+PM-{'NO' if k_side == 'YES' else 'YES'}",
                    strategy="cross_platform_arbitrage",
                    signal_types=["cross_platform_arbitrage"],
                    entry_price=total,
                    fair_price=1.0,
                    edge=gross,
                    edge_pct=gross / total * 100.0,
                    fees_per_contract=k_fee + p_fee,
                    net_edge=net,
                    net_edge_pct=net_pct,
                    kelly_fraction=settings.kelly_fraction,
                    suggested_stake=min(
                        settings.bankroll * settings.max_bet_pct,
                        settings.bankroll
                        * settings.arb_bankroll_share
                        * settings.kelly_fraction,
                    ),
                    expected_value=net,
                    confidence=0.65,
                    score=net * 2.0,
                    liquidity=km.liquidity,
                    volume_24h=km.volume_24h,
                    spread_cents=km.spread_cents,
                    last_trade_age_seconds=km.last_trade_age_seconds,
                    close_time=km.close_time,
                    rationale=rationale,
                    extra={
                        "polymarket_slug": pm.slug,
                        "polymarket_condition_id": pm.condition_id,
                        "polymarket_question": pm.question,
                        "polymarket_yes_price": pm.yes_price,
                        "polymarket_no_price": pm.no_price,
                        "kalshi_ask": k_ask,
                        "total": total,
                        "kalshi_fee_per_contract": k_fee,
                        "polymarket_fee_assumed": p_fee,
                    },
                )
            )
    return out
