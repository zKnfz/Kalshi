"""Telegram and Discord webhook alerts for high-conviction opportunities.

The dispatcher is registered with ``AnalyzerEngine.on_update`` and gets
called once per tick. For every opportunity whose net edge exceeds
``ALERT_MIN_EDGE_PCT`` it sends a webhook message — with per-key
rate limiting so the same opportunity can't spam the channel every
poll cycle (``ALERT_COOLDOWN_SECONDS``).

Both transports are best-effort: a failed webhook is logged and the
opportunity is *not* retried on the next tick (it's already been
deduplicated in the cooldown table).
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Any

import httpx

from .config import settings

log = logging.getLogger(__name__)


def _format_opportunity_text(op: dict[str, Any]) -> str:
    sig = ", ".join(op.get("signal_types") or [op.get("strategy", "?")])
    age = op.get("last_trade_age_seconds")
    stale = ""
    if age is not None and age > 60:
        stale = f" (last trade {age/60:.0f}m ago)"
    parts = [
        f"⚡ {op.get('title') or op.get('ticker')}",
        f"  ticker: {op.get('ticker')}  side: {op.get('side')}",
        f"  signals: {sig}",
        f"  entry: {op.get('entry_price', 0):.2f}  fair: {op.get('fair_price', 0):.2f}",
        f"  edge: {op.get('edge_pct', 0):.2f}%  net edge: {op.get('net_edge_pct', 0):.2f}%",
        f"  fees: ${op.get('fees_per_contract', 0):.4f}/contract",
        f"  Kelly: {op.get('kelly_fraction', 0)*100:.2f}%  stake≈${op.get('suggested_stake', 0):.2f}{stale}",
    ]
    return "\n".join(parts)


def _markdown_for_telegram(op: dict[str, Any]) -> str:
    return _format_opportunity_text(op)


def _embed_for_discord(op: dict[str, Any]) -> dict[str, Any]:
    color = 0xF472B6 if "arbitrage" in (op.get("strategy") or "") else 0x60A5FA
    return {
        "username": "Kalshi Edge Analyzer",
        "embeds": [
            {
                "title": op.get("title") or op.get("ticker"),
                "color": color,
                "description": _format_opportunity_text(op),
                "url": f"https://kalshi.com/markets/{op.get('ticker', '')}",
                "timestamp": op.get("generated_at"),
            }
        ],
    }


@dataclass
class AlertDispatcher:
    """Holds the cooldown state and chooses transports based on env."""

    cooldown_seconds: float
    min_edge_pct: float
    _last_sent: dict[str, float]
    _client: httpx.AsyncClient

    @classmethod
    def from_settings(cls) -> "AlertDispatcher":
        return cls(
            cooldown_seconds=settings.alert_cooldown_seconds,
            min_edge_pct=settings.alert_min_edge_pct,
            _last_sent={},
            _client=httpx.AsyncClient(timeout=10.0),
        )

    async def close(self) -> None:
        await self._client.aclose()

    @property
    def enabled(self) -> bool:
        return bool(
            (settings.telegram_bot_token and settings.telegram_chat_id)
            or settings.discord_webhook_url
        )

    def _candidates(self, snapshot: dict[str, Any]) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        now = time.time()
        for op in snapshot.get("opportunities", []):
            net = op.get("net_edge_pct") or 0.0
            if net < self.min_edge_pct:
                continue
            key = f"{op.get('ticker')}:{op.get('side')}:{op.get('strategy')}"
            last = self._last_sent.get(key, 0.0)
            if now - last < self.cooldown_seconds:
                continue
            self._last_sent[key] = now
            out.append(op)
        return out

    async def dispatch(self, snapshot: dict[str, Any]) -> int:
        if not self.enabled:
            return 0
        candidates = self._candidates(snapshot)
        if not candidates:
            return 0

        tasks: list[asyncio.Task] = []
        for op in candidates:
            if settings.telegram_bot_token and settings.telegram_chat_id:
                tasks.append(asyncio.create_task(self._send_telegram(op)))
            if settings.discord_webhook_url:
                tasks.append(asyncio.create_task(self._send_discord(op)))
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        return len(candidates)

    async def _send_telegram(self, op: dict[str, Any]) -> None:
        url = (
            f"https://api.telegram.org/bot{settings.telegram_bot_token}/sendMessage"
        )
        body = {
            "chat_id": settings.telegram_chat_id,
            "text": _markdown_for_telegram(op),
            "disable_web_page_preview": True,
        }
        try:
            resp = await self._client.post(url, json=body)
            if resp.status_code >= 400:
                log.warning(
                    "telegram alert failed status=%d body=%s",
                    resp.status_code,
                    resp.text[:200],
                )
        except Exception as exc:
            log.warning("telegram alert exception: %s", exc)

    async def _send_discord(self, op: dict[str, Any]) -> None:
        try:
            resp = await self._client.post(
                settings.discord_webhook_url, json=_embed_for_discord(op)
            )
            if resp.status_code >= 400:
                log.warning(
                    "discord alert failed status=%d body=%s",
                    resp.status_code,
                    resp.text[:200],
                )
        except Exception as exc:
            log.warning("discord alert exception: %s", exc)
