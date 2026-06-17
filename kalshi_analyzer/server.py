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
    def __init__(self) -> None:
        self._clients: set[WebSocket] = set()
        self._lock = asyncio.Lock()

    async def connect(self, ws: WebSocket) -> None:
        await ws.accept()
        async with self._lock:
            self._clients.add(ws)

    async def disconnect(self, ws: WebSocket) -> None:
        async with self._lock:
            self._clients.discard(ws)

    async def broadcast(self, payload: dict) -> None:
        if not self._clients:
            return
        msg = json.dumps(payload)
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


def _schedule_broadcast(snapshot: dict) -> None:
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    loop.create_task(broker.broadcast(snapshot))


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
            "base_url": settings.base_url,
        }
    )


@app.get("/api/opportunities")
async def opportunities() -> JSONResponse:
    if not engine:
        return JSONResponse({"opportunities": [], "stats": {}}, status_code=503)
    return JSONResponse(engine.latest)


@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket) -> None:
    await broker.connect(ws)
    try:
        if engine and engine.latest.get("opportunities") is not None:
            await ws.send_text(json.dumps(engine.latest))
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
