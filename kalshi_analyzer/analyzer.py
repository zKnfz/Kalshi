from __future__ import annotations

import math
from collections import defaultdict
from datetime import datetime, timezone
from typing import Iterable

from .config import settings, strategy_bankroll_cap
from .fees import per_contract_fee
from .models import Event, Market, Opportunity
from .sports import is_sports_market, live_status, series_ticker_for
from .sports_types import SportsPrediction


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
    """Kelly fraction for a binary YES contract on Kalshi.

    Derivation (single-trial growth-optimal stake):

    A Kalshi YES contract bought at price ``p`` (in $/contract) pays
    ``$1`` if YES resolves true, ``$0`` otherwise. Spending fraction ``f``
    of bankroll buys ``f·B / p`` contracts, so:

      * with probability ``q`` (your estimated YES probability):
          bankroll → ``B · (1 + f · (1-p)/p)``
      * with probability ``1-q``:
          bankroll → ``B · (1 - f)``

    Setting the derivative of expected log-growth to zero yields::

        f* = (b·q - (1-q)) / b   with   b = (1-p)/p

    which simplifies algebraically to the closed form used here::

        f* = (q - p) / (1 - p)

    Both forms are equivalent; the closed form is preferred because it is
    numerically stable at ``p → 0`` (whereas ``b → ∞``). When the user-
    specified fair value ``q`` equals ``p`` the result is exactly zero,
    i.e. no edge ⇒ no bet. The result is clamped to ``[0, 1]``.

    The fully-Kelly fraction is later scaled by ``KELLY_FRACTION`` (¼ by
    default) and hard-capped by ``MAX_BET_PCT`` in ``size_position``.
    """

    p = max(EPS, min(1 - EPS, entry_price))
    q = max(0.0, min(1.0, fair_price))
    denom = 1.0 - p
    if denom <= EPS:
        return 0.0
    f = (q - p) / denom
    return max(0.0, min(1.0, f))


def size_position(
    strategy: str,
    raw_kelly: float,
    *,
    bankroll: float | None = None,
) -> tuple[float, float]:
    """Return ``(scaled_kelly_fraction, stake_dollars)`` after applying:

    1. ``KELLY_FRACTION`` (fractional-Kelly de-risk).
    2. ``MAX_BET_PCT`` hard cap on per-bet bankroll fraction.
    3. Per-strategy bankroll sub-cap so arb and fair-value pools don't
       drain each other.
    """

    bk = settings.bankroll if bankroll is None else bankroll
    scaled = max(0.0, raw_kelly * settings.kelly_fraction)
    scaled = min(scaled, settings.max_bet_pct)
    stake = scaled * bk
    cap = strategy_bankroll_cap(strategy)
    stake = min(stake, cap)
    return scaled, stake


def consensus_fair_price(market: Market) -> float | None:
    """Recency-weighted blend of mid, last, and previous prices.

    Weights are ``RECENCY_WEIGHTS`` (default ``(0.50, 0.35, 0.15)`` for
    mid/last/prior). The ``last_price`` weight is exponentially decayed
    once ``last_trade_age_seconds`` exceeds ``STALE_LAST_AGE_SECONDS`` —
    each subsequent ``STALE_LAST_AGE_SECONDS`` halves the weight, so a
    minutes-stale last trade contributes essentially nothing while a
    seconds-fresh trade contributes its full configured weight.
    """

    mid = market.mid_price
    last = _safe_cents_to_dollars(market.last_price)
    prev = _safe_cents_to_dollars(market.previous_price)

    w_mid, w_last, w_prior = settings.recency_weights
    if market.last_trade_age_seconds is not None and settings.stale_last_age_seconds > 0:
        excess = max(
            0.0,
            market.last_trade_age_seconds - settings.stale_last_age_seconds,
        )
        decay = 0.5 ** (excess / settings.stale_last_age_seconds)
        w_last *= decay

    samples: list[tuple[float, float]] = []
    if mid is not None and w_mid > 0:
        samples.append((mid, w_mid))
    if last is not None and w_last > 0:
        samples.append((last, w_last))
    if prev is not None and w_prior > 0:
        samples.append((prev, w_prior))

    if not samples:
        return None
    num = sum(p * w for p, w in samples)
    den = sum(w for _, w in samples)
    return num / den if den else None


