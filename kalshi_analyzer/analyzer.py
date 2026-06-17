from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Iterable

from .config import settings
from .models import Event, Market, Opportunity


CENT = 1 / 100.0
EPS = 1e-9


def _safe_cents_to_dollars(value: int | None) -> float | None:
    if value is None:
        return None
    return max(0.0, min(1.0, value * CENT))


def _hours_to_close(close_time: str | None) -> float | None:
    if not close_time:
        return None
    try:
        if close_time.endswith("Z"):
            close_time = close_time[:-1] + "+00:00"
        dt = datetime.fromisoformat(close_time)
        delta = dt.astimezone(timezone.utc) - datetime.now(tz=timezone.utc)
        return max(0.0, delta.total_seconds() / 3600.0)
    except Exception:
        return None


def kelly_fraction_for_yes(entry_price: float, fair_price: float) -> float:
    """Kelly fraction for buying a binary YES contract at ``entry_price``.

    A YES contract pays $1 if the event resolves true, $0 otherwise.
    With our estimated win probability ``fair_price``:
        win amount  = 1 - entry_price
        loss amount = entry_price
        Kelly: f* = (q*(1-p) - (1-q)*p) / (1-p)  =  (q - p) / (1 - p)
    """

    p = max(EPS, min(1 - EPS, entry_price))
    q = max(0.0, min(1.0, fair_price))
    denom = 1.0 - p
    if denom <= EPS:
        return 0.0
    f = (q - p) / denom
    return max(0.0, min(1.0, f))


def liquidity_confidence(market: Market) -> float:
    """Map raw liquidity + 24h volume into a 0-1 confidence score."""

    liq_proxy = max(
        market.liquidity,
        market.volume_24h * 100,
        market.open_interest * 100,
    )
    vol = max(0, market.volume_24h)
    oi = max(0, market.open_interest)
    spread = market.spread_cents if market.spread_cents is not None else 99

    liq_score = math.tanh(liq_proxy / 50_000.0)
    vol_score = math.tanh(vol / 5_000.0)
    oi_score = math.tanh(oi / 5_000.0)
    spread_score = max(0.0, 1.0 - spread / 25.0)

    return round(
        0.35 * liq_score + 0.30 * vol_score + 0.15 * oi_score + 0.20 * spread_score,
        4,
    )


def consensus_fair_price(market: Market) -> float | None:
    """Blend mid, last, and previous prices into a 'consensus' fair value."""

    mid = market.mid_price
    last = _safe_cents_to_dollars(market.last_price)
    prev = _safe_cents_to_dollars(market.previous_price)

    samples: list[tuple[float, float]] = []
    if mid is not None:
        samples.append((mid, 0.6))
    if last is not None:
        samples.append((last, 0.3))
    if prev is not None:
        samples.append((prev, 0.1))

    if not samples:
        return None
    num = sum(p * w for p, w in samples)
    den = sum(w for _, w in samples)
    return num / den if den else None


def _build_opportunity(
    market: Market,
    side: str,
    entry_price: float,
    fair_price: float,
    strategy: str,
    rationale: str,
    confidence_boost: float = 0.0,
    extra: dict | None = None,
) -> Opportunity | None:
    if entry_price <= 0 or entry_price >= 1:
        return None
    if fair_price <= 0 or fair_price >= 1:
        return None

    edge = fair_price - entry_price
    if edge <= 0:
        return None

    edge_pct = edge / entry_price * 100.0
    ev = edge
    kelly = kelly_fraction_for_yes(entry_price, fair_price)
    kelly_scaled = kelly * settings.kelly_fraction
    stake = max(0.0, kelly_scaled * settings.bankroll)

    confidence = min(1.0, liquidity_confidence(market) + confidence_boost)

    arb_bonus = 1.5 if strategy.endswith("arbitrage") else 1.0
    score = edge * confidence * arb_bonus * (1.0 + math.tanh(edge_pct / 25.0))

    return Opportunity(
        ticker=market.ticker,
        event_ticker=market.event_ticker,
        title=market.title or market.ticker,
        side=side,
        strategy=strategy,
        entry_price=entry_price,
        fair_price=fair_price,
        edge=edge,
        edge_pct=edge_pct,
        kelly_fraction=kelly_scaled,
        suggested_stake=stake,
        expected_value=ev,
        confidence=confidence,
        score=score,
        liquidity=market.liquidity,
        volume_24h=market.volume_24h,
        spread_cents=market.spread_cents,
        close_time=market.close_time,
        rationale=rationale,
        extra=extra or {},
    )


