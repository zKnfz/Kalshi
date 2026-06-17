"""Tests for the snapshot recorder + replay engine."""

from __future__ import annotations

import json

from kalshi_analyzer.backtest import append_snapshot, iter_snapshots, replay


def _snap(ticker: str, side: str, entry: float, net_pct: float, stake: float) -> dict:
    return {
        "generated_at": "2026-06-17T13:00:00Z",
        "opportunities": [
            {
                "ticker": ticker,
                "side": side,
                "strategy": "fair_value_yes",
                "entry_price": entry,
                "net_edge_pct": net_pct,
                "suggested_stake": stake,
            }
        ],
    }


def test_append_and_iter_snapshots(tmp_path):
    p = tmp_path / "snaps.jsonl"
    append_snapshot(_snap("T", "YES", 0.40, 5.0, 50.0), path=str(p))
    append_snapshot(_snap("U", "NO", 0.50, 3.0, 20.0), path=str(p))
    lines = list(iter_snapshots(str(p)))
    assert len(lines) == 2
    assert lines[0]["opportunities"][0]["ticker"] == "T"


def test_iter_skips_malformed_lines(tmp_path):
    p = tmp_path / "snaps.jsonl"
    p.write_text(
        json.dumps(_snap("A", "YES", 0.40, 5, 100))
        + "\n{not json\n"
        + json.dumps(_snap("B", "YES", 0.40, 5, 100))
        + "\n"
    )
    assert len(list(iter_snapshots(str(p)))) == 2


def test_replay_returns_report_with_orders(tmp_path):
    snaps = tmp_path / "snaps.jsonl"
    for _ in range(3):
        append_snapshot(_snap("T1", "YES", 0.40, 8.0, 50.0), path=str(snaps))

    report = replay(
        str(snaps),
        paper_state_path=str(tmp_path / "paper.json"),
        starting_bankroll=1000.0,
        min_net_edge_pct=2.0,
        max_orders_per_tick=5,
    )
    assert report.snapshots_replayed == 3
    assert "fair_value_yes" in report.by_strategy
    s = report.by_strategy["fair_value_yes"]
    assert s.orders == 3
    assert s.accepted == 1
    assert s.rejected == 2
    assert "already_held" in report.rejection_reasons


def test_replay_respects_min_edge_filter(tmp_path):
    snaps = tmp_path / "snaps.jsonl"
    append_snapshot(_snap("T1", "YES", 0.40, 1.0, 50.0), path=str(snaps))
    report = replay(
        str(snaps),
        paper_state_path=str(tmp_path / "paper.json"),
        starting_bankroll=1000.0,
        min_net_edge_pct=5.0,
    )
    assert all(s.orders == 0 for s in report.by_strategy.values())


def test_replay_with_no_snapshots(tmp_path):
    p = tmp_path / "missing.jsonl"
    report = replay(
        str(p),
        paper_state_path=str(tmp_path / "paper.json"),
        starting_bankroll=100.0,
        min_net_edge_pct=2.0,
    )
    assert report.snapshots_replayed == 0
    assert report.ending_equity == 100.0


def test_cli_replay_smoke(tmp_path, capsys):
    """Ensure the CLI argparser + replay path runs end-to-end."""

    from kalshi_analyzer.cli import main

    snaps = tmp_path / "snaps.jsonl"
    append_snapshot(_snap("T1", "YES", 0.40, 5.0, 50.0), path=str(snaps))
    code = main(
        [
            "replay",
            "--path",
            str(snaps),
            "--paper-state",
            str(tmp_path / "p.json"),
            "--bankroll",
            "1000",
            "--min-edge",
            "1",
            "--json",
        ]
    )
    out = capsys.readouterr().out
    assert code == 0
    report = json.loads(out)
    assert report["snapshots_replayed"] == 1
    assert "by_strategy" in report
