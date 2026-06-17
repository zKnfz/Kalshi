from __future__ import annotations

import asyncio
import logging
import random
from datetime import datetime, timezone
from typing import Any, Callable

from .analyzer import evaluate_markets
from .client import KalshiClient
from .config import settings
from .models import Event, Market, Opportunity

log = logging.getLogger(__name__)


def _iso_now() -> str:
    return (
        datetime.now(tz=timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )


def _parse_iso(s: str | None) -> datetime | None:
    if not s:
        return None
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


class AnalyzerEngine:
    """Polls Kalshi, scores live markets, and pushes opportunities to listeners.

    On every tick the engine emits ``{snapshot, delta}`` to ``on_update``:

      * ``snapshot`` is the full ranked list (used on first connect / API
        polling clients).
      * ``delta`` is the diff vs. the previous tick: ``added`` /
        ``updated`` opportunities (full objects) and ``removed`` keys
        (``ticker:side`` strings). The WebSocket broker uses this to avoid
        re-sending the full payload every cycle.

    First-seen timestamps are persisted across ticks per ``(ticker, side)``
    so the dashboard can show an age label.
    """

    def __init__(
        self,
        client: KalshiClient | None = None,
        on_update: Callable[[dict[str, Any], dict[str, Any]], None] | None = None,
    ) -> None:
        self._client = client or KalshiClient()
        self._on_update = on_update
        self._task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()
        self._latest_snapshot: dict[str, Any] = {
            "generated_at": None,
            "stats": {},
            "opportunities": [],
            "demo": settings.demo_mode,
        }
        self._first_seen: dict[tuple[str, str], str] = {}
        self._last_fingerprints: dict[tuple[str, str], tuple[Any, ...]] = {}
        self._last_opps_by_key: dict[tuple[str, str], dict[str, Any]] = {}
        self._lock = asyncio.Lock()

    @property
    def latest(self) -> dict[str, Any]:
        return self._latest_snapshot

    async def start(self) -> None:
        if self._task and not self._task.done():
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._run(), name="kalshi-analyzer")

    async def stop(self) -> None:
        self._stop.set()
        if self._task:
            try:
                await asyncio.wait_for(self._task, timeout=5)
            except asyncio.TimeoutError:
                self._task.cancel()
        await self._client.close()

    async def _run(self) -> None:
        log.info(
            "Analyzer engine starting (demo=%s, base_url=%s)",
            settings.demo_mode,
            settings.base_url,
        )
        while not self._stop.is_set():
            try:
                snapshot, delta = await self._tick()
                async with self._lock:
                    self._latest_snapshot = snapshot
                if self._on_update:
                    try:
                        self._on_update(snapshot, delta)
                    except Exception:
                        log.exception("on_update callback failed")
            except Exception:
                log.exception("analyzer tick failed")
            wait_s = self._next_interval()
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=wait_s)
            except asyncio.TimeoutError:
                pass

    def _next_interval(self) -> float:
        base = settings.poll_interval_seconds
        jitter_pct = max(0.0, min(0.95, settings.poll_jitter_pct))
        if jitter_pct <= 0:
            return base
        factor = 1.0 + random.uniform(-jitter_pct, jitter_pct)
        return max(0.5, base * factor)

    async def _tick(self) -> tuple[dict[str, Any], dict[str, Any]]:
        if settings.demo_mode:
            events = _build_demo_events()
            source = "demo"
        else:
            events = await self._load_events_from_kalshi()
            source = "kalshi"

        all_markets = [m for ev in events for m in ev.markets]
        opportunities = evaluate_markets(events)
        opportunities = opportunities[: max(50, settings.max_markets)]

        now_iso = _iso_now()
        now_dt = _parse_iso(now_iso) or datetime.now(tz=timezone.utc)

        for op in opportunities:
            key = op.key()
            first = self._first_seen.get(key)
            if first is None:
                first = now_iso
                self._first_seen[key] = first
            op.first_seen = first
            seen_dt = _parse_iso(first)
            if seen_dt is not None:
                op.age_seconds = max(0.0, (now_dt - seen_dt).total_seconds())

        current_keys = {op.key() for op in opportunities}
        stale = [k for k in self._first_seen if k not in current_keys]
        for k in stale:
            self._first_seen.pop(k, None)

        snapshot = {
            "generated_at": now_iso,
            "source": source,
            "demo": settings.demo_mode,
            "stats": {
                "events_scanned": len(events),
                "markets_scanned": len(all_markets),
                "opportunities_found": len(opportunities),
                "bankroll": settings.bankroll,
                "kelly_fraction_cap": settings.kelly_fraction,
                "max_bet_pct": settings.max_bet_pct,
                "min_edge_pct": settings.min_edge_pct,
                "poll_interval_seconds": settings.poll_interval_seconds,
                "poll_jitter_pct": settings.poll_jitter_pct,
            },
            "opportunities": [op.to_dict() for op in opportunities],
        }

        delta = self._compute_delta(opportunities, snapshot)

        log.info(
            "tick: %d events, %d markets, %d ops (added=%d updated=%d removed=%d) source=%s",
            len(events),
            len(all_markets),
            len(opportunities),
            len(delta["added"]),
            len(delta["updated"]),
            len(delta["removed"]),
            source,
        )
        return snapshot, delta

    def _compute_delta(
        self, opportunities: list[Opportunity], snapshot: dict[str, Any]
    ) -> dict[str, Any]:
        added: list[dict[str, Any]] = []
        updated: list[dict[str, Any]] = []
        new_fingerprints: dict[tuple[str, str], tuple[Any, ...]] = {}
        new_opps_by_key: dict[tuple[str, str], dict[str, Any]] = {}

        for op in opportunities:
            key = op.key()
            fp = op.fingerprint()
            new_fingerprints[key] = fp
            payload = op.to_dict()
            new_opps_by_key[key] = payload
            prev_fp = self._last_fingerprints.get(key)
            if prev_fp is None:
                added.append(payload)
            elif prev_fp != fp:
                updated.append(payload)

        removed = [
            f"{k[0]}:{k[1]}"
            for k in self._last_fingerprints.keys()
            if k not in new_fingerprints
        ]
        self._last_fingerprints = new_fingerprints
        self._last_opps_by_key = new_opps_by_key

        return {
            "type": "delta",
            "generated_at": snapshot["generated_at"],
            "stats": snapshot["stats"],
            "added": added,
            "updated": updated,
            "removed": removed,
        }

    async def _load_events_from_kalshi(self) -> list[Event]:
        try:
            raw_events = await self._client.list_events(
                status="open", with_nested_markets=True
            )
            events = [Event.from_api(e) for e in raw_events]
            if events:
                return events
        except Exception as exc:
            log.warning("events fetch failed (%s); falling back to /markets", exc)

        markets_raw = await self._client.list_markets(status="open")
        markets = [Market.from_api(m) for m in markets_raw]
        bucket: dict[str, list[Market]] = {}
        for m in markets:
            bucket.setdefault(m.event_ticker or m.ticker, []).append(m)
        return [
            Event(
                event_ticker=ev,
                title=ms[0].title,
                category=ms[0].category,
                markets=ms,
            )
            for ev, ms in bucket.items()
        ]


