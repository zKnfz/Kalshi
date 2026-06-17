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


@dataclass
class Opportunity:
    """A scored betting opportunity surfaced by the analyzer."""

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
    extra: dict[str, Any] = field(default_factory=dict)
    generated_at: str = field(
        default_factory=lambda: datetime.now(tz=timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )

    def to_dict(self) -> dict[str, Any]:
        return {
            "ticker": self.ticker,
            "event_ticker": self.event_ticker,
            "title": self.title,
            "side": self.side,
            "strategy": self.strategy,
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
            "rationale": self.rationale,
            "extra": self.extra,
            "generated_at": self.generated_at,
        }
