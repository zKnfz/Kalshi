"""Kalshi Trade API v2 native WebSocket client.

Connects to ``wss://<host>/trade-api/ws/v2`` (auth required), subscribes
to ``ticker_v2`` + ``orderbook_delta`` channels for a list of tickers,
and emits incremental price updates as they arrive.

This is **opt-in** (``USE_NATIVE_WS=true`` + credentials in env). When
disabled the engine continues to use REST polling. The protocol used
here matches Kalshi's published asyncapi schema as of June 2026:

    {"id": <int>, "cmd": "subscribe",
     "params": {"channels": ["ticker_v2","orderbook_delta"],
                "market_tickers": [...]}}

Incoming messages have a ``type`` discriminator (``subscribed``,
``ticker_v2``, ``orderbook_snapshot``, ``orderbook_delta``, ``error``,
``ping``); we forward the price-bearing types to the engine via the
``on_update`` callback as normalized dicts that look enough like the
REST market payload for ``Market.from_api`` to consume them.
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
from typing import Any, Awaitable, Callable
from urllib.parse import urlparse

import websockets

from .auth import KalshiAuth

log = logging.getLogger(__name__)


def _http_to_ws_base(base_url: str) -> str:
    u = urlparse(base_url)
    scheme = "wss" if u.scheme == "https" else "ws"
    path = u.path.rstrip("/")
    if path.endswith("/v2"):
        path = path[:-3] + "/ws/v2"
    elif "/trade-api" in path:
        path = path.split("/trade-api")[0] + "/trade-api/ws/v2"
    else:
        path = "/trade-api/ws/v2"
    return f"{scheme}://{u.netloc}{path}"


class KalshiWebSocket:
    """Background-task style WS client.

    Use::

        ws = KalshiWebSocket(rest_base_url=..., auth=auth,
                             on_update=handler)
        await ws.start(tickers=[...])
        ...
        await ws.stop()

    ``on_update`` receives one dict per ticker that looks like a slim
    REST market payload (``ticker``, ``yes_bid``, ``yes_ask``,
    ``no_bid``, ``no_ask``, ``last_price``, ...) and a ``_source``
    field set to ``"ws"`` so the engine can tell it apart from REST.
    """

    def __init__(
        self,
        *,
        rest_base_url: str,
        auth: KalshiAuth,
        on_update: Callable[[dict[str, Any]], Awaitable[None] | None],
        reconnect_initial_delay: float = 2.0,
        reconnect_max_delay: float = 60.0,
    ) -> None:
        self._url = _http_to_ws_base(rest_base_url)
        self._auth = auth
        self._on_update = on_update
        self._stop = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._reconnect_initial_delay = reconnect_initial_delay
        self._reconnect_max_delay = reconnect_max_delay
        self._next_id = 1
        self._tickers: list[str] = []
        self._best: dict[str, dict[str, Any]] = {}

    @property
    def url(self) -> str:
        return self._url

    async def start(self, tickers: list[str]) -> None:
        self._tickers = list(tickers)
        if self._task and not self._task.done():
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._run(), name="kalshi-ws")

    async def stop(self) -> None:
        self._stop.set()
        if self._task:
            try:
                await asyncio.wait_for(self._task, timeout=5.0)
            except asyncio.TimeoutError:
                self._task.cancel()

    async def _run(self) -> None:
        delay = self._reconnect_initial_delay
        while not self._stop.is_set():
            try:
                await self._connect_and_pump()
                delay = self._reconnect_initial_delay
            except Exception as exc:
                log.warning("ws session ended (%s); reconnecting in %.1fs", exc, delay)
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=delay)
                except asyncio.TimeoutError:
                    pass
                delay = min(self._reconnect_max_delay, delay * 2 + random.uniform(0, 1))

    async def _connect_and_pump(self) -> None:
        headers = self._auth.signed_headers("GET", "/trade-api/ws/v2")
        log.info("native ws connecting %s", self._url)
        async with websockets.connect(
            self._url,
            additional_headers=headers,
            ping_interval=20,
            ping_timeout=20,
            max_size=4 * 1024 * 1024,
        ) as conn:
            await self._send_subscribe(conn, self._tickers)
            async for raw in conn:
                if self._stop.is_set():
                    break
                try:
                    msg = json.loads(raw)
                except (TypeError, ValueError):
                    continue
                await self._handle_message(msg)

    async def _send_subscribe(self, conn, tickers: list[str]) -> None:
        chunk_size = 100
        for i in range(0, max(1, len(tickers)), chunk_size):
            chunk = tickers[i : i + chunk_size]
            cmd = {
                "id": self._next_id,
                "cmd": "subscribe",
                "params": {
                    "channels": ["ticker_v2", "orderbook_delta"],
                    "market_tickers": chunk,
                },
            }
            self._next_id += 1
            await conn.send(json.dumps(cmd))

    async def _handle_message(self, msg: dict[str, Any]) -> None:
        mtype = msg.get("type") or msg.get("channel")
        if mtype == "error":
            log.warning("ws error: %s", msg)
            return
        if mtype in ("subscribed", "ack"):
            return
        payload = msg.get("msg") or msg.get("data") or msg
        ticker = (
            payload.get("market_ticker")
            or payload.get("ticker")
            or msg.get("market_ticker")
        )
        if not ticker:
            return

        cur = self._best.setdefault(ticker, {"ticker": ticker})
        if mtype == "ticker_v2":
            for src, dst in (
                ("yes_bid", "yes_bid"),
                ("yes_ask", "yes_ask"),
                ("no_bid", "no_bid"),
                ("no_ask", "no_ask"),
                ("last_price", "last_price"),
                ("yes_bid_dollars", "yes_bid_dollars"),
                ("yes_ask_dollars", "yes_ask_dollars"),
                ("no_bid_dollars", "no_bid_dollars"),
                ("no_ask_dollars", "no_ask_dollars"),
                ("last_price_dollars", "last_price_dollars"),
                ("volume", "volume"),
                ("volume_24h", "volume_24h"),
                ("open_interest", "open_interest"),
                ("ts", "last_trade_time"),
            ):
                if src in payload:
                    cur[dst] = payload[src]
        elif mtype in ("orderbook_snapshot", "orderbook_delta"):
            yes_bids = payload.get("yes") or []
            no_bids = payload.get("no") or []
            if yes_bids:
                best_yes = max(yes_bids, key=lambda p: float(p[0]))
                cur["yes_bid"] = int(round(float(best_yes[0]) * 100))
                cur["yes_ask"] = max(0, 100 - cur["yes_bid"]) if cur.get("yes_ask") is None else cur["yes_ask"]
            if no_bids:
                best_no = max(no_bids, key=lambda p: float(p[0]))
                cur["no_bid"] = int(round(float(best_no[0]) * 100))
        else:
            return

        cur["_source"] = "ws"
        try:
            res = self._on_update(cur)
            if asyncio.iscoroutine(res):
                await res
        except Exception:
            log.exception("on_update from ws handler failed")
