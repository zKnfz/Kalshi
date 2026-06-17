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
        """Retry policy:

        * 429 / 503 → dedicated exponential backoff up to 6 attempts
          starting at 4s (caps at 64s) to play nicely with Kalshi rate
          limiters.
        * Other 5xx / transport errors → 4 attempts at 1s/2s/4s.
        * 4xx other than 429 → no retry (the request is bad).
        """

        rate_attempt = 0
        max_rate_attempts = 6
        for attempt in range(4):
            try:
                resp = await self._client.get(path, params=params)
                if resp.status_code in (429, 503):
                    if rate_attempt >= max_rate_attempts:
                        resp.raise_for_status()
                    retry_after = resp.headers.get("retry-after")
                    base = float(retry_after) if retry_after else 4.0 * (2**rate_attempt)
                    delay = min(64.0, base)
                    rate_attempt += 1
                    log.warning(
                        "GET %s -> %d; backing off %.1fs (rate-attempt %d/%d)",
                        path,
                        resp.status_code,
                        delay,
                        rate_attempt,
                        max_rate_attempts,
                    )
                    await asyncio.sleep(delay)
                    continue
                if 400 <= resp.status_code < 500:
                    resp.raise_for_status()
                resp.raise_for_status()
                return resp.json()
            except httpx.HTTPStatusError as exc:
                status = exc.response.status_code if exc.response else 0
                if status in (429, 503):
                    continue
                if status and 400 <= status < 500:
                    raise
                if attempt == 3:
                    raise
                delay = 2**attempt
                log.warning("GET %s failed (%s); retrying in %ss", path, exc, delay)
                await asyncio.sleep(delay)
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