def liquidity_confidence(market: Market) -> float:
    """Map raw liquidity / volume / spread into a 0-1 confidence score.

    Returns ``0.0`` immediately when ``liquidity <= 0`` — a zero-liquidity
    leg cannot be traded regardless of edge.
    """

    if market.liquidity <= 0:
        return 0.0

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

    conf = 0.35 * liq_score + 0.30 * vol_score + 0.15 * oi_score + 0.20 * spread_score

    if (
        market.last_trade_age_seconds is not None
        and settings.stale_last_age_seconds > 0
    ):
        excess = max(
            0.0,
            market.last_trade_age_seconds - settings.stale_last_age_seconds,
        )
        conf *= 0.5 ** (excess / (settings.stale_last_age_seconds * 4))

    return round(max(0.0, min(1.0, conf)), 4)


def _orderbook_size_for(side: str, market: Market) -> int | None:
    """Best-effort top-of-book size when included in the market payload."""

    raw = market.raw or {}
    keys = (
        (f"{side.lower()}_ask_size", f"{side.lower()}_ask_count")
        if side.upper() == "YES"
        else (f"{side.lower()}_ask_size", f"{side.lower()}_ask_count")
    )
    for k in keys:
        v = raw.get(k)
        if v is None:
            continue
        try:
            return int(v)
        except (TypeError, ValueError):
            continue
    return None


def _leg_fill_feasible(market: Market, side: str) -> bool:
    """Return whether the top-of-book can fill at least ``MIN_FILL_QTY``."""

    if market.liquidity <= 0:
        return False
    size = _orderbook_size_for(side, market)
    if size is None:
        return True
    return size >= settings.min_fill_qty


def _market_meta(market: Market) -> dict[str, object]:
    return {
        "series_ticker": series_ticker_for(market.ticker, market.event_ticker),
        "is_sports": is_sports_market(market.ticker, market.event_ticker),
        "live_status": live_status(market.close_time),
    }


def _apply_market_meta(op: Opportunity, market: Market) -> None:
    meta = _market_meta(market)
    op.series_ticker = str(meta["series_ticker"])
    op.is_sports = bool(meta["is_sports"])
    op.live_status = meta["live_status"]  # type: ignore[assignment]


def _basket_id_for(event: Event) -> str:
    return event.event_ticker or event.raw.get("series_ticker") or "unknown"


def _expected_basket_tickers(event: Event) -> set[str]:
    return {m.ticker for m in event.markets if m.ticker}


def _basket_is_complete(event: Event, tradable: list[Market]) -> bool:
    """Every mutually-exclusive leg from the event must be present and tradable."""

    if not event.mutually_exclusive:
        return True
    expected = _expected_basket_tickers(event)
    if len(expected) < 2:
        return True
    tradable_tickers = {m.ticker for m in tradable}
    return expected <= tradable_tickers


