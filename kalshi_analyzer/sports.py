"""Sports market detection and live-event helpers."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Iterable

from .config import settings


def sports_prefixes() -> tuple[str, ...]:
    raw = settings.sports_prefixes
    if isinstance(raw, str):
        return tuple(p.strip().upper() for p in raw.split(",") if p.strip())
    return tuple(str(p).upper() for p in raw)


def is_sports_market(ticker: str, event_ticker: str = "") -> bool:
    if not settings.sports_enabled:
        return False
    t = (ticker or "").upper()
    e = (event_ticker or "").upper()
    for prefix in sports_prefixes():
        if (
            t.startswith(f"{prefix}-")
            or t.startswith(prefix)
            or e.startswith(f"{prefix}-")
            or e.startswith(prefix)
        ):
            return True
    return False


def series_ticker_for(ticker: str, event_ticker: str = "") -> str:
    """Best-effort series ticker — event_ticker when present, else ticker stem."""

    if event_ticker:
        return event_ticker
    if "-" in ticker:
        return ticker.rsplit("-", 1)[0]
    return ticker


def live_status(close_time: str | None, *, now: datetime | None = None) -> str | None:
    """Return ``LIVE`` if close is within 3h, ``TODAY`` if same UTC day and >3h."""

    if not close_time:
        return None
    now = now or datetime.now(tz=timezone.utc)
    try:
        s = close_time
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        else:
            dt = dt.astimezone(timezone.utc)
    except ValueError:
        return None
    delta = (dt - now).total_seconds()
    if delta < 0:
        return None
    if delta <= 3 * 3600:
        return "LIVE"
    if dt.date() == now.date():
        return "TODAY"
    return None
