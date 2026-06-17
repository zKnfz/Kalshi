from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

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


def _env_csv_strs(name: str, default: tuple[str, ...]) -> tuple[str, ...]:
    raw = os.getenv(name)
    if not raw:
        return default
    out = tuple(p.strip() for p in raw.split(",") if p.strip())
    return out if out else default


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
    use_native_ws: bool = _env_bool("USE_NATIVE_WS", True)
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
    arb_fill_timeout_seconds: float = _env_float("ARB_FILL_TIMEOUT_SECONDS", 2.0)
    sports_enabled: bool = _env_bool("SPORTS_ENABLED", True)
    sports_prefixes: tuple[str, ...] = field(
        default_factory=lambda: _env_csv_strs(
            "SPORTS_PREFIXES",
            ("NFL", "NBA", "MLB", "NHL", "CFB", "SOC", "GOLF", "TEN", "MMA"),
        )
    )
    sports_only_mode: bool = _env_bool("SPORTS_ONLY_MODE", False)
    sports_min_volume_24h: int = _env_int("SPORTS_MIN_VOLUME_24H", 1000)
    sports_model_enabled: bool = _env_bool("SPORTS_MODEL_ENABLED", False)
    espn_poll_seconds: int = _env_int("ESPN_POLL_SECONDS", 30)
    espn_backoff_seconds: int = _env_int("ESPN_BACKOFF_SECONDS", 300)
    sports_match_path: str = os.getenv("SPORTS_MATCH_PATH", "./sports_match.json")
    sports_model_min_confidence: float = _env_float("SPORTS_MODEL_MIN_CONFIDENCE", 0.40)
    sports_model_min_edge_pct: float = _env_float("SPORTS_MODEL_MIN_EDGE_PCT", 3.0)


settings = Settings()


def credentials_configured() -> bool:
    key = (settings.kalshi_key_id or "").strip()
    path = (settings.kalshi_private_key_path or "").strip()
    return bool(key and path and Path(path).exists())


def effective_native_ws() -> bool:
    """Auto-enable the native push feed once RSA credentials are configured.

    Set ``USE_NATIVE_WS=false`` to force REST polling even when keys exist.
    """

    if not credentials_configured():
        return False
    return settings.use_native_ws


def strategy_bankroll_cap(strategy: str) -> float:
    """Per-strategy sub-cap on the bankroll dollars available to a single bet.

    Arbitrage and fair-value plays draw from separate sub-pools so that one
    strategy can't drain the bankroll out from under the other.
    """

    if strategy.endswith("_arbitrage") or strategy.endswith("_mispricing"):
        return settings.bankroll * settings.arb_bankroll_share
    if strategy.startswith("fair_value"):
        return settings.bankroll * settings.fairvalue_bankroll_share
    if strategy.startswith("sports_model"):
        return settings.bankroll * settings.fairvalue_bankroll_share
    return settings.bankroll * settings.fairvalue_bankroll_share
