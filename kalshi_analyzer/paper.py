"""Paper-trading engine.

Simulates fills, slippage and P&L for opportunities surfaced by the
analyzer. State is persisted to ``PAPER_STATE_PATH`` (a JSON file) so
positions and the running P&L survive restarts.

Designed to mirror the same interface as the live executor so the rest
of the system can call ``submit_order`` / ``mark_to_market`` /
``settle`` agnostic of whether real or paper trades are in flight.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any

from .fees import per_contract_fee, total_fee

log = logging.getLogger(__name__)


def _now() -> str:
    return (
        datetime.now(tz=timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )


@dataclass
class PaperFill:
    """One simulated fill. Records the *effective* price after slippage
    and the fee Kalshi would charge under the standard taker schedule."""

    ts: str
    ticker: str
    side: str
    contracts: int
    fill_price: float
    requested_price: float
    fee: float
    notes: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class PaperPosition:
    ticker: str
    side: str
    contracts: int = 0
    avg_price: float = 0.0
    fees_paid: float = 0.0
    realized_pnl: float = 0.0
    last_mark_price: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class PaperLedger:
    fills: list[PaperFill] = field(default_factory=list)
    positions: dict[str, PaperPosition] = field(default_factory=dict)
    cash: float = 0.0
    realized_pnl: float = 0.0
    fees_paid: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "fills": [f.to_dict() for f in self.fills],
            "positions": {k: p.to_dict() for k, p in self.positions.items()},
            "cash": round(self.cash, 4),
            "realized_pnl": round(self.realized_pnl, 4),
            "fees_paid": round(self.fees_paid, 4),
        }


class PaperEngine:
    """Thread-safe paper execution engine with disk persistence."""

    def __init__(
        self,
        state_path: str,
        starting_bankroll: float,
        *,
        slippage_cents: float = 1.0,
    ) -> None:
        self._path = state_path
        self._lock = threading.Lock()
        self._slippage = max(0.0, slippage_cents) / 100.0
        if os.path.exists(state_path):
            self._ledger = self._load()
        else:
            self._ledger = PaperLedger(cash=starting_bankroll)
            self._save()

    @property
    def ledger(self) -> PaperLedger:
        return self._ledger

    def _load(self) -> PaperLedger:
        try:
            with open(self._path, "r") as f:
                data = json.load(f)
        except Exception:
            log.warning(
                "paper ledger %s unreadable; starting fresh", self._path
            )
            return PaperLedger()
        positions = {
            k: PaperPosition(**v) for k, v in (data.get("positions") or {}).items()
        }
        fills = [PaperFill(**f) for f in data.get("fills") or []]
        return PaperLedger(
            fills=fills,
            positions=positions,
            cash=float(data.get("cash") or 0.0),
            realized_pnl=float(data.get("realized_pnl") or 0.0),
            fees_paid=float(data.get("fees_paid") or 0.0),
        )

    def _save(self) -> None:
        tmp = f"{self._path}.tmp"
        with open(tmp, "w") as f:
            json.dump(self._ledger.to_dict(), f, indent=2)
        os.replace(tmp, self._path)

    @staticmethod
    def _key(ticker: str, side: str) -> str:
        return f"{ticker}:{side.upper()}"

    def has_position(self, ticker: str, side: str) -> bool:
        with self._lock:
            return self._key(ticker, side) in self._ledger.positions

    def submit_order(
        self,
        *,
        ticker: str,
        side: str,
        contracts: int,
        limit_price: float,
        is_taker: bool = True,
        notes: str = "",
    ) -> PaperFill | None:
        if contracts <= 0 or limit_price <= 0 or limit_price >= 1:
            return None
        side = side.upper()
        slippage = self._slippage if is_taker else 0.0
        fill_price = min(0.99, max(0.01, limit_price + slippage))
        cost = fill_price * contracts
        fee = total_fee(price=fill_price, contracts=contracts, ticker=ticker, is_taker=is_taker)
        fill = PaperFill(
            ts=_now(),
            ticker=ticker,
            side=side,
            contracts=contracts,
            fill_price=round(fill_price, 4),
            requested_price=round(limit_price, 4),
            fee=round(fee, 4),
            notes=notes,
        )
        with self._lock:
            if cost + fee > self._ledger.cash + 1e-6:
                log.info(
                    "paper: rejecting order (cost %.2f + fee %.2f > cash %.2f)",
                    cost,
                    fee,
                    self._ledger.cash,
                )
                return None
            self._ledger.cash -= cost + fee
            self._ledger.fees_paid += fee
            self._ledger.fills.append(fill)

            key = self._key(ticker, side)
            pos = self._ledger.positions.get(key)
            if pos is None:
                pos = PaperPosition(ticker=ticker, side=side)
                self._ledger.positions[key] = pos
            new_contracts = pos.contracts + contracts
            pos.avg_price = (
                (pos.avg_price * pos.contracts + fill_price * contracts) / new_contracts
            )
            pos.contracts = new_contracts
            pos.fees_paid += fee
            self._save()
        return fill

    def mark_to_market(self, marks: dict[str, dict[str, float]]) -> dict[str, Any]:
        """Update every open position's last_mark_price using the supplied
        marks dict. ``marks[ticker] = {"yes_mid": 0.42, "no_mid": 0.58}``.

        Returns a snapshot of total unrealized + realized P&L.
        """

        unrealized = 0.0
        with self._lock:
            for key, pos in self._ledger.positions.items():
                m = marks.get(pos.ticker)
                if not m:
                    continue
                mid = m.get("yes_mid") if pos.side == "YES" else m.get("no_mid")
                if mid is None:
                    continue
                pos.last_mark_price = float(mid)
                unrealized += (mid - pos.avg_price) * pos.contracts
            self._save()
            return {
                "cash": round(self._ledger.cash, 4),
                "realized_pnl": round(self._ledger.realized_pnl, 4),
                "fees_paid": round(self._ledger.fees_paid, 4),
                "unrealized_pnl": round(unrealized, 4),
                "equity": round(
                    self._ledger.cash
                    + sum(
                        (p.last_mark_price or p.avg_price) * p.contracts
                        for p in self._ledger.positions.values()
                    ),
                    4,
                ),
                "open_positions": len(self._ledger.positions),
            }

    def settle(self, ticker: str, side: str, won: bool) -> float:
        """Close out the position at $1.00 or $0.00. Returns realized
        P&L delta for this settlement."""

        with self._lock:
            key = self._key(ticker, side)
            pos = self._ledger.positions.get(key)
            if pos is None or pos.contracts <= 0:
                return 0.0
            payout = (1.0 if won else 0.0) * pos.contracts
            pnl = payout - pos.avg_price * pos.contracts
            self._ledger.cash += payout
            self._ledger.realized_pnl += pnl
            pos.realized_pnl += pnl
            pos.contracts = 0
            self._ledger.positions.pop(key, None)
            self._save()
            return pnl

    def reset(self, starting_bankroll: float) -> None:
        with self._lock:
            self._ledger = PaperLedger(cash=starting_bankroll)
            self._save()

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return self._ledger.to_dict()
