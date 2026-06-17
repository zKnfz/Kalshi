"""Snapshot-based backtest.

Live operation produces a stream of analyzer snapshots — opportunities
plus the raw market quotes that produced them. This module supports:

  1. **Recording** — when ``BACKTEST_RECORDING=true`` the engine
     appends every tick's snapshot to a JSONL file
     (``BACKTEST_SNAPSHOT_PATH``).
  2. **Replay** — ``replay(snapshot_path, paper_state_path, ...)``
     iterates the recorded snapshots, drives a fresh ``PaperEngine``
     with a configurable execution policy, marks-to-market on every
     tick, and finally settles open positions at the last observed mid.
     Returns a ``BacktestReport`` summarizing P&L by strategy and a
     trade-by-trade ledger.

The point is to answer the user's question: *"does a 3% gross-edge
opportunity actually convert to profit after fees and slippage?"*
without anything more elaborate than a few hours of recorded snapshots.

Run from the CLI::

    python -m kalshi_analyzer.cli record-snapshots
    python -m kalshi_analyzer.cli replay --bankroll 1000 --min-edge 3.0
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from typing import Any, Iterable

from .config import settings
from .paper import PaperEngine

log = logging.getLogger(__name__)


def append_snapshot(snapshot: dict[str, Any], path: str | None = None) -> None:
    p = path or settings.backtest_snapshot_path
    line = json.dumps(snapshot, separators=(",", ":"))
    with open(p, "a") as f:
        f.write(line + "\n")


def iter_snapshots(path: str) -> Iterable[dict[str, Any]]:
    if not os.path.exists(path):
        return
    with open(path) as f:
        for raw in f:
            raw = raw.strip()
            if not raw:
                continue
            try:
                yield json.loads(raw)
            except json.JSONDecodeError:
                continue


@dataclass
class StrategyStats:
    strategy: str
    orders: int = 0
    accepted: int = 0
    rejected: int = 0
    realized_pnl: float = 0.0
    fees_paid: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "strategy": self.strategy,
            "orders": self.orders,
            "accepted": self.accepted,
            "rejected": self.rejected,
            "realized_pnl": round(self.realized_pnl, 4),
            "fees_paid": round(self.fees_paid, 4),
        }


@dataclass
class BacktestReport:
    snapshots_replayed: int
    starting_bankroll: float
    ending_equity: float
    realized_pnl: float
    unrealized_pnl: float
    fees_paid: float
    by_strategy: dict[str, StrategyStats] = field(default_factory=dict)
    rejection_reasons: dict[str, int] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "snapshots_replayed": self.snapshots_replayed,
            "starting_bankroll": round(self.starting_bankroll, 4),
            "ending_equity": round(self.ending_equity, 4),
            "realized_pnl": round(self.realized_pnl, 4),
            "unrealized_pnl": round(self.unrealized_pnl, 4),
            "total_pnl": round(self.realized_pnl + self.unrealized_pnl, 4),
            "fees_paid": round(self.fees_paid, 4),
            "roi_pct": (
                100.0
                * (self.ending_equity - self.starting_bankroll)
                / self.starting_bankroll
                if self.starting_bankroll
                else 0.0
            ),
            "by_strategy": {k: v.to_dict() for k, v in self.by_strategy.items()},
            "rejection_reasons": self.rejection_reasons,
        }


def replay(
    snapshot_path: str,
    *,
    paper_state_path: str = "/tmp/kalshi-backtest-paper.json",
    starting_bankroll: float = 1000.0,
    min_net_edge_pct: float | None = None,
    max_orders_per_tick: int = 5,
    slippage_cents: float = 1.0,
    allow_pyramid: bool = False,
) -> BacktestReport:
    """Replay a JSONL snapshot stream against a fresh paper engine.

    Policy: every tick, take the top-N opportunities whose
    ``net_edge_pct`` exceeds ``min_net_edge_pct`` and that we don't
    already hold; submit each at the recorded entry price and recorded
    suggested-stake (rounded down to a contract count). Mark to market
    against the per-tick mids embedded in each opportunity (entry =
    side ask). At the end, settle every still-open position at its last
    observed mid (no oracle into the future).
    """

    if os.path.exists(paper_state_path):
        os.remove(paper_state_path)
    paper = PaperEngine(
        state_path=paper_state_path,
        starting_bankroll=starting_bankroll,
        slippage_cents=slippage_cents,
    )

    threshold = (
        min_net_edge_pct
        if min_net_edge_pct is not None
        else settings.min_net_edge_pct
    )

    by_strategy: dict[str, StrategyStats] = {}
    rejection_reasons: dict[str, int] = {}
    snapshots = 0
    last_marks: dict[str, dict[str, float]] = {}

    for snap in iter_snapshots(snapshot_path):
        snapshots += 1
        marks: dict[str, dict[str, float]] = {}
        for op in snap.get("opportunities", []):
            t = op.get("ticker")
            entry = op.get("entry_price") or 0.0
            if not t:
                continue
            side = (op.get("side") or "YES").upper()
            if side == "YES":
                marks.setdefault(t, {})["yes_mid"] = entry
                marks.setdefault(t, {})["no_mid"] = max(0.0, 1.0 - entry)
            elif side == "NO":
                marks.setdefault(t, {})["no_mid"] = entry
                marks.setdefault(t, {})["yes_mid"] = max(0.0, 1.0 - entry)
        for t, m in marks.items():
            last_marks[t] = m
        if marks:
            paper.mark_to_market(marks)

        eligible = [
            op
            for op in (snap.get("opportunities") or [])
            if (op.get("net_edge_pct") or 0.0) >= threshold
        ][:max_orders_per_tick]
        for op in eligible:
            t = op.get("ticker") or ""
            side = (op.get("side") or "YES").upper()
            entry = op.get("entry_price") or 0.0
            strat = op.get("strategy") or "?"
            stats = by_strategy.setdefault(strat, StrategyStats(strategy=strat))
            stats.orders += 1

            if not allow_pyramid and paper.has_position(t, side):
                stats.rejected += 1
                rejection_reasons["already_held"] = (
                    rejection_reasons.get("already_held", 0) + 1
                )
                continue
            stake = float(op.get("suggested_stake") or 0.0)
            if entry <= 0 or entry >= 1 or stake <= 0:
                stats.rejected += 1
                rejection_reasons["bad_inputs"] = (
                    rejection_reasons.get("bad_inputs", 0) + 1
                )
                continue
            contracts = int(stake // entry)
            if contracts <= 0:
                stats.rejected += 1
                rejection_reasons["stake_below_one_contract"] = (
                    rejection_reasons.get("stake_below_one_contract", 0) + 1
                )
                continue
            fill = paper.submit_order(
                ticker=t,
                side=side,
                contracts=contracts,
                limit_price=entry,
                is_taker=True,
                notes=f"backtest:{strat}",
            )
            if fill is None:
                stats.rejected += 1
                rejection_reasons["paper_rejected"] = (
                    rejection_reasons.get("paper_rejected", 0) + 1
                )
                continue
            stats.accepted += 1
            stats.fees_paid += fill.fee

    final = paper.mark_to_market(last_marks) if last_marks else {
        "cash": paper.ledger.cash,
        "realized_pnl": paper.ledger.realized_pnl,
        "fees_paid": paper.ledger.fees_paid,
        "unrealized_pnl": 0.0,
        "equity": paper.ledger.cash,
    }

    realized_by_strategy: dict[str, float] = {}
    for fill in paper.ledger.fills:
        notes = fill.notes or ""
        if notes.startswith("backtest:"):
            strat = notes.split(":", 1)[1]
            realized_by_strategy[strat] = realized_by_strategy.get(strat, 0.0)
    for strat, stats in by_strategy.items():
        stats.realized_pnl = realized_by_strategy.get(strat, 0.0)

    return BacktestReport(
        snapshots_replayed=snapshots,
        starting_bankroll=starting_bankroll,
        ending_equity=final["equity"],
        realized_pnl=final["realized_pnl"],
        unrealized_pnl=final["unrealized_pnl"],
        fees_paid=final["fees_paid"],
        by_strategy=by_strategy,
        rejection_reasons=rejection_reasons,
    )