def _incomplete_basket_opportunities(
    event: Event,
    tradable: list[Market],
    *,
    strategy: str,
) -> list[Opportunity]:
    """Surface incomplete baskets without computing fair values or arb edge."""

    basket_id = _basket_id_for(event)
    series = series_ticker_for(
        tradable[0].ticker if tradable else "",
        event.event_ticker,
    )
    is_sports = any(
        is_sports_market(m.ticker, m.event_ticker) for m in tradable
    )
    live = next(
        (live_status(m.close_time) for m in tradable if live_status(m.close_time)),
        None,
    )
    missing = _expected_basket_tickers(event) - {m.ticker for m in tradable}
    rationale = (
        f"Incomplete basket: {len(missing)} leg(s) missing from tradable set "
        f"(dropped by liquidity/spread/volume filter or not fetched). "
        f"Fair value and arb edge skipped."
    )
    out: list[Opportunity] = []
    for m in tradable:
        yes_ask = _safe_cents_to_dollars(m.yes_ask) or 0.0
        out.append(
            Opportunity(
                ticker=m.ticker,
                event_ticker=m.event_ticker,
                title=m.title or m.ticker,
                side="YES",
                strategy=strategy,
                signal_types=[strategy],
                entry_price=yes_ask,
                fair_price=yes_ask,
                edge=0.0,
                edge_pct=0.0,
                fees_per_contract=0.0,
                net_edge=0.0,
                net_edge_pct=0.0,
                kelly_fraction=0.0,
                suggested_stake=0.0,
                expected_value=0.0,
                confidence=0.0,
                score=0.0,
                liquidity=m.liquidity,
                volume_24h=m.volume_24h,
                spread_cents=m.spread_cents,
                last_trade_age_seconds=m.last_trade_age_seconds,
                close_time=m.close_time,
                rationale=rationale,
                fill_feasible=False,
                basket_complete=False,
                basket_id=basket_id,
                series_ticker=series,
                is_sports=is_sports,
                live_status=live,
                extra={"missing_legs": sorted(missing)},
            )
        )
    return out


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
    if edge_pct < settings.min_edge_pct:
        return None

    fee = per_contract_fee(
        entry_price,
        ticker=market.ticker,
        is_taker=settings.assume_taker_fees,
    )
    net_edge = edge - fee
    net_edge_pct = (net_edge / entry_price * 100.0) if entry_price > 0 else 0.0
    if net_edge_pct < settings.min_net_edge_pct:
        return None

    ev = net_edge
    raw_kelly = kelly_fraction_for_yes(entry_price + fee, fair_price)
    kelly_scaled, stake = size_position(strategy, raw_kelly)

    fill_ok = _leg_fill_feasible(market, side)
    if market.liquidity <= 0 or not fill_ok:
        confidence = 0.0
    else:
        confidence = min(1.0, liquidity_confidence(market) + confidence_boost)

    arb_bonus = 1.5 if strategy.endswith("arbitrage") else 1.0
    score = net_edge * confidence * arb_bonus * (1.0 + math.tanh(net_edge_pct / 25.0))

    op = Opportunity(
        ticker=market.ticker,
        event_ticker=market.event_ticker,
        title=market.title or market.ticker,
        side=side,
        strategy=strategy,
        signal_types=[strategy],
        entry_price=entry_price,
        fair_price=fair_price,
        edge=edge,
        edge_pct=edge_pct,
        fees_per_contract=fee,
        net_edge=net_edge,
        net_edge_pct=net_edge_pct,
        kelly_fraction=kelly_scaled,
        suggested_stake=stake,
        expected_value=ev,
        confidence=confidence,
        score=score,
        liquidity=market.liquidity,
        volume_24h=market.volume_24h,
        spread_cents=market.spread_cents,
        last_trade_age_seconds=market.last_trade_age_seconds,
        close_time=market.close_time,
        rationale=rationale,
        extra=extra or {},
    )
    op.fill_feasible = fill_ok and market.liquidity > 0
    _apply_market_meta(op, market)
    return op


