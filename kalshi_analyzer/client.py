from __future__ import annotations

import asyncio
import logging
from typing import Any, AsyncIterator

import httpx

from .config import settings

log = logging.getLogger(__name__)


class KalshiClient:
    """Thin async client for Kalshi's public Trade API v2 endpoints.

    Only read-only endpoints are used (markets, events, single-market
    orderbooks), so no authentication is required.
    """

    def __init__(self, base_url: str | None = None, timeout: float = 20.0) -> None:
        self.base_url = (base_url or settings.base_url).rstrip("/")
        self._client = httpx.AsyncClient(
            base_url=self.base_url,
            timeout=timeout,
            headers={"User-Agent": "kalshi-edge-analyzer/0.1"},
        )

    async def close(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> "KalshiClient":
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.close()

    async def _get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        for attempt in range(4):
            try:
                resp = await self._client.get(path, params=params)
                resp.raise_for_status()
                return resp.json()
            except (httpx.HTTPError, httpx.TransportError) as exc:
                if attempt == 3:
                    raise
                delay = 2**attempt
                log.warning("GET %s failed (%s); retrying in %ss", path, exc, delay)
                await asyncio.sleep(delay)
        return {}

    async def list_markets(
        self,
        status: str = "open",
        limit: int = 1000,
        max_pages: int = 4,
    ) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        cursor: str | None = None
        for _ in range(max_pages):
            params: dict[str, Any] = {"status": status, "limit": min(limit, 1000)}
            if cursor:
                params["cursor"] = cursor
            data = await self._get("/markets", params=params)
            markets = data.get("markets") or []
            out.extend(markets)
            cursor = data.get("cursor") or None
            if not cursor or not markets:
                break
        return out

    async def list_events(
        self,
        status: str = "open",
        with_nested_markets: bool = True,
        limit: int = 200,
        max_pages: int = 4,
    ) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        cursor: str | None = None
        for _ in range(max_pages):
            params: dict[str, Any] = {
                "status": status,
                "with_nested_markets": str(with_nested_markets).lower(),
                "limit": min(limit, 200),
            }
            if cursor:
                params["cursor"] = cursor
            data = await self._get("/events", params=params)
            events = data.get("events") or []
            out.extend(events)
            cursor = data.get("cursor") or None
            if not cursor or not events:
                break
        return out

    async def get_orderbook(self, ticker: str, depth: int = 10) -> dict[str, Any]:
        data = await self._get(
            f"/markets/{ticker}/orderbook", params={"depth": depth}
        )
        return data.get("orderbook") or {}

    async def stream_orderbooks(
        self, tickers: list[str], concurrency: int = 8
    ) -> AsyncIterator[tuple[str, dict[str, Any]]]:
        sem = asyncio.Semaphore(concurrency)

        async def fetch(t: str) -> tuple[str, dict[str, Any]]:
            async with sem:
                try:
                    return t, await self.get_orderbook(t)
                except Exception as exc:
                    log.debug("orderbook fetch failed for %s: %s", t, exc)
                    return t, {}

        tasks = [asyncio.create_task(fetch(t)) for t in tickers]
        for coro in asyncio.as_completed(tasks):
            yield await coro