def analyze_yes_no_arbitrage(market: Market) -> Opportunity | None:
    """Detect pure YES+NO buy arbitrage: yes_ask + no_ask < $1.00."""

    yes_ask = _safe_cents_to_dollars(market.yes_ask)
    no_ask = _safe_cents_to_dollars(market.no_ask)
    if yes_ask is None or no_ask is None:
        return None
    if yes_ask <= 0 or no_ask <= 0:
        return None
    total = yes_ask + no_ask
    if total >= 1.0 - 0.005:
        return None

    profit = 1.0 - total
    confidence = min(1.0, liquidity_confidence(market) + 0.3)
    score = profit * confidence * 2.5

    rationale = (
        f"YES ask {yes_ask:.2f} + NO ask {no_ask:.2f} = {total:.3f} < $1.00; "
        f"buying both sides locks in {profit*100:.1f}¢ per contract pair."
    )
    return Opportunity(
        ticker=market.ticker,
        event_ticker=market.event_ticker,
        title=market.title or market.ticker,
        side="YES+NO",
        strategy="yes_no_arbitrage",
        entry_price=total,
        fair_price=1.0,
        edge=profit,
        edge_pct=profit / total * 100.0,
        kelly_fraction=settings.kelly_fraction,
        suggested_stake=settings.bankroll * settings.kelly_fraction,
        expected_value=profit,
        confidence=confidence,
        score=score,
        liquidity=market.liquidity,
        volume_24h=market.volume_24h,
        spread_cents=market.spread_cents,
        close_time=market.close_time,
        rationale=rationale,
        extra={"yes_ask": yes_ask, "no_ask": no_ask, "guaranteed_profit": profit},
    )


def analyze_market_fair_value(market: Market) -> list[Opportunity]:
    """Compare best YES/NO asks against the consensus fair price."""

    fair = consensus_fair_price(market)
    if fair is None:
        return []

    out: list[Opportunity] = []
    yes_ask = _safe_cents_to_dollars(market.yes_ask)
    if yes_ask is not None and yes_ask > 0:
        op = _build_opportunity(
            market,
            side="YES",
            entry_price=yes_ask,
            fair_price=fair,
            strategy="fair_value_yes",
            rationale=(
                f"YES ask {yes_ask:.2f} is below blended fair {fair:.2f} "
                f"(mid + last + prior)."
            ),
        )
        if op:
            out.append(op)

    no_ask = _safe_cents_to_dollars(market.no_ask)
    if no_ask is not None and no_ask > 0:
        no_fair = 1.0 - fair
        op = _build_opportunity(
            market,
            side="NO",
            entry_price=no_ask,
            fair_price=no_fair,
            strategy="fair_value_no",
            rationale=(
                f"NO ask {no_ask:.2f} is below implied NO fair {no_fair:.2f} "
                f"(=1−blended YES fair {fair:.2f})."
            ),
        )
        if op:
            out.append(op)
    return out


