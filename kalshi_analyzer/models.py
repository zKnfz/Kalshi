from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


def _to_cents(v: Any) -> int | None:
    """Coerce Kalshi price fields to integer cents.

    Kalshi exposes prices either as integer cents (``yes_bid``) or as
    dollar-denominated strings (``yes_bid_dollars`` like ``"0.1300"``).
    """

    if v is None:
        return None
    if isinstance(v, bool):
        return None
    if isinstance(v, (int,)):
        return v
    if isinstance(v, float):
        if v <= 1.0001 and v >= -0.0001:
            return int(round(v * 100))
        return int(round(v))
    if isinstance(v, str):
        s = v.strip()
        if not s:
            return None
        try:
            f = float(s)
        except ValueError:
            return None
        if "." in s or abs(f) <= 1.0001:
            return int(round(f * 100))
        return int(round(f))
    return None


def _to_int(v: Any) -> int:
    if v is None:
        return 0
    if isinstance(v, bool):
        return 0
    if isinstance(v, (int,)):
        return v
    if isinstance(v, float):
        return int(round(v))
    if isinstance(v, str):
        s = v.strip()
        if not s:
            return 0
        try:
            return int(round(float(s)))
        except ValueError:
            return 0
    return 0


def _liquidity_to_cents(data: dict[str, Any]) -> int:
    raw = data.get("liquidity")
    if raw not in (None, ""):
        return _to_int(raw)
    dollars = data.get("liquidity_dollars")
    if dollars not in (None, ""):
        try:
            return int(round(float(dollars) * 100))
        except (TypeError, ValueError):
            return 0
    return 0


def _pick(data: dict[str, Any], *names: str) -> Any:
    for n in names:
        if n in data and data[n] not in (None, ""):
            return data[n]
    return None


