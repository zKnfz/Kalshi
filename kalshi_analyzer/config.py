from __future__ import annotations

import os
from dataclasses import dataclass, field

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


def _env_csv_floats(name: str, default: tuple[float, ...]) -> tuple[float, ...]:
    raw = os.getenv(name)
    if not raw:
        return default
    out: list[float] = []
    for piece in raw.split(","):
        piece = piece.strip()
        if not piece:
            continue
        try:
            out.append(float(piece))
        except ValueError:
            return default
    return tuple(out) if out else default


@dataclass
class Settings:
    base_url: str = os.getenv(
        "KALSHI_BASE_URL", "https://api.elections.kalshi.com/trade-api/v2"
    )
    poll_interval_seconds: float = _env_float("POLL_INTERVAL_SECONDS", 5.0)
    poll_jitter_pct: float = _env_float("POLL_JITTER_PCT", 0.15)
    orderbook_refresh_seconds: float = _env_float("ORDERBOOK_REFRESH_SECONDS", 15.0)
    max_markets: int = _env_int("MAX_MARKETS", 400)
    min_liquidity_cents: int = _env_int("MIN_LIQUIDITY_CENTS", 2000)
    max_spread_cents: int = _env_int("MAX_SPREAD_CENTS", 15)
    min_volume_24h: int = _env_int("MIN_VOLUME_24H", 0)
    min_fill_qty: int = _env_int("MIN_FILL_QTY", 25)
    stale_last_age_seconds: int = _env_int("STALE_LAST_AGE_SECONDS", 60)
    recency_weights: tuple[float, float, float] = field(
        default_factory=lambda: _env_csv_floats(
            "RECENCY_WEIGHTS", (0.50, 0.35, 0.15)
        )  # type: ignore[arg-type]
    )
    demo_mode: bool = _env_bool("DEMO_MODE", False)
    host: str = os.getenv("HOST", "0.0.0.0")
    port: int = _env_int("PORT", 8000)
    bankroll: float = _env_float("BANKROLL", 1000.0)
    kelly_fraction: float = _env_float("KELLY_FRACTION", 0.25)
    max_bet_pct: float = _env_float("MAX_BET_PCT", 0.05)
    arb_bankroll_share: float = _env_float("ARB_BANKROLL_SHARE", 0.60)
    fairvalue_bankroll_share: float = _env_float("FAIRVALUE_BANKROLL_SHARE", 0.30)
    min_edge_pct: float = _env_float("MIN_EDGE_PCT", 0.5)
    min_net_edge_pct: float = _env_float("MIN_NET_EDGE_PCT", 1.0)
    assume_taker_fees: bool = _env_bool("ASSUME_TAKER_FEES", True)
    use_native_ws: bool = _env_bool("USE_NATIVE_WS", False)
    kalshi_key_id: str = os.getenv("KALSHI_KEY_ID", "")
    kalshi_private_key_path: str = os.getenv("KALSHI_PRIVATE_KEY_PATH", "")
    execution_mode: str = os.getenv("EXECUTION_MODE", "off")
    max_daily_loss: float = _env_float("MAX_DAILY_LOSS", 50.0)
    kill_switch: bool = _env_bool("KILL_SWITCH", False)
    kill_switch_file: str = os.getenv("KILL_SWITCH_FILE", "/tmp/kalshi-kill-switch")
    paper_slippage_cents: float = _env_float("PAPER_SLIPPAGE_CENTS", 1.0)
    paper_state_path: str = os.getenv("PAPER_STATE_PATH", "./paper_state.json")
    position_state_path: str = os.getenv("POSITION_STATE_PATH", "./positions.json")
    alert_min_edge_pct: float = _env_float("ALERT_MIN_EDGE_PCT", 5.0)
    alert_cooldown_seconds: float = _env_float("ALERT_COOLDOWN_SECONDS", 300.0)
    telegram_bot_token: str = os.getenv("TELEGRAM_BOT_TOKEN", "")
    telegram_chat_id: str = os.getenv("TELEGRAM_CHAT_ID", "")
    discord_webhook_url: str = os.getenv("DISCORD_WEBHOOK_URL", "")
    polymarket_enabled: bool = _env_bool("POLYMARKET_ENABLED", False)
    polymarket_base_url: str = os.getenv(
        "POLYMARKET_BASE_URL", "https://gamma-api.polymarket.com"
    )
    polymarket_clob_url: str = os.getenv(
        "POLYMARKET_CLOB_URL", "https://clob.polymarket.com"
    )
    polymarket_match_path: str = os.getenv(
        "POLYMARKET_MATCH_PATH", "./polymarket_map.json"
    )
    backtest_snapshot_path: str = os.getenv(
        "BACKTEST_SNAPSHOT_PATH", "./snapshots.jsonl"
    )
    backtest_recording: bool = _env_bool("BACKTEST_RECORDING", False)


settings = Settings()


def strategy_bankroll_cap(strategy: str) -> float:
    """Per-strategy sub-cap on the bankroll dollars available to a single bet.

    Arbitrage and fair-value plays draw from separate sub-pools so that one
    strategy can't drain the bankroll out from under the other.
    """

    if strategy.endswith("_arbitrage") or strategy.endswith("_mispricing"):
        return settings.bankroll * settings.arb_bankroll_share
    if strategy.startswith("fair_value"):
        return settings.bankroll * settings.fairvalue_bankroll_share
    return settings.bankroll * settings.fairvalue_bankroll_share
