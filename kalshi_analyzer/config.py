from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


@dataclass(frozen=True)
class Settings:
    base_url: str = os.getenv(
        "KALSHI_BASE_URL", "https://api.elections.kalshi.com/trade-api/v2"
    )
    poll_interval_seconds: float = _env_float("POLL_INTERVAL_SECONDS", 5.0)
    orderbook_refresh_seconds: float = _env_float("ORDERBOOK_REFRESH_SECONDS", 15.0)
    max_markets: int = _env_int("MAX_MARKETS", 400)
    min_liquidity_cents: int = _env_int("MIN_LIQUIDITY_CENTS", 2000)
    demo_mode: bool = _env_bool("DEMO_MODE", False)
    host: str = os.getenv("HOST", "0.0.0.0")
    port: int = _env_int("PORT", 8000)
    bankroll: float = _env_float("BANKROLL", 1000.0)
    kelly_fraction: float = _env_float("KELLY_FRACTION", 0.25)


settings = Settings()