def _parse_dt(value: Any) -> datetime | None:
    if not value:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(float(value), tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    if isinstance(value, str):
        s = value.strip()
        if not s:
            return None
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        try:
            dt = datetime.fromisoformat(s)
        except ValueError:
            return None
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    return None


def _compute_trade_age_seconds(data: dict[str, Any]) -> float | None:
    raw = _pick(data, "last_trade_time", "last_trade_at", "last_trade_ts")
    dt = _parse_dt(raw)
    if dt is None:
        return None
    delta = datetime.now(tz=timezone.utc) - dt
    return max(0.0, delta.total_seconds())


@dataclass
class Market:
    """Snapshot of a single Kalshi binary market.

    Prices are stored in cents (0-100). All optional fields default to None
    so partial responses from the API do not break the analyzer.
    """

    ticker: str
    event_ticker: str
    title: str
    subtitle: str = ""
    yes_sub_title: str = ""
    no_sub_title: str = ""
    status: str = "active"
    yes_bid: int | None = None
    yes_ask: int | None = None
    no_bid: int | None = None
    no_ask: int | None = None
    last_price: int | None = None
    previous_price: int | None = None
    volume: int = 0
    volume_24h: int = 0
    open_interest: int = 0
    liquidity: int = 0
    last_trade_time: str | None = None
    last_trade_age_seconds: float | None = None
    close_time: str | None = None
    category: str = ""
    raw: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_api(cls, data: dict[str, Any]) -> "Market":
        return cls(
            ticker=data.get("ticker", ""),
            event_ticker=data.get("event_ticker", ""),
            title=data.get("title", ""),
            subtitle=data.get("subtitle") or data.get("yes_sub_title") or "",
            yes_sub_title=data.get("yes_sub_title", ""),
            no_sub_title=data.get("no_sub_title", ""),
            status=data.get("status", "active"),
            yes_bid=_to_cents(_pick(data, "yes_bid", "yes_bid_dollars")),
            yes_ask=_to_cents(_pick(data, "yes_ask", "yes_ask_dollars")),
            no_bid=_to_cents(_pick(data, "no_bid", "no_bid_dollars")),
            no_ask=_to_cents(_pick(data, "no_ask", "no_ask_dollars")),
            last_price=_to_cents(_pick(data, "last_price", "last_price_dollars")),
            previous_price=_to_cents(
                _pick(data, "previous_price", "previous_price_dollars")
            ),
            volume=_to_int(_pick(data, "volume", "volume_fp")),
            volume_24h=_to_int(_pick(data, "volume_24h", "volume_24h_fp")),
            open_interest=_to_int(_pick(data, "open_interest", "open_interest_fp")),
            liquidity=_liquidity_to_cents(data),
            last_trade_time=_pick(
                data, "last_trade_time", "last_trade_at", "last_trade_ts"
            ),
            last_trade_age_seconds=_compute_trade_age_seconds(data),
            close_time=data.get("close_time"),
            category=data.get("category", ""),
            raw=data,
        )

    @property
    def mid_price(self) -> float | None:
        if self.yes_bid is None or self.yes_ask is None:
            return None
        if self.yes_bid <= 0 and self.yes_ask <= 0:
            return None
        return (self.yes_bid + self.yes_ask) / 200.0

    @property
    def spread_cents(self) -> int | None:
        if self.yes_bid is None or self.yes_ask is None:
            return None
        return max(0, self.yes_ask - self.yes_bid)


@dataclass
class Event:
    event_ticker: str
    title: str
    category: str = ""
    mutually_exclusive: bool = False
    markets: list[Market] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_api(cls, data: dict[str, Any]) -> "Event":
        markets_raw = data.get("markets") or []
        return cls(
            event_ticker=data.get("event_ticker", ""),
            title=data.get("title", ""),
            category=data.get("category", ""),
            mutually_exclusive=bool(data.get("mutually_exclusive", False)),
            markets=[Market.from_api(m) for m in markets_raw],
            raw=data,
        )


def _utc_now_iso() -> str:
    return (
        datetime.now(tz=timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )


@dataclass
class Opportunity:
    """A scored betting opportunity surfaced by the analyzer.

    A single market/side may light up under multiple signals (e.g. a leg of
    a dutch-book basket can also be cheap vs. its blended consensus). The
    engine deduplicates per (ticker, side) and merges ``signal_types``
    accordingly; this dataclass is the post-merge shape.
    """

    ticker: str
    event_ticker: str
    title: str
    side: str
    strategy: str
    entry_price: float
    fair_price: float
    edge: float
    edge_pct: float
    kelly_fraction: float
    suggested_stake: float
    expected_value: float
    confidence: float
    score: float
    liquidity: int
    volume_24h: int
    spread_cents: int | None
    close_time: str | None
    rationale: str
    signal_types: list[str] = field(default_factory=list)
    fill_feasible: bool = True
    basket_complete: bool = True
    basket_id: str | None = None
    series_ticker: str = ""
    is_sports: bool = False
    live_status: str | None = None
    game_state: dict[str, Any] | None = None
    model_yes_prob: float | None = None
    fees_per_contract: float = 0.0
    net_edge: float = 0.0
    net_edge_pct: float = 0.0
    last_trade_age_seconds: float | None = None
    first_seen: str = field(default_factory=_utc_now_iso)
    age_seconds: float = 0.0
    extra: dict[str, Any] = field(default_factory=dict)
    generated_at: str = field(default_factory=_utc_now_iso)

    def key(self) -> tuple[str, str]:
        return (self.ticker, self.side)

    def fingerprint(self) -> tuple[Any, ...]:
        """Cheap tuple used by the diff-broadcaster to detect changes."""

        return (
            self.ticker,
            self.side,
            tuple(sorted(self.signal_types)),
            round(self.entry_price, 4),
            round(self.fair_price, 4),
            round(self.edge_pct, 2),
            round(self.net_edge_pct, 2),
            round(self.kelly_fraction, 4),
            round(self.suggested_stake, 2),
            round(self.confidence, 3),
            round(self.score, 3),
            self.fill_feasible,
            self.basket_complete,
            self.basket_id,
            self.is_sports,
            self.live_status,
            self.model_yes_prob,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "ticker": self.ticker,
            "event_ticker": self.event_ticker,
            "title": self.title,
            "side": self.side,
            "strategy": self.strategy,
            "signal_types": list(self.signal_types),
            "fill_feasible": self.fill_feasible,
            "basket_complete": self.basket_complete,
            "basket_id": self.basket_id,
            "series_ticker": self.series_ticker,
            "is_sports": self.is_sports,
            "live_status": self.live_status,
            "game_state": self.game_state,
            "model_yes_prob": (
                round(self.model_yes_prob, 4) if self.model_yes_prob is not None else None
            ),
            "fees_per_contract": round(self.fees_per_contract, 4),
            "net_edge": round(self.net_edge, 4),
            "net_edge_pct": round(self.net_edge_pct, 2),
            "entry_price": round(self.entry_price, 4),
            "fair_price": round(self.fair_price, 4),
            "edge": round(self.edge, 4),
            "edge_pct": round(self.edge_pct, 2),
            "kelly_fraction": round(self.kelly_fraction, 4),
            "suggested_stake": round(self.suggested_stake, 2),
            "expected_value": round(self.expected_value, 4),
            "confidence": round(self.confidence, 3),
            "score": round(self.score, 3),
            "liquidity": self.liquidity,
            "volume_24h": self.volume_24h,
            "spread_cents": self.spread_cents,
            "close_time": self.close_time,
            "last_trade_age_seconds": (
                round(self.last_trade_age_seconds, 1)
                if self.last_trade_age_seconds is not None
                else None
            ),
            "first_seen": self.first_seen,
            "age_seconds": round(self.age_seconds, 1),
            "rationale": self.rationale,
            "extra": self.extra,
            "generated_at": self.generated_at,
        }