def analyze_yes_no_arbitrage(market: Market) -> Opportunity | None:
    """Detect pure YES+NO buy arbitrage: ``yes_ask + no_ask < $1.00``."""

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
    yes_fee = per_contract_fee(yes_ask, market.ticker, settings.assume_taker_fees)
    no_fee = per_contract_fee(no_ask, market.ticker, settings.assume_taker_fees)
    pair_fee = yes_fee + no_fee
    net_profit = profit - pair_fee
    if net_profit <= 0:
        return None
    net_edge_pct = net_profit / total * 100.0
    if net_edge_pct < settings.min_net_edge_pct:
        return None
    raw_kelly = 1.0
    kelly_scaled, stake = size_position("yes_no_arbitrage", raw_kelly)
    fill_ok = _leg_fill_feasible(market, "YES") and _leg_fill_feasible(market, "NO")
    if market.liquidity <= 0 or not fill_ok:
        confidence = 0.0
    else:
        confidence = min(1.0, liquidity_confidence(market) + 0.3)
    score = net_profit * confidence * 2.5

    rationale = (
        f"YES ask {yes_ask:.2f} + NO ask {no_ask:.2f} = {total:.3f} < $1.00; "
        f"buying both sides nets {net_profit*100:.1f}¢ per pair after "
        f"{pair_fee*100:.2f}¢ in fees."
    )
    op = Opportunity(
        ticker=market.ticker,
        event_ticker=market.event_ticker,
        title=market.title or market.ticker,
        side="YES+NO",
        strategy="yes_no_arbitrage",
        signal_types=["yes_no_arbitrage"],
        entry_price=total,
        fair_price=1.0,
        edge=profit,
        edge_pct=profit / total * 100.0,
        fees_per_contract=pair_fee,
        net_edge=net_profit,
        net_edge_pct=net_edge_pct,
        kelly_fraction=kelly_scaled,
        suggested_stake=stake,
        expected_value=net_profit,
        confidence=confidence,
        score=score,
        liquidity=market.liquidity,
        volume_24h=market.volume_24h,
        spread_cents=market.spread_cents,
        last_trade_age_seconds=market.last_trade_age_seconds,
        close_time=market.close_time,
        rationale=rationale,
        extra={
            "yes_ask": yes_ask,
            "no_ask": no_ask,
            "guaranteed_profit_gross": profit,
            "fees_per_pair": pair_fee,
        },
    )
    op.fill_feasible = fill_ok and market.liquidity > 0
    _apply_market_meta(op, market)
    return op


def analyze_market_fair_value(market: Market) -> list[Opportunity]:
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
                f"YES ask {yes_ask:.2f} below blended fair {fair:.2f} "
                f"(mid+last+prior with stale-last decay)."
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
                f"NO ask {no_ask:.2f} below implied NO fair {no_fair:.2f} "
                f"(=1−YES fair {fair:.2f})."
            ),
        )
        if op:
            out.append(op)
    return out


def _basket_fill_feasibility(markets: list[Market], side: str = "YES") -> tuple[bool, int | None]:
    """Return (feasible, min_observed_size) for a dutch-book basket.

    Runs before confidence scoring. Any leg with ``liquidity <= 0`` or
    reported ask size below ``MIN_FILL_QTY`` breaks the basket.
    """

    sizes: list[int] = []
    for m in markets:
        if m.liquidity <= 0:
            return False, 0
        s = _orderbook_size_for(side, m)
        if s is None:
            continue
        sizes.append(s)
    if not sizes:
        return True, None
    min_size = min(sizes)
    return min_size >= settings.min_fill_qty, min_size


