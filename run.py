"""Launcher for the Kalshi edge analyzer web app."""

from __future__ import annotations

import uvicorn

from kalshi_analyzer.config import settings


def main() -> None:
    uvicorn.run(
        "kalshi_analyzer.server:app",
        host=settings.host,
        port=settings.port,
        log_level="info",
        reload=False,
    )


if __name__ == "__main__":
    main()
