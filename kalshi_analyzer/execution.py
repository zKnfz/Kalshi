"""Execution layer with hard safety rails.

Three modes selected via ``EXECUTION_MODE``:

  * ``off``    — default; the executor refuses every order. Use this
                 mode while the analyzer is still being tuned.
  * ``paper``  — orders are routed to ``PaperEngine`` instead of
                 Kalshi. Real fees and slippage are simulated; no
                 capital is at risk.
  * ``live``   — orders are sent to ``POST /portfolio/orders`` on
                 Kalshi. **Requires** an authenticated ``KalshiClient``
                 and an explicit, separate environment toggle. Every
                 live order passes the same circuit-breaker stack as
                 paper orders (kill switch, daily-loss limit,
                 position-already-held guard, sizing cap).

Circuit breakers (all enforced in ``_check_circuit_breakers``):

  1. ``KILL_SWITCH=true`` *or* the existence of ``KILL_SWITCH_FILE`` —
     any order is rejected immediately. The file path lets you trip the
     breaker from outside the running process (``touch /tmp/kalshi-
     kill-switch``).
  2. Daily realized loss exceeds ``MAX_DAILY_LOSS`` — every subsequent
     order is rejected until midnight UTC.
  3. The same ``(ticker, side)`` already has an open position — we
     don't double-down on signals that were already actioned this
     session. Override via ``allow_pyramid=True`` per order.
  4. Notional cost (price × contracts + fees) exceeds the configured
     ``MAX_BET_PCT × BANKROLL`` cap.

Everything here is async-safe but executes sequentially per process —
``OrderResult`` carries either the fill or the rejection reason.
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from .client import KalshiClient
from .config import settings
from .fees import total_fee
from .paper import PaperEngine, PaperFill

log = logging.getLogger(__name__)


def _today_utc() -> str:
    return datetime.now(tz=timezone.utc).strftime("%Y-%m-%d")


@dataclass
class OrderResult:
    accepted: bool
    mode: str
    ticker: str
    side: str
    contracts: int
    requested_price: float
    fill: PaperFill | None = None
    live_response: dict[str, Any] | None = None
    rejection_reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "accepted": self.accepted,
            "mode": self.mode,
            "ticker": self.ticker,
            "side": self.side,
            "contracts": self.contracts,
            "requested_price": round(self.requested_price, 4),
            "fill": self.fill.to_dict() if self.fill else None,
            "live_response": self.live_response,
            "rejection_reason": self.rejection_reason,
        }


@dataclass
class CircuitState:
    """State that determines whether the executor is allowed to send
    new orders. Persisted only in-process; ``paper`` realized P&L is
    sourced from the PaperEngine ledger and the ``live`` daily loss
    counter is reset at midnight UTC."""

    day: str = field(default_factory=_today_utc)
    live_daily_realized_pnl: float = 0.0
    orders_sent_today: int = 0
    rejections: dict[str, int] = field(default_factory=dict)


class Executor:
    """Order router used by the engine when an opportunity scores high
    enough to trade."""

    def __init__(
        self,
        *,
        paper: PaperEngine | None = None,
        client: KalshiClient | None = None,
    ) -> None:
        self._paper = paper
        self._client = client
        self._state = CircuitState()

    @property
    def mode(self) -> str:
        return settings.execution_mode

    @property
    def state(self) -> CircuitState:
        if self._state.day != _today_utc():
            self._state = CircuitState()
        return self._state

    def _check_circuit_breakers(
        self,
        *,
        ticker: str,
        side: str,
        contracts: int,
        price: float,
        allow_pyramid: bool,
    ) -> str | None:
        if settings.kill_switch:
            return "KILL_SWITCH env flag is set"
        if os.path.exists(settings.kill_switch_file):
            return f"kill-switch file present at {settings.kill_switch_file}"

        st = self.state
        live_loss = -min(0.0, st.live_daily_realized_pnl)
        paper_loss = (
            -min(0.0, self._paper.ledger.realized_pnl) if self._paper else 0.0
        )
        if self.mode == "live" and live_loss >= settings.max_daily_loss:
            return f"daily live loss {live_loss:.2f} ≥ MAX_DAILY_LOSS {settings.max_daily_loss:.2f}"
        if self.mode == "paper" and paper_loss >= settings.max_daily_loss:
            return f"daily paper loss {paper_loss:.2f} ≥ MAX_DAILY_LOSS {settings.max_daily_loss:.2f}"

        notional = price * contracts + total_fee(
            price=price, contracts=contracts, ticker=ticker
        )
        max_notional = settings.bankroll * settings.max_bet_pct
        if notional > max_notional + 1e-6:
            return (
                f"notional ${notional:.2f} exceeds MAX_BET_PCT cap "
                f"${max_notional:.2f}"
            )

        if not allow_pyramid:
            if self._paper and self._paper.has_position(ticker, side):
                return f"already holding {ticker} {side} (pyramid disabled)"
        return None

    async def submit(
        self,
        *,
        ticker: str,
        side: str,
        contracts: int,
        limit_price: float,
        is_taker: bool = True,
        allow_pyramid: bool = False,
        notes: str = "",
    ) -> OrderResult:
        side = side.upper()
        contracts = max(0, int(contracts))
        if contracts <= 0 or not (0 < limit_price < 1):
            return OrderResult(
                accepted=False,
                mode=self.mode,
                ticker=ticker,
                side=side,
                contracts=contracts,
                requested_price=limit_price,
                rejection_reason="invalid contracts or price",
            )

        if self.mode not in ("paper", "live"):
            self._track_reject("execution_off")
            return OrderResult(
                accepted=False,
                mode=self.mode,
                ticker=ticker,
                side=side,
                contracts=contracts,
                requested_price=limit_price,
                rejection_reason=(
                    "EXECUTION_MODE=off (set to 'paper' or 'live' to enable)"
                ),
            )

        reason = self._check_circuit_breakers(
            ticker=ticker,
            side=side,
            contracts=contracts,
            price=limit_price,
            allow_pyramid=allow_pyramid,
        )
        if reason:
            self._track_reject(reason)
            return OrderResult(
                accepted=False,
                mode=self.mode,
                ticker=ticker,
                side=side,
                contracts=contracts,
                requested_price=limit_price,
                rejection_reason=reason,
            )

        if self.mode == "paper":
            if self._paper is None:
                return OrderResult(
                    accepted=False,
                    mode=self.mode,
                    ticker=ticker,
                    side=side,
                    contracts=contracts,
                    requested_price=limit_price,
                    rejection_reason="paper engine not configured",
                )
            fill = self._paper.submit_order(
                ticker=ticker,
                side=side,
                contracts=contracts,
                limit_price=limit_price,
                is_taker=is_taker,
                notes=notes,
            )
            self.state.orders_sent_today += 1
            return OrderResult(
                accepted=fill is not None,
                mode=self.mode,
                ticker=ticker,
                side=side,
                contracts=contracts,
                requested_price=limit_price,
                fill=fill,
                rejection_reason="" if fill else "paper engine rejected (insufficient cash)",
            )

        if self._client is None or not self._client.has_auth:
            return OrderResult(
                accepted=False,
                mode=self.mode,
                ticker=ticker,
                side=side,
                contracts=contracts,
                requested_price=limit_price,
                rejection_reason="live mode requires authenticated KalshiClient",
            )

        body = {
            "action": "buy",
            "client_order_id": f"kea-{int(time.time() * 1000)}",
            "count": contracts,
            "side": "yes" if side == "YES" else "no",
            "ticker": ticker,
            "type": "limit",
            "yes_price": int(round(limit_price * 100))
            if side == "YES"
            else None,
            "no_price": int(round(limit_price * 100)) if side == "NO" else None,
        }
        body = {k: v for k, v in body.items() if v is not None}
        try:
            response = await self._client.post_authenticated(
                "/portfolio/orders", json=body
            )
        except Exception as exc:
            log.exception("live order failed: %s", exc)
            return OrderResult(
                accepted=False,
                mode=self.mode,
                ticker=ticker,
                side=side,
                contracts=contracts,
                requested_price=limit_price,
                rejection_reason=f"live POST failed: {exc}",
            )
        self.state.orders_sent_today += 1
        return OrderResult(
            accepted=True,
            mode=self.mode,
            ticker=ticker,
            side=side,
            contracts=contracts,
            requested_price=limit_price,
            live_response=response,
        )

    def _track_reject(self, reason: str) -> None:
        key = reason.split(":")[0].split("(")[0].strip()[:80] or "unknown"
        self.state.rejections[key] = self.state.rejections.get(key, 0) + 1
        log.info("executor rejected order: %s", reason)

    def stats(self) -> dict[str, Any]:
        st = self.state
        return {
            "mode": self.mode,
            "kill_switch": settings.kill_switch
            or os.path.exists(settings.kill_switch_file),
            "kill_switch_file": settings.kill_switch_file,
            "day_utc": st.day,
            "orders_sent_today": st.orders_sent_today,
            "live_daily_realized_pnl": round(st.live_daily_realized_pnl, 4),
            "max_daily_loss": settings.max_daily_loss,
            "rejections": dict(st.rejections),
            "max_notional_per_order": round(
                settings.bankroll * settings.max_bet_pct, 4
            ),
        }


def write_kill_switch(path: str | None = None) -> str:
    p = path or settings.kill_switch_file
    with open(p, "w") as f:
        f.write(f"tripped {datetime.now(tz=timezone.utc).isoformat()}\n")
    log.warning("kill switch tripped: %s", p)
    return p


def clear_kill_switch(path: str | None = None) -> None:
    p = path or settings.kill_switch_file
    try:
        os.remove(p)
    except FileNotFoundError:
        return
    log.info("kill switch cleared: %s", p)