def analyze_event_dutch_book(event: Event) -> list[Opportunity]:
    tradable = [m for m in event.markets if is_tradable(m)]
    quoted = [
        m
        for m in event.markets
        if (m.yes_bid or 0) > 0 or (m.yes_ask or 0) > 0
    ]
    if len(quoted) < 2:
        return []

    basket_complete = _basket_is_complete(event, tradable)
    basket_id = _basket_id_for(event)
    if not basket_complete:
        return _incomplete_basket_opportunities(
            event, quoted, strategy="dutch_book_arbitrage"
        )

    markets = [
        m for m in tradable if (m.yes_bid or 0) > 0 or (m.yes_ask or 0) > 0
    ]
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
    feasible, min_size = _basket_fill_feasibility(markets, "YES")
    zero_liq = any(m.liquidity <= 0 for m in markets)
    feas_penalty = 0.4 if not feasible or zero_liq else 0.0

    if sum_asks < 1.0 - 0.01:
        profit = 1.0 - sum_asks
        for m, a in zip(markets, asks):
            implied_fair = max(
                a + EPS, min(1.0 - EPS, a + profit * (a / sum_asks))
            )
            op = _build_opportunity(
                m,
                side="YES",
                entry_price=a,
                fair_price=implied_fair,
                strategy="dutch_book_arbitrage",
                rationale=(
                    f"Σ(YES asks)={sum_asks:.3f} < 1.00 across {len(markets)} "
                    f"mutually-exclusive markets — buying every YES locks "
                    f"in {profit*100:.1f}¢ regardless of outcome"
                    + (
                        f" (but min leg size {min_size} < MIN_FILL_QTY "
                        f"{settings.min_fill_qty} — basket may not fill)."
                        if not feasible and min_size is not None
                        else "."
                    )
                ),
                confidence_boost=0.35 - feas_penalty,
                extra={
                    "event_sum_asks": sum_asks,
                    "event_basket_profit": profit,
                    "basket_size": len(markets),
                    "min_leg_size": min_size,
                    "fill_feasible": feasible and not zero_liq,
                    "basket_id": basket_id,
                },
            )
            if op:
                op.fill_feasible = feasible and not zero_liq
                op.basket_complete = True
                op.basket_id = basket_id
                opportunities.append(op)
        return opportunities

    if sum_mids > 0 and abs(sum_mids - 1.0) > 0.02:
        no_feasible, no_min_size = _basket_fill_feasibility(markets, "NO")
        mis_feasible = feasible and no_feasible and not zero_liq
        mis_penalty = 0.4 if not mis_feasible else 0.0
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
                        f"Event mids sum to {sum_mids:.3f}≠1. Normalizing "
                        f"puts this market's fair at {normalized:.2f} vs "
                        f"YES ask {a:.2f}."
                    ),
                    confidence_boost=0.10 - mis_penalty,
                    extra={
                        "event_sum_mids": sum_mids,
                        "normalized_fair": normalized,
                        "fill_feasible": mis_feasible,
                        "min_leg_size": min_size,
                        "basket_id": basket_id,
                    },
                )
                if op:
                    op.fill_feasible = mis_feasible
                    op.basket_complete = True
                    op.basket_id = basket_id
                    opportunities.append(op)
            no_ask = _safe_cents_to_dollars(m.no_ask)
            if no_ask is None or no_ask <= 0:
                continue
            no_fair = 1.0 - normalized
            if no_fair > no_ask + 0.005:
                op = _build_opportunity(
                    m,
                    side="NO",
                    entry_price=no_ask,
                    fair_price=no_fair,
                    strategy="dutch_book_mispricing",
                    rationale=(
                        f"Event mids sum to {sum_mids:.3f}≠1. Normalized "
                        f"YES fair {normalized:.2f} ⇒ NO fair {no_fair:.2f}"
                        f" vs NO ask {no_ask:.2f}."
                    ),
                    confidence_boost=0.10 - mis_penalty,
                    extra={
                        "event_sum_mids": sum_mids,
                        "normalized_yes_fair": normalized,
                        "fill_feasible": mis_feasible,
                        "min_leg_size": no_min_size,
                        "basket_id": basket_id,
                    },
                )
                if op:
                    op.fill_feasible = mis_feasible
                    op.basket_complete = True
                    op.basket_id = basket_id
                    opportunities.append(op)
    return opportunities


def looks_mutually_exclusive(markets: list[Market]) -> bool:
    if len(markets) < 3:
        return False
    mids = [m.mid_price for m in markets if m.mid_price is not None]
    if len(mids) < 3:
        return False
    total = sum(mids)
    return 0.85 <= total <= 1.15