def analyze_event_dutch_book(event: Event) -> list[Opportunity]:
    """Find arbitrage / mispricings across markets within an event.

    For an event whose markets are mutually exclusive and exhaustive, the
    YES prices must sum to 1. Two signals are produced:
      * Hard arbitrage if Σ(yes_ask) < 1.
      * Soft mispricing if Σ(mid) > 1, allowing per-market normalization.
    """

    markets = [m for m in event.markets if (m.yes_bid or 0) > 0 or (m.yes_ask or 0) > 0]
    if len(markets) < 2:
        return []

    asks = []
    mids = []
    for m in markets:
        a = _safe_cents_to_dollars(m.yes_ask)
        mid = m.mid_price
        if a is None or mid is None:
            asks = []
            break
        asks.append(a)
        mids.append(mid)
    if not asks:
        return []

    opportunities: list[Opportunity] = []
    sum_asks = sum(asks)
    sum_mids = sum(mids)

    if sum_asks < 1.0 - 0.01:
        profit = 1.0 - sum_asks
        for m, a in zip(markets, asks):
            implied_fair = max(a + EPS, min(1.0 - EPS, a + profit * (a / sum_asks)))
            op = _build_opportunity(
                m,
                side="YES",
                entry_price=a,
                fair_price=implied_fair,
                strategy="dutch_book_arbitrage",
                rationale=(
                    f"Σ(YES asks)={sum_asks:.3f} < 1.00 across {len(markets)} "
                    f"mutually-exclusive markets — buying every YES locks in "
                    f"{profit*100:.1f}¢ regardless of outcome."
                ),
                confidence_boost=0.35,
                extra={
                    "event_sum_asks": sum_asks,
                    "event_basket_profit": profit,
                    "basket_size": len(markets),
                },
            )
            if op:
                opportunities.append(op)
        return opportunities

    if sum_mids > 0 and abs(sum_mids - 1.0) > 0.02:
        for m, mid, a in zip(markets, mids, asks):
            normalized = mid / sum_mids
            if a > 0 and normalized > a + 0.005:
                op = _build_opportunity(
                    m,
                    side="YES",
                    entry_price=a,
                    fair_price=normalized,
                    strategy="dutch_book_mispricing",
                    rationale=(
                        f"Event mids sum to {sum_mids:.3f}≠1. Normalizing puts "
                        f"this market's fair at {normalized:.2f} vs ask {a:.2f}."
                    ),
                    confidence_boost=0.10,
                    extra={"event_sum_mids": sum_mids, "normalized_fair": normalized},
                )
                if op:
                    opportunities.append(op)
    return opportunities


def looks_mutually_exclusive(markets: list[Market]) -> bool:
    """Heuristic: treat an event as mutually-exclusive when its YES mids
    sum to something close to 1, with at least 3 priced markets. This
    catches range/ordinal events whose API flag is not reliably set."""

    if len(markets) < 3:
        return False
    mids = [m.mid_price for m in markets if m.mid_price is not None]
    if len(mids) < 3:
        return False
    total = sum(mids)
    return 0.85 <= total <= 1.15


def is_tradable(market: Market) -> bool:
    if market.status not in {"active", "open", "initialized", ""}:
        return False
    if market.yes_ask is None and market.no_ask is None:
        return False
    if (market.yes_ask or 0) <= 0 and (market.no_ask or 0) <= 0:
        return False
    spread = market.spread_cents if market.spread_cents is not None else 99
    if spread >= 99:
        return False
    liquidity_proxy = max(
        market.liquidity,
        market.volume_24h * 100,
        market.open_interest * 100,
    )
    return liquidity_proxy >= settings.min_liquidity_cents


def evaluate_markets(events: Iterable[Event]) -> list[Opportunity]:
    """Run the full analyzer pipeline over a collection of events.

    Returns a list of opportunities sorted by descending score.
    """

    found: list[Opportunity] = []
    for ev in events:
        tradable = [m for m in ev.markets if is_tradable(m)]
        if not tradable:
            continue

        for m in tradable:
            arb = analyze_yes_no_arbitrage(m)
            if arb:
                found.append(arb)
            found.extend(analyze_market_fair_value(m))

        mx = ev.mutually_exclusive or looks_mutually_exclusive(tradable)
        if mx and len(tradable) >= 2:
            ev_for_arb = Event(
                event_ticker=ev.event_ticker,
                title=ev.title,
                category=ev.category,
                mutually_exclusive=True,
                markets=tradable,
                raw=ev.raw,
            )
            found.extend(analyze_event_dutch_book(ev_for_arb))

    seen: dict[tuple[str, str, str], Opportunity] = {}
    for op in found:
        key = (op.ticker, op.side, op.strategy)
        if key not in seen or op.score > seen[key].score:
            seen[key] = op

    out = sorted(seen.values(), key=lambda o: o.score, reverse=True)
    return out
