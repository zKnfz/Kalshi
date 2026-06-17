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


class AnalyzerEngine:
    """Polls Kalshi, scores live markets, and pushes opportunities to listeners."""

    def __init__(
        self,
        client: KalshiClient | None = None,
        on_update: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        self._client = client or KalshiClient()
        self._on_update = on_update
        self._task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()
        self._latest: dict[str, Any] = {
            "generated_at": None,
            "stats": {},
            "opportunities": [],
            "demo": settings.demo_mode,
        }
        self._lock = asyncio.Lock()

    @property
    def latest(self) -> dict[str, Any]:
        return self._latest

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
        log.info("Analyzer engine starting (demo=%s)", settings.demo_mode)
        while not self._stop.is_set():
            try:
                snapshot = await self._tick()
                async with self._lock:
                    self._latest = snapshot
                if self._on_update:
                    try:
                        self._on_update(snapshot)
                    except Exception:
                        log.exception("on_update callback failed")
            except Exception:
                log.exception("analyzer tick failed")
            try:
                await asyncio.wait_for(
                    self._stop.wait(), timeout=settings.poll_interval_seconds
                )
            except asyncio.TimeoutError:
                pass

    async def _tick(self) -> dict[str, Any]:
        if settings.demo_mode:
            events = _build_demo_events()
            source = "demo"
        else:
            events = await self._load_events_from_kalshi()
            source = "kalshi"

        all_markets = [m for ev in events for m in ev.markets]
        opportunities = evaluate_markets(events)
        opportunities = opportunities[: max(50, settings.max_markets)]

        snapshot = {
            "generated_at": datetime.now(tz=timezone.utc).isoformat(timespec="seconds"),
            "source": source,
            "demo": settings.demo_mode,
            "stats": {
                "events_scanned": len(events),
                "markets_scanned": len(all_markets),
                "opportunities_found": len(opportunities),
                "bankroll": settings.bankroll,
                "kelly_fraction_cap": settings.kelly_fraction,
            },
            "opportunities": [op.to_dict() for op in opportunities],
        }
        log.info(
            "tick: %d events, %d markets, %d opportunities (source=%s)",
            len(events),
            len(all_markets),
            len(opportunities),
            source,
        )
        return snapshot

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
    """Synthetic markets used when DEMO_MODE=true or Kalshi is unreachable."""

    random.seed(int(datetime.now(tz=timezone.utc).timestamp()) // 5)

    def mk(
        ticker: str,
        event: str,
        title: str,
        yes_bid: int,
        yes_ask: int,
        liquidity: int = 25_000,
        volume_24h: int = 3_000,
        last: int | None = None,
    ) -> Market:
        no_bid = max(0, 100 - yes_ask - 1)
        no_ask = max(yes_ask, 100 - yes_bid + 1)
        return Market(
            ticker=ticker,
            event_ticker=event,
            title=title,
            yes_bid=yes_bid,
            yes_ask=yes_ask,
            no_bid=no_bid,
            no_ask=min(99, no_ask),
            last_price=last if last is not None else (yes_bid + yes_ask) // 2,
            previous_price=(yes_bid + yes_ask) // 2,
            volume=volume_24h * 4,
            volume_24h=volume_24h,
            open_interest=volume_24h * 3,
            liquidity=liquidity,
            status="active",
            close_time=(
                datetime.now(tz=timezone.utc).replace(microsecond=0).isoformat()
            ),
            category="DEMO",
        )

    fed = Event(
        event_ticker="FED-DEMO",
        title="Fed rate decision (demo)",
        mutually_exclusive=True,
        markets=[
            mk("FED-DEMO-HOLD", "FED-DEMO", "Fed holds rates", 38, 41),
            mk("FED-DEMO-CUT25", "FED-DEMO", "Fed cuts 25bps", 48, 51),
            mk("FED-DEMO-CUT50", "FED-DEMO", "Fed cuts 50bps", 4, 6),
        ],
    )

    nba = Event(
        event_ticker="NBA-DEMO",
        title="NBA tonight (demo)",
        mutually_exclusive=False,
        markets=[
            mk("NBA-DEMO-LAL", "NBA-DEMO", "Lakers beat Celtics", 55, 58, last=62),
            mk("NBA-DEMO-OT", "NBA-DEMO", "Game goes to OT", 18, 22),
        ],
    )

    weather = Event(
        event_ticker="WX-DEMO",
        title="NYC high temp today (demo)",
        mutually_exclusive=True,
        markets=[
            mk("WX-DEMO-LT60", "WX-DEMO", "High < 60°F", 20, 24),
            mk("WX-DEMO-60-70", "WX-DEMO", "High 60–70°F", 30, 33),
            mk("WX-DEMO-70-80", "WX-DEMO", "High 70–80°F", 25, 28),
            mk("WX-DEMO-GT80", "WX-DEMO", "High > 80°F", 8, 10),
        ],
    )

    return [fed, nba, weather]