def _build_demo_events() -> list[Event]:
    """Synthetic events used for DEMO_MODE.

    Hand-tuned so all four signal types fire reliably:

      * ``ARB-DEMO``     — yes_no_arbitrage (yes_ask + no_ask < $1).
      * ``FED-DEMO``     — dutch_book_arbitrage (Σ yes_ask < $1).
      * ``WX-DEMO``      — dutch_book_mispricing (mids sum > 1).
      * ``ECON-DEMO``    — fair_value_yes / fair_value_no
                            (last and mid skew the blend below ask).
    """

    now_iso = (
        datetime.now(tz=timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )

    def mk(
        ticker: str,
        event: str,
        title: str,
        yes_bid: int,
        yes_ask: int,
        liquidity: int = 50_000,
        volume_24h: int = 6_000,
        open_interest: int = 12_000,
        last: int | None = None,
        prev: int | None = None,
        no_bid: int | None = None,
        no_ask: int | None = None,
        last_trade_age: float = 5.0,
    ) -> Market:
        if no_bid is None:
            no_bid = max(0, 100 - yes_ask - 1)
        if no_ask is None:
            no_ask = max(yes_ask, 100 - yes_bid + 1)
        mid = (yes_bid + yes_ask) // 2
        last_p = last if last is not None else mid
        return Market(
            ticker=ticker,
            event_ticker=event,
            title=title,
            yes_bid=yes_bid,
            yes_ask=yes_ask,
            no_bid=max(1, min(99, no_bid)),
            no_ask=max(1, min(99, no_ask)),
            last_price=last_p,
            previous_price=prev if prev is not None else mid,
            volume=volume_24h * 4,
            volume_24h=volume_24h,
            open_interest=open_interest,
            liquidity=liquidity,
            status="active",
            last_trade_age_seconds=last_trade_age,
            close_time=now_iso,
            category="DEMO",
        )

    arb = Event(
        event_ticker="ARB-DEMO",
        title="Two-leg arbitrage (demo, after fees)",
        mutually_exclusive=False,
        markets=[
            mk(
                "ARB-DEMO-MAIN",
                "ARB-DEMO",
                "yes_ask + no_ask < $1 (net of fees)",
                yes_bid=34,
                yes_ask=38,
                no_bid=46,
                no_ask=48,
                last=36,
                last_trade_age=3.0,
            ),
        ],
    )

    fed = Event(
        event_ticker="FED-DEMO",
        title="Fed rate decision (demo)",
        mutually_exclusive=True,
        markets=[
            mk("FED-DEMO-HOLD", "FED-DEMO", "Fed holds rates", 36, 38, last=37),
            mk("FED-DEMO-CUT25", "FED-DEMO", "Fed cuts 25bps", 46, 48, last=47),
            mk("FED-DEMO-CUT50", "FED-DEMO", "Fed cuts 50bps", 4, 6, last=5),
        ],
    )

    weather = Event(
        event_ticker="WX-DEMO",
        title="NYC high temp today (demo, mispriced basket)",
        mutually_exclusive=True,
        markets=[
            mk("WX-DEMO-LT60", "WX-DEMO", "High < 60°F", 28, 32, last=30),
            mk("WX-DEMO-60-70", "WX-DEMO", "High 60–70°F", 40, 44, last=42),
            mk("WX-DEMO-70-80", "WX-DEMO", "High 70–80°F", 34, 38, last=36),
            mk("WX-DEMO-GT80", "WX-DEMO", "High > 80°F", 14, 18, last=16),
        ],
    )

    econ = Event(
        event_ticker="ECON-DEMO",
        title="Q-end PCE inflation prints below 2.5% (demo)",
        mutually_exclusive=False,
        markets=[
            mk(
                "ECON-DEMO-PCE",
                "ECON-DEMO",
                "PCE < 2.5%",
                yes_bid=50,
                yes_ask=53,
                last=64,
                prev=62,
                last_trade_age=8.0,
            ),
        ],
    )

    return [arb, fed, weather, econ]