def is_tradable(market: Market) -> bool:
    """Pre-signal noise filter.

    Removes markets that would only ever produce illusory edges:
      * non-open status
      * absent quotes
      * spread wider than ``MAX_SPREAD_CENTS``
      * 24h volume below ``MIN_VOLUME_24H`` (or ``SPORTS_MIN_VOLUME_24H``)
      * liquidity proxy below ``MIN_LIQUIDITY_CENTS``
      * ``SPORTS_ONLY_MODE`` when not a sports ticker
    """

    if settings.sports_only_mode and not is_sports_market(
        market.ticker, market.event_ticker
    ):
        return False
    if market.status not in {"active", "open", "initialized", ""}:
        return False
    if market.yes_ask is None and market.no_ask is None:
        return False
    if (market.yes_ask or 0) <= 0 and (market.no_ask or 0) <= 0:
        return False

    spread = market.spread_cents if market.spread_cents is not None else 99
    if spread >= 99:
        return False
    if spread > settings.max_spread_cents:
        return False

    vol_floor = (
        settings.sports_min_volume_24h
        if is_sports_market(market.ticker, market.event_ticker)
        else settings.min_volume_24h
    )
    if vol_floor > 0 and market.volume_24h < vol_floor:
        return False

    liquidity_proxy = max(
        market.liquidity,
        market.volume_24h * 100,
        market.open_interest * 100,
    )
    return liquidity_proxy >= settings.min_liquidity_cents


def evaluate_markets(events: Iterable[Event]) -> list[Opportunity]:
    """Run the full analyzer pipeline and dedupe per (ticker, side).

    Multiple signals on the same (market, side) are merged into a single
    opportunity whose ``signal_types`` lists every matching strategy. The
    primary ``strategy`` becomes the one with the highest individual
    score, and the rationale is the union of the merged rationales.
    """

    raw: list[Opportunity] = []
    for ev in events:
        tradable = [m for m in ev.markets if is_tradable(m)]
        if not tradable:
            continue

        for m in tradable:
            arb = analyze_yes_no_arbitrage(m)
            if arb:
                raw.append(arb)
            raw.extend(analyze_market_fair_value(m))

        mx = ev.mutually_exclusive or looks_mutually_exclusive(tradable)
        if mx and len(tradable) >= 2:
            ev_for_arb = Event(
                event_ticker=ev.event_ticker,
                title=ev.title,
                category=ev.category,
                mutually_exclusive=True,
                markets=ev.markets,
                raw=ev.raw,
            )
            raw.extend(analyze_event_dutch_book(ev_for_arb))

    by_key: dict[tuple[str, str], Opportunity] = {}
    for op in raw:
        existing = by_key.get(op.key())
        if existing is None:
            by_key[op.key()] = op
            continue
        # merge: keep the higher-scoring strategy as primary, accumulate signal_types
        merged_signal_types = list(
            dict.fromkeys([*existing.signal_types, *op.signal_types])
        )
        if op.score > existing.score:
            primary = op
            secondary = existing
        else:
            primary = existing
            secondary = op
        primary.signal_types = merged_signal_types
        primary.fill_feasible = existing.fill_feasible and op.fill_feasible
        primary.basket_complete = existing.basket_complete and op.basket_complete
        primary.rationale = (
            primary.rationale
            if secondary.rationale in primary.rationale
            else f"{primary.rationale} | {secondary.rationale}"
        )
        by_key[op.key()] = primary

    return sorted(by_key.values(), key=lambda o: o.score, reverse=True)


