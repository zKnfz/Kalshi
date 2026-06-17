"""Small CLI for backtest + kill-switch operations.

Usage::

    python -m kalshi_analyzer.cli record-snapshots
    python -m kalshi_analyzer.cli replay --bankroll 1000 --min-edge 3
    python -m kalshi_analyzer.cli kill-switch on
    python -m kalshi_analyzer.cli kill-switch off
"""

from __future__ import annotations

import argparse
import json
import sys

from .backtest import replay
from .config import settings
from .execution import clear_kill_switch, write_kill_switch


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="kalshi-edge")
    sub = parser.add_subparsers(dest="cmd", required=True)

    rec = sub.add_parser(
        "record-snapshots",
        help="Print a reminder of how to enable recording.",
    )
    rec.add_argument(
        "--path", default=settings.backtest_snapshot_path, help="JSONL output path"
    )

    rep = sub.add_parser("replay", help="Replay a recorded JSONL of snapshots.")
    rep.add_argument(
        "--path", default=settings.backtest_snapshot_path, help="Input JSONL path"
    )
    rep.add_argument(
        "--paper-state",
        default="/tmp/kalshi-backtest-paper.json",
        help="Temporary PaperEngine state file (deleted before replay).",
    )
    rep.add_argument("--bankroll", type=float, default=1000.0)
    rep.add_argument(
        "--min-edge",
        type=float,
        default=settings.min_net_edge_pct,
        help="Minimum net edge %% to act on.",
    )
    rep.add_argument("--max-orders-per-tick", type=int, default=5)
    rep.add_argument("--slippage-cents", type=float, default=1.0)
    rep.add_argument("--allow-pyramid", action="store_true")
    rep.add_argument(
        "--json", action="store_true", help="Emit the report as raw JSON."
    )

    ks = sub.add_parser("kill-switch", help="Trip or clear the kill switch.")
    ks.add_argument("action", choices=["on", "off"])

    args = parser.parse_args(argv)

    if args.cmd == "record-snapshots":
        print(
            "To record snapshots, set BACKTEST_RECORDING=true in your .env "
            "and run `python run.py`. Each tick will be appended to "
            f"{args.path}."
        )
        return 0

    if args.cmd == "replay":
        report = replay(
            args.path,
            paper_state_path=args.paper_state,
            starting_bankroll=args.bankroll,
            min_net_edge_pct=args.min_edge,
            max_orders_per_tick=args.max_orders_per_tick,
            slippage_cents=args.slippage_cents,
            allow_pyramid=args.allow_pyramid,
        )
        if args.json:
            print(json.dumps(report.to_dict(), indent=2))
        else:
            r = report
            print(
                f"Replayed {r.snapshots_replayed} snapshots. "
                f"Start: ${r.starting_bankroll:.2f}  End: ${r.ending_equity:.2f}  "
                f"ROI: {((r.ending_equity / r.starting_bankroll - 1) * 100 if r.starting_bankroll else 0):+.2f}%"
            )
            print(
                f"Realized P&L: ${r.realized_pnl:.2f}  "
                f"Unrealized: ${r.unrealized_pnl:.2f}  "
                f"Fees paid: ${r.fees_paid:.2f}"
            )
            if r.by_strategy:
                print("By strategy:")
                for s in r.by_strategy.values():
                    print(
                        f"  {s.strategy:24}  orders={s.orders:<4} "
                        f"accepted={s.accepted:<4} rejected={s.rejected:<4} "
                        f"fees=${s.fees_paid:.2f}"
                    )
            if r.rejection_reasons:
                print("Rejections:")
                for k, v in r.rejection_reasons.items():
                    print(f"  {k:32}  {v}")
        return 0

    if args.cmd == "kill-switch":
        if args.action == "on":
            path = write_kill_switch()
            print(f"kill switch tripped: {path}")
        else:
            clear_kill_switch()
            print("kill switch cleared")
        return 0

    parser.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
