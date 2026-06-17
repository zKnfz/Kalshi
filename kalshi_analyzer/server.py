from __future__ import annotations

import asyncio
import json
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .config import settings
from .engine import AnalyzerEngine

log = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"


class WebSocketBroker:
    """Tiny pub-sub for the live dashboard.

    Sends each connecting client the full snapshot once, then only
    deltas (``added`` / ``updated`` / ``removed``) thereafter — so a
    1000-row opportunity table doesn't get re-serialized every poll.
    """

    def __init__(self) -> None:
        self._clients: set[WebSocket] = set()
        self._lock = asyncio.Lock()

    async def connect(self, ws: WebSocket, snapshot: dict | None) -> None:
        await ws.accept()
        async with self._lock:
            self._clients.add(ws)
        if snapshot is not None:
            try:
                await ws.send_text(
                    json.dumps({"type": "snapshot", "snapshot": snapshot})
                )
            except Exception:
                await self.disconnect(ws)

    async def disconnect(self, ws: WebSocket) -> None:
        async with self._lock:
            self._clients.discard(ws)

    async def broadcast_delta(self, delta: dict) -> None:
        if not self._clients:
            return
        if not delta["added"] and not delta["updated"] and not delta["removed"]:
            heartbeat = {
                "type": "heartbeat",
                "generated_at": delta.get("generated_at"),
                "stats": delta.get("stats"),
            }
            msg = json.dumps(heartbeat)
        else:
            msg = json.dumps(delta)
        async with self._lock:
            dead: list[WebSocket] = []
            for ws in list(self._clients):
                try:
                    await ws.send_text(msg)
                except Exception:
                    dead.append(ws)
            for ws in dead:
                self._clients.discard(ws)


broker = WebSocketBroker()
engine: AnalyzerEngine | None = None


def _schedule_broadcast(snapshot: dict, delta: dict) -> None:
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    loop.create_task(broker.broadcast_delta(delta))


@asynccontextmanager
async def lifespan(app: FastAPI):
    global engine
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    engine = AnalyzerEngine(on_update=_schedule_broadcast)
    await engine.start()
    try:
        yield
    finally:
        if engine:
            await engine.stop()


app = FastAPI(title="Kalshi Edge Analyzer", lifespan=lifespan)


@app.get("/api/health")
async def health() -> JSONResponse:
    return JSONResponse(
        {
            "status": "ok",
            "demo_mode": settings.demo_mode,
            "poll_interval_seconds": settings.poll_interval_seconds,
            "poll_jitter_pct": settings.poll_jitter_pct,
            "max_bet_pct": settings.max_bet_pct,
            "kelly_fraction_cap": settings.kelly_fraction,
            "min_edge_pct": settings.min_edge_pct,
            "max_spread_cents": settings.max_spread_cents,
            "min_volume_24h": settings.min_volume_24h,
            "min_fill_qty": settings.min_fill_qty,
            "base_url": settings.base_url,
        }
    )


@app.get("/api/opportunities")
async def opportunities() -> JSONResponse:
    if not engine:
        return JSONResponse({"opportunities": [], "stats": {}}, status_code=503)
    return JSONResponse(engine.latest)


@app.get("/api/paper")
async def paper_state() -> JSONResponse:
    if not engine or engine._paper is None:
        return JSONResponse({"enabled": False}, status_code=200)
    return JSONResponse(
        {"enabled": True, "ledger": engine._paper.snapshot()}
    )


@app.get("/api/execution")
async def execution_state() -> JSONResponse:
    if not engine:
        return JSONResponse({"available": False}, status_code=503)
    return JSONResponse({"available": True, "stats": engine._executor.stats()})


@app.post("/api/kill-switch")
async def trip_kill_switch() -> JSONResponse:
    from .execution import write_kill_switch

    path = write_kill_switch()
    return JSONResponse({"tripped": True, "path": path})


@app.delete("/api/kill-switch")
async def clear_kill_switch_endpoint() -> JSONResponse:
    from .execution import clear_kill_switch

    clear_kill_switch()
    return JSONResponse({"tripped": False})


@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket) -> None:
    snap = engine.latest if engine else None
    await broker.connect(ws, snap)
    try:
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        await broker.disconnect(ws)
    except Exception:
        await broker.disconnect(ws)


if STATIC_DIR.exists():
    app.mount(
        "/static",
        StaticFiles(directory=str(STATIC_DIR)),
        name="static",
    )

    @app.get("/")
    async def index() -> FileResponse:
        return FileResponse(str(STATIC_DIR / "index.html"))