def evaluate_sports_prediction(pred: SportsPrediction) -> Opportunity | None:
    """Convert a live sports model prediction into a ranked Opportunity."""

    market = pred.market
    if market is None:
        return None
    if not is_tradable(market):
        return None

    entry_price = pred.kalshi_yes_ask
    fair_price = pred.model_yes_prob
    if entry_price <= 0 or entry_price >= 1 or fair_price <= 0 or fair_price >= 1:
        return None

    edge = fair_price - entry_price
    if edge <= 0:
        return None

    edge_pct = edge / entry_price * 100.0
    if edge_pct < settings.sports_model_min_edge_pct:
        return None

    fee = per_contract_fee(
        entry_price,
        ticker=market.ticker,
        is_taker=settings.assume_taker_fees,
    )
    net_edge = edge - fee
    net_edge_pct = (net_edge / entry_price * 100.0) if entry_price > 0 else 0.0
    if net_edge_pct < settings.min_net_edge_pct:
        return None

    fill_ok = _leg_fill_feasible(market, "YES")
    gs = pred.game_state or {}
    last_update = float(gs.get("last_update") or 0.0)
    if last_update > 0:
        age = max(
            0.0,
            datetime.now(tz=timezone.utc).timestamp() - last_update,
        )
        if age > settings.stale_last_age_seconds:
            pred.confidence *= 0.5 ** (
                (age - settings.stale_last_age_seconds)
                / max(settings.stale_last_age_seconds, 1)
            )
    if market.liquidity <= 0 or not fill_ok:
        confidence = 0.0
    else:
        confidence = min(1.0, pred.confidence)
    if confidence < settings.sports_model_min_confidence:
        return None

    raw_kelly = kelly_fraction_for_yes(entry_price + fee, fair_price)
    kelly_scaled, stake = size_position("sports_model_edge", raw_kelly)
    score = net_edge * confidence * (1.0 + math.tanh(net_edge_pct / 20.0))

    rationale = (
        f"Sports model {fair_price*100:.1f}% vs Kalshi YES ask {entry_price*100:.1f}% "
        f"({pred.away_team} @ {pred.home_team}, "
        f"{gs.get('clock', '')} P{gs.get('period', '?')}). "
        f"Net edge {net_edge_pct:.1f}% after fees."
    )

    op = Opportunity(
        ticker=market.ticker,
        event_ticker=market.event_ticker,
        title=market.title or market.ticker,
        side="YES",
        strategy="sports_model_edge",
        signal_types=["sports_model_edge"],
        entry_price=entry_price,
        fair_price=fair_price,
        edge=edge,
        edge_pct=edge_pct,
        fees_per_contract=fee,
        net_edge=net_edge,
        net_edge_pct=net_edge_pct,
        kelly_fraction=kelly_scaled,
        suggested_stake=stake,
        expected_value=net_edge,
        confidence=confidence,
        score=score,
        liquidity=market.liquidity,
        volume_24h=market.volume_24h,
        spread_cents=market.spread_cents,
        last_trade_age_seconds=market.last_trade_age_seconds,
        close_time=market.close_time,
        rationale=rationale,
        fill_feasible=fill_ok and market.liquidity > 0,
        model_yes_prob=fair_price,
        game_state=gs,
        extra={
            "sport": pred.sport,
            "home_team": pred.home_team,
            "away_team": pred.away_team,
            "game_state": gs,
        },
    )
    _apply_market_meta(op, market)
    op.is_sports = True
    op.live_status = "LIVE" if gs.get("is_live") else op.live_status
    return op


def group_baskets(opportunities: Iterable[Opportunity]) -> list[dict]:
    """Aggregate dutch-book legs by ``basket_id`` for API/UI consumption."""

    baskets: dict[str, list[Opportunity]] = defaultdict(list)
    for opp in opportunities:
        if opp.strategy in {"dutch_book_arbitrage", "dutch_book_mispricing"}:
            bid = opp.basket_id or opp.event_ticker
            if bid:
                baskets[bid].append(opp)

    grouped: list[dict] = []
    for basket_id, legs in baskets.items():
        if not legs:
            continue
        grouped.append(
            {
                "basket_id": basket_id,
                "strategy": legs[0].strategy,
                "series_ticker": legs[0].series_ticker,
                "event_ticker": legs[0].event_ticker,
                "edge_pct": legs[0].edge_pct,
                "net_edge_pct": min(l.net_edge_pct for l in legs),
                "total_stake": sum(l.suggested_stake for l in legs),
                "worst_liquidity": min(l.liquidity for l in legs),
                "basket_complete": all(l.basket_complete for l in legs),
                "is_sports": any(l.is_sports for l in legs),
                "live_status": next(
                    (l.live_status for l in legs if l.live_status), None
                ),
                "legs": [l.to_dict() for l in legs],
            }
        )
    return grouped
