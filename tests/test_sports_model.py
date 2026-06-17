"""Tests for ESPN feed normalization, sports models, and prediction pipeline."""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone

import pytest

from kalshi_analyzer.analyzer import evaluate_sports_prediction
from kalshi_analyzer.config import settings
from kalshi_analyzer.models import Event, Market
from kalshi_analyzer.sports_feed import (
    ESPNClient,
    LiveGame,
    load_sports_match_map,
    match_game_to_kalshi,
    normalize_espn_scoreboard,
)
from kalshi_analyzer.sports_catalog import LEAGUE_CONFIGS, sport_key_for_ticker
from kalshi_analyzer.sports_model import (
    SportsModelEngine,
    dixon_coles_home_win_prob,
    predict_game,
    tennis_win_prob,
)
from kalshi_analyzer.sports_types import SportsPrediction


MOCK_ESPN_TENNIS_MATCH = {
    "id": "match-123",
    "competitors": [
        {
            "homeAway": "away",
            "athlete": {"displayName": "Daniel Altmaier"},
            "linescores": [{"value": "4"}, {"value": "3"}, {"value": "2"}],
        },
        {
            "homeAway": "home",
            "athlete": {"displayName": "Hubert Hurkacz"},
            "linescores": [{"value": "6"}, {"value": "6"}, {"value": "1"}],
        },
    ],
    "status": {
        "period": 3,
        "type": {"state": "in", "shortDetail": "3rd", "completed": False},
    },
}

MOCK_ESPN_TENNIS_EVENT = {
    "id": "27-2026",
    "shortName": "Terra Wortmann Open",
    "groupings": [
        {
            "grouping": {"displayName": "Men's Singles"},
            "competitions": [MOCK_ESPN_TENNIS_MATCH],
        }
    ],
}

MOCK_ESPN_EVENT = {
    "id": "401547689",
    "competitions": [
        {
            "id": "401547689",
            "competitors": [
                {
                    "homeAway": "home",
                    "score": "14",
                    "team": {"displayName": "Kansas City Chiefs", "abbreviation": "KC"},
                },
                {
                    "homeAway": "away",
                    "score": "10",
                    "team": {"displayName": "Buffalo Bills", "abbreviation": "BUF"},
                },
            ],
            "status": {
                "period": 3,
                "displayClock": "8:42",
                "type": {"state": "in", "shortDetail": "3rd 8:42"},
            },
            "situation": {
                "down": 2,
                "distance": 7,
                "possession": "KC",
                "yardLine": 45,
            },
        }
    ],
    "status": {"type": {"state": "in"}},
}


def mk_market(
    ticker: str = "KXNFL-24-KC",
    title: str = "Kansas City Chiefs to win",
    yes_ask: int = 48,
    **kwargs,
) -> Market:
    return Market(
        ticker=ticker,
        event_ticker="KXNFL-24",
        title=title,
        yes_bid=max(1, yes_ask - 2),
        yes_ask=yes_ask,
        no_bid=50,
        no_ask=52,
        liquidity=80_000,
        volume_24h=12_000,
        open_interest=20_000,
        status="active",
        **kwargs,
    )


def test_dixon_coles_halftime_scoreless_near_even_with_home_adv():
    p = dixon_coles_home_win_prob(0, 0, period=1, minutes_left=45.0)
    assert 0.48 <= p <= 0.62


def test_dixon_coles_score_deficit_shifts_toward_trailing_team():
    p_even = dixon_coles_home_win_prob(0, 0, period=1, minutes_left=45.0)
    p_home_trailing = dixon_coles_home_win_prob(0, 1, period=1, minutes_left=45.0)
    p_home_leading = dixon_coles_home_win_prob(1, 0, period=1, minutes_left=45.0)
    assert p_home_trailing < p_even
    assert p_home_leading > p_even


def test_espn_tennis_groupings_normalize_live_matches():
    games = normalize_espn_scoreboard(
        {"events": [MOCK_ESPN_TENNIS_EVENT]}, sport="tennis", league="atp"
    )
    assert len(games) == 1
    g = games[0]
    assert g.is_live is True
    assert g.away_team == "Daniel Altmaier"
    assert g.home_team == "Hubert Hurkacz"
    assert g.home_score == 2  # sets won
    assert g.away_score == 1
    assert g.tournament == "Terra Wortmann Open"


def test_espn_payload_normalization():
    games = normalize_espn_scoreboard(
        {"events": [MOCK_ESPN_EVENT]}, sport="football", league="nfl"
    )
    assert len(games) == 1
    g = games[0]
    assert g.home_team == "Kansas City Chiefs"
    assert g.away_team == "Buffalo Bills"
    assert g.home_score == 14
    assert g.away_score == 10
    assert g.is_live is True
    assert g.period == 3
    assert g.clock == "8:42"
    assert g.possession == "KC"


