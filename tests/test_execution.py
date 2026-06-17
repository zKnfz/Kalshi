"""Tests for the execution layer + paper engine + circuit breakers."""

from __future__ import annotations

import asyncio
import os

import pytest

from kalshi_analyzer.config import settings
from kalshi_analyzer.execution import (
    Executor,
    clear_kill_switch,
    write_kill_switch,
)
from kalshi_analyzer.paper import PaperEngine


@pytest.fixture
def paper(tmp_path):
    return PaperEngine(
        state_path=str(tmp_path / "paper.json"),
        starting_bankroll=1000.0,
        slippage_cents=1.0,
    )


def run(coro):
    return asyncio.run(coro)


def test_default_execution_mode_rejects_orders(monkeypatch, paper):
    monkeypatch.setattr(settings, "execution_mode", "off", raising=False)
    ex = Executor(paper=paper)
    result = run(
        ex.submit(ticker="T", side="YES", contracts=10, limit_price=0.40)
    )
    assert not result.accepted
    assert "EXECUTION_MODE=off" in result.rejection_reason


def test_paper_mode_executes_and_persists(monkeypatch, paper, tmp_path):
    monkeypatch.setattr(settings, "execution_mode", "paper", raising=False)
    monkeypatch.setattr(settings, "max_bet_pct", 0.5, raising=False)
    ex = Executor(paper=paper)
    result = run(
        ex.submit(ticker="T", side="YES", contracts=10, limit_price=0.40)
    )
    assert result.accepted
    assert result.fill is not None
    assert result.fill.contracts == 10
    assert result.fill.fee > 0
    assert paper.has_position("T", "YES")


def test_paper_double_entry_blocked_without_pyramid(monkeypatch, paper):
    monkeypatch.setattr(settings, "execution_mode", "paper", raising=False)
    monkeypatch.setattr(settings, "max_bet_pct", 0.5, raising=False)
    ex = Executor(paper=paper)
    r1 = run(ex.submit(ticker="T", side="YES", contracts=10, limit_price=0.40))
    assert r1.accepted
    r2 = run(ex.submit(ticker="T", side="YES", contracts=10, limit_price=0.40))
    assert not r2.accepted
    assert "already holding" in r2.rejection_reason


def test_paper_double_entry_allowed_with_pyramid(monkeypatch, paper):
    monkeypatch.setattr(settings, "execution_mode", "paper", raising=False)
    monkeypatch.setattr(settings, "max_bet_pct", 0.5, raising=False)
    ex = Executor(paper=paper)
    r1 = run(ex.submit(ticker="T", side="YES", contracts=10, limit_price=0.40))
    r2 = run(
        ex.submit(
            ticker="T",
            side="YES",
            contracts=10,
            limit_price=0.40,
            allow_pyramid=True,
        )
    )
    assert r1.accepted and r2.accepted


def test_max_bet_pct_caps_order(monkeypatch, paper):
    monkeypatch.setattr(settings, "execution_mode", "paper", raising=False)
    monkeypatch.setattr(settings, "max_bet_pct", 0.01, raising=False)
    ex = Executor(paper=paper)
    # 100 contracts at $0.50 = $50 notional > 1% of $1000 = $10
    result = run(
        ex.submit(ticker="T", side="YES", contracts=100, limit_price=0.50)
    )
    assert not result.accepted
    assert "MAX_BET_PCT" in result.rejection_reason


def test_kill_switch_file_blocks_orders(monkeypatch, paper, tmp_path):
    monkeypatch.setattr(settings, "execution_mode", "paper", raising=False)
    monkeypatch.setattr(settings, "max_bet_pct", 0.5, raising=False)
    ksf = tmp_path / "killswitch"
    monkeypatch.setattr(settings, "kill_switch_file", str(ksf), raising=False)
    write_kill_switch(str(ksf))
    ex = Executor(paper=paper)
    result = run(
        ex.submit(ticker="T", side="YES", contracts=10, limit_price=0.40)
    )
    assert not result.accepted
    assert "kill-switch" in result.rejection_reason
    clear_kill_switch(str(ksf))
    assert not os.path.exists(ksf)


def test_kill_switch_env_flag_blocks_orders(monkeypatch, paper):
    monkeypatch.setattr(settings, "execution_mode", "paper", raising=False)
    monkeypatch.setattr(settings, "max_bet_pct", 0.5, raising=False)
    monkeypatch.setattr(settings, "kill_switch", True, raising=False)
    ex = Executor(paper=paper)
    result = run(
        ex.submit(ticker="T", side="YES", contracts=10, limit_price=0.40)
    )
    assert not result.accepted
    assert "KILL_SWITCH" in result.rejection_reason


def test_paper_daily_loss_breaker(monkeypatch, paper):
    monkeypatch.setattr(settings, "execution_mode", "paper", raising=False)
    monkeypatch.setattr(settings, "max_bet_pct", 0.5, raising=False)
    monkeypatch.setattr(settings, "max_daily_loss", 5.0, raising=False)
    # Force realized_pnl below the limit
    paper.ledger.realized_pnl = -6.0
    ex = Executor(paper=paper)
    result = run(
        ex.submit(ticker="T", side="YES", contracts=10, limit_price=0.40)
    )
    assert not result.accepted
    assert "MAX_DAILY_LOSS" in result.rejection_reason


def test_paper_mark_to_market_and_settle(monkeypatch, paper):
    monkeypatch.setattr(settings, "execution_mode", "paper", raising=False)
    monkeypatch.setattr(settings, "max_bet_pct", 0.5, raising=False)
    ex = Executor(paper=paper)
    run(ex.submit(ticker="T", side="YES", contracts=10, limit_price=0.40))
    mtm = paper.mark_to_market({"T": {"yes_mid": 0.55, "no_mid": 0.45}})
    assert mtm["unrealized_pnl"] > 0
    pnl = paper.settle("T", "YES", won=True)
    assert pnl > 0
    assert not paper.has_position("T", "YES")
    assert paper.ledger.realized_pnl > 0


def test_paper_state_persisted(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "execution_mode", "paper", raising=False)
    monkeypatch.setattr(settings, "max_bet_pct", 0.5, raising=False)
    path = str(tmp_path / "p.json")
    p1 = PaperEngine(state_path=path, starting_bankroll=1000.0)
    ex = Executor(paper=p1)
    run(ex.submit(ticker="T", side="YES", contracts=10, limit_price=0.40))
    p2 = PaperEngine(state_path=path, starting_bankroll=999.0)
    assert p2.has_position("T", "YES")
    assert p2.ledger.cash < 1000.0


def test_live_mode_without_auth_fails_safely(monkeypatch, paper):
    monkeypatch.setattr(settings, "execution_mode", "live", raising=False)
    monkeypatch.setattr(settings, "max_bet_pct", 0.5, raising=False)
    ex = Executor(paper=paper, client=None)
    result = run(
        ex.submit(ticker="T", side="YES", contracts=10, limit_price=0.40)
    )
    assert not result.accepted
    assert "authenticated KalshiClient" in result.rejection_reason