def test_team_name_fuzzy_match_hits_kalshi_ticker():
    game = normalize_espn_scoreboard(
        {"events": [MOCK_ESPN_EVENT]}, sport="football", league="nfl"
    )[0]
    ev = Event(
        event_ticker="KXNFL-24",
        title="Chiefs vs Bills",
        markets=[mk_market()],
    )
    market, ticker = match_game_to_kalshi(game, [ev])
    assert market is not None
    assert ticker == "KXNFL-24-KC"


def test_evaluate_sports_prediction_zero_edge_when_model_equals_ask():
    market = mk_market(yes_ask=50)
    game = LiveGame(
        game_id="1",
        sport="football",
        league="nfl",
        home_team="Kansas City Chiefs",
        away_team="Buffalo Bills",
        home_score=14,
        away_score=10,
        status="in",
        period=3,
        clock="8:42",
        possession="KC",
        situation=None,
        is_live=True,
        last_update=time.time(),
    )
    pred = predict_game(game, market)
    assert pred is not None
    pred.model_yes_prob = 0.50
    pred.kalshi_yes_ask = 0.50
    pred.edge_pct = 0.0
    assert evaluate_sports_prediction(pred) is None


def test_confidence_decays_when_last_update_stale(monkeypatch):
    monkeypatch.setattr(settings, "stale_last_age_seconds", 60, raising=False)
    monkeypatch.setattr(settings, "sports_model_min_edge_pct", 0.1, raising=False)
    monkeypatch.setattr(settings, "sports_model_min_confidence", 0.01, raising=False)
    monkeypatch.setattr(settings, "min_net_edge_pct", 0.1, raising=False)

    market = mk_market(yes_ask=40)
    stale_ts = time.time() - 600
    game = LiveGame(
        game_id="1",
        sport="football",
        league="nfl",
        home_team="Kansas City Chiefs",
        away_team="Buffalo Bills",
        home_score=21,
        away_score=10,
        status="in",
        period=4,
        clock="2:00",
        possession="KC",
        situation=None,
        is_live=True,
        last_update=stale_ts,
    )
    pred = predict_game(game, market, now=time.time())
    assert pred is not None
    fresh_conf = pred.confidence
    pred.game_state["last_update"] = stale_ts
    op = evaluate_sports_prediction(pred)
    if op is not None:
        assert op.confidence <= fresh_conf


def test_offseason_no_games_returns_empty_without_raising(monkeypatch):
    import asyncio

    monkeypatch.setattr(settings, "sports_model_enabled", True, raising=False)

    async def _empty_fetch(self):
        self._last_games = []
        self._last_fetch = time.time()
        return []

    monkeypatch.setattr(ESPNClient, "fetch_scoreboards", _empty_fetch)

    async def run():
        engine = SportsModelEngine(espn=ESPNClient())
        preds = await engine.get_predictions([])
        await engine.close()
        return preds

    preds = asyncio.run(run())
    assert preds == []


def test_manual_match_map_overrides_fuzzy_match(tmp_path, monkeypatch):
    path = tmp_path / "sports_match.json"
    path.write_text(json.dumps({"401547689": "KXNFL-MANUAL-LEG"}))
    monkeypatch.setattr(settings, "sports_match_path", str(path), raising=False)
    manual = load_sports_match_map(str(path))
    game = LiveGame(
        game_id="401547689",
        sport="football",
        league="nfl",
        home_team="Kansas City Chiefs",
        away_team="Buffalo Bills",
        home_score=0,
        away_score=0,
        status="in",
        period=1,
        clock="15:00",
        possession=None,
        situation=None,
        is_live=True,
        last_update=time.time(),
    )
    ev = Event(
        event_ticker="KXNFL-24",
        title="Unrelated Event",
        markets=[
            mk_market(ticker="KXNFL-OTHER", title="Random market"),
            mk_market(ticker="KXNFL-MANUAL-LEG", title="Manual mapped leg"),
        ],
    )
    market, ticker = match_game_to_kalshi(game, [ev], manual_map=manual)
    assert ticker == "KXNFL-MANUAL-LEG"
    assert market is not None
    assert market.ticker == "KXNFL-MANUAL-LEG"


def test_tennis_set_lead_increases_win_probability():
    p_even = tennis_win_prob(1, 1)
    p_home_lead = tennis_win_prob(2, 1)
    p_home_trail = tennis_win_prob(1, 2)
    assert p_home_lead > p_even
    assert p_home_trail < p_even


def test_sport_key_detects_tennis_and_esports():
    assert sport_key_for_ticker("KXATP-2026-FINAL", "KXATP-2026") == "tennis"
    assert sport_key_for_ticker("KXCS2-MAJOR", "KXCS2") == "esports"


def test_league_catalog_includes_tennis_and_college():
    leagues = {c.espn_league for c in LEAGUE_CONFIGS}
    assert "atp" in leagues and "wta" in leagues
    assert "college-football" in leagues
    assert "mens-college-basketball" in leagues


def test_app_imports_without_numpy():
    import importlib

    import kalshi_analyzer.server as srv

    importlib.reload(srv)
    assert srv.app.title == "Kalshi Edge Analyzer"
