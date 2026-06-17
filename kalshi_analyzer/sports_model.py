"""Statistical in-play sports models — no ML training required.

Pure-Python implementation (no numpy/scipy import at load time) so the app
starts without optional scientific deps. Enable via ``SPORTS_MODEL_ENABLED``.
"""

from __future__ import annotations

import logging
import math
import time
from typing import Any

from .config import settings
from .models import Event, Market
from .sports_feed import (
    ESPNClient,
    LiveGame,
    load_sports_match_map,
    match_game_to_kalshi,
)
from .sports_types import SportsPrediction

log = logging.getLogger(__name__)

HOME_ADVANTAGE = 0.08
DEFAULT_XG_PER_HALF = 1.35
DIXON_COLES_RHO = -0.13

_NBA_COEF = [
    [-4.2, -3.5, -2.8, -2.2, -1.6],
    [-3.0, -2.5, -2.0, -1.5, -1.1],
    [-1.8, -1.4, -1.0, -0.7, -0.5],
    [0.0, 0.0, 0.0, 0.0, 0.0],
    [1.8, 1.4, 1.0, 0.7, 0.5],
    [3.0, 2.5, 2.0, 1.5, 1.1],
    [4.2, 3.5, 2.8, 2.2, 1.6],
]
_NFL_COEF = [
    [-3.8, -3.0, -2.3, -1.7, -1.2],
    [-2.6, -2.0, -1.5, -1.1, -0.8],
    [-1.5, -1.1, -0.8, -0.6, -0.4],
    [0.0, 0.0, 0.0, 0.0, 0.0],
    [1.5, 1.1, 0.8, 0.6, 0.4],
    [2.6, 2.0, 1.5, 1.1, 0.8],
    [3.8, 3.0, 2.3, 1.7, 1.2],
]
_MLB_RE = [
    [0.46, 0.72, 1.04, 1.35, 1.15, 1.45, 1.82, 2.10],
    [0.24, 0.45, 0.62, 0.88, 0.52, 0.75, 0.98, 1.20],
    [0.10, 0.20, 0.28, 0.41, 0.22, 0.35, 0.48, 0.60],
]


def _clamp(p: float, lo: float = 0.01, hi: float = 0.99) -> float:
    return max(lo, min(hi, p))


def _poisson_pmf(k: int, lam: float) -> float:
    if lam <= 0:
        return 1.0 if k == 0 else 0.0
    return math.exp(-lam) * (lam**k) / math.factorial(k)


def _minutes_remaining(game: LiveGame) -> float:
    if game.league in {"nba", "wnba", "mens-college-basketball", "womens-college-basketball"}:
        period_len = 12 if game.league != "mens-college-basketball" else 20
        periods = 4
        remaining_periods = max(0, periods - max(0, game.period - 1))
        clock_m = period_len
        if game.clock and ":" in game.clock:
            try:
                mm, ss = game.clock.split(":", 1)
                clock_m = int(mm) + int(ss) / 60.0
            except ValueError:
                pass
        return remaining_periods * period_len + clock_m
    if game.league == "nhl":
        period_len = 20
        remaining_periods = max(0, 3 - max(0, game.period - 1))
        clock_m = period_len
        if game.clock and ":" in game.clock:
            try:
                mm, ss = game.clock.split(":", 1)
                clock_m = int(mm) + int(ss) / 60.0
            except ValueError:
                pass
        return remaining_periods * period_len + clock_m
    if game.league in {"nfl", "college-football"}:
        period_len = 15
        remaining_periods = max(0, 4 - max(0, game.period - 1))
        clock_m = period_len
        if game.clock and ":" in game.clock:
            try:
                mm, ss = game.clock.split(":", 1)
                clock_m = int(mm) + int(ss) / 60.0
            except ValueError:
                pass
        return remaining_periods * period_len + clock_m
    if game.league == "mlb":
        return max(1.0, (9 - min(game.period, 9)) * 3.0)
    if game.league in {"fifa.world", "usa.1", "eng.1", "uefa.champions"}:
        return max(5.0, (2 - min(game.period, 2)) * 45.0)
    if game.league in {"atp", "wta"}:
        return max(10.0, (3 - min(game.period, 3)) * 45.0)
    return 30.0


def dixon_coles_home_win_prob(
    home_score: int,
    away_score: int,
    *,
    period: int = 1,
    minutes_left: float | None = None,
    home_xg: float = DEFAULT_XG_PER_HALF,
    away_xg: float = DEFAULT_XG_PER_HALF,
    home_elo_adv: float = HOME_ADVANTAGE,
) -> float:
    if minutes_left is None:
        minutes_left = 45.0
    frac = _clamp(minutes_left / 90.0, 0.05, 1.0)
    lam_h = home_xg * frac * (1.0 + home_elo_adv)
    lam_a = away_xg * frac

    max_goals = 8
    p_home = 0.0
    p_away = 0.0
    for add_h in range(max_goals + 1):
        for add_a in range(max_goals + 1):
            ph = _poisson_pmf(add_h, lam_h)
            pa = _poisson_pmf(add_a, lam_a)
            if add_h == 0 and add_a == 0:
                tau = 1.0 - DIXON_COLES_RHO * math.sqrt(lam_h * lam_a) / (lam_h + lam_a + 1e-9)
            else:
                tau = 1.0
            p = ph * pa * tau
            fh = home_score + add_h
            fa = away_score + add_a
            if fh > fa:
                p_home += p
            elif fa > fh:
                p_away += p
    denom = p_home + p_away
    if denom <= 1e-9:
        return 0.5
    return _clamp(p_home / denom)


def _bucket_index(minutes_left: float, sport: str) -> int:
    edges = (6, 12, 24, 36) if sport == "nba" else (5, 10, 20, 30)
    for i, edge in enumerate(edges):
        if minutes_left < edge:
            return i
    return len(edges)


def win_prob_table(
    score_diff: int,
    minutes_left: float,
    *,
    sport: str = "nba",
    has_possession: bool = False,
) -> float:
    diff = max(-3, min(3, score_diff))
    idx = diff + 3
    bucket = _bucket_index(minutes_left, sport)
    table = _NBA_COEF if sport == "nba" else _NFL_COEF
    coef = table[idx][bucket]
    if has_possession and abs(score_diff) <= 1:
        coef += 0.15 if score_diff >= 0 else -0.15
    return _clamp(1.0 / (1.0 + math.exp(-coef)))


def mlb_run_expectancy_win_prob(
    inning: int,
    outs: int,
    bases: int,
    score_diff: int,
) -> float:
    outs_i = max(0, min(2, outs))
    bases_i = max(0, min(7, bases))
    re = _MLB_RE[outs_i][bases_i]
    innings_left = max(0.5, 9 - min(inning, 9))
    exp_runs = re * innings_left / 3.0
    adj_diff = score_diff + exp_runs * 0.35
    return _clamp(1.0 / (1.0 + math.exp(-adj_diff * 0.85)))


def tennis_win_prob(
    home_sets: int,
    away_sets: int,
    *,
    home_games: int = 0,
    away_games: int = 0,
) -> float:
    """P(home wins match) from sets/games — logistic on set deficit."""

    set_diff = home_sets - away_sets
    game_diff = home_games - away_games
    coef = set_diff * 1.35 + game_diff * 0.08
    return _clamp(1.0 / (1.0 + math.exp(-coef)))


def _infer_yes_is_home(market: Market, game: LiveGame) -> bool:
    title = (market.title or "").lower()
    home_token = _normalize_team(game.home_team)
    away_token = _normalize_team(game.away_team)
    if home_token and home_token in title:
        return True
    if away_token and away_token in title:
        return False
    return "home" in title or game.home_team.lower() in title


def _normalize_team(name: str) -> str:
    return name.lower().split()[-1] if name else ""


def predict_game(
    game: LiveGame,
    market: Market,
    *,
    now: float | None = None,
) -> SportsPrediction | None:
    yes_ask = market.yes_ask
    if yes_ask is None or yes_ask <= 0:
        return None
    kalshi_yes_ask = yes_ask / 100.0
    score_diff = game.home_score - game.away_score
    minutes_left = _minutes_remaining(game)
    yes_is_home = _infer_yes_is_home(market, game)

    soccer_leagues = {"fifa.world", "usa.1", "eng.1", "uefa.champions"}
    basketball_leagues = {"nba", "wnba", "mens-college-basketball", "womens-college-basketball"}

    if game.sport == "soccer" or game.league in soccer_leagues:
        p_home = dixon_coles_home_win_prob(
            game.home_score, game.away_score, period=game.period, minutes_left=minutes_left
        )
        model_yes = p_home if yes_is_home else (1.0 - p_home)
    elif game.league == "mlb":
        sit = game.raw.get("competitions", [{}])[0].get("situation") or {}
        outs = int(sit.get("outs") or 0)
        on_base = sit.get("onBase") or []
        bases = min(7, len(on_base))
        p_home = mlb_run_expectancy_win_prob(max(1, game.period), outs, bases, score_diff)
        model_yes = p_home if yes_is_home else (1.0 - p_home)
    elif game.league in basketball_leagues:
        poss = game.possession and _normalize_team(game.possession) == _normalize_team(game.home_team)
        p_home = win_prob_table(score_diff, minutes_left, sport="nba", has_possession=bool(poss))
        model_yes = p_home if yes_is_home else (1.0 - p_home)
    elif game.league in {"nfl", "college-football"}:
        poss = game.possession and _normalize_team(game.possession) == _normalize_team(game.home_team)
        p_home = win_prob_table(score_diff, minutes_left, sport="nfl", has_possession=bool(poss))
        model_yes = p_home if yes_is_home else (1.0 - p_home)
    elif game.league in {"atp", "wta"} or game.sport == "tennis":
        home_sets = int(game.raw.get("_home_sets") or game.home_score or 0)
        away_sets = int(game.raw.get("_away_sets") or game.away_score or 0)
        p_home = tennis_win_prob(home_sets, away_sets)
        model_yes = p_home if yes_is_home else (1.0 - p_home)
    elif game.league == "nhl":
        p_home = win_prob_table(score_diff, minutes_left, sport="nba")
        model_yes = p_home if yes_is_home else (1.0 - p_home)
    else:
        p_home = 1.0 / (1.0 + math.exp(-score_diff * 1.2))
        model_yes = p_home if yes_is_home else (1.0 - p_home)

    model_yes = _clamp(model_yes)
    edge = model_yes - kalshi_yes_ask
    edge_pct = edge / kalshi_yes_ask * 100.0 if kalshi_yes_ask > 0 else 0.0

    gs = game.to_game_state()
    now = now or time.time()
    age = max(0.0, now - game.last_update)
    freshness = math.exp(-age / max(settings.stale_last_age_seconds, 1))
    high_fit = {
        "nba", "nfl", "mlb", "fifa.world", "atp", "wta",
        "college-football", "mens-college-basketball",
    }
    fit = 0.85 if game.league in high_fit else 0.65
    confidence = _clamp(freshness * fit, 0.0, 1.0)

    return SportsPrediction(
        kalshi_ticker=market.ticker,
        sport=game.league,
        home_team=game.home_team,
        away_team=game.away_team,
        model_yes_prob=model_yes,
        kalshi_yes_ask=kalshi_yes_ask,
        edge_pct=edge_pct,
        confidence=confidence,
        game_state=gs,
        market=market,
    )


class SportsModelEngine:
    """Combines ESPN feed + statistical models into ranked predictions."""

    def __init__(
        self,
        espn: ESPNClient | None = None,
        match_map: dict[str, str] | None = None,
    ) -> None:
        self._espn = espn or ESPNClient()
        self._match_map = match_map if match_map is not None else load_sports_match_map()
        self._predictions: list[SportsPrediction] = []
        self._live_feed: list[dict[str, Any]] = []

    @property
    def predictions(self) -> list[SportsPrediction]:
        return list(self._predictions)

    @property
    def live_feed(self) -> list[dict[str, Any]]:
        return list(self._live_feed)

    async def close(self) -> None:
        await self._espn.close()

    async def get_predictions(
        self,
        kalshi_events: list[Event],
        *,
        now: float | None = None,
    ) -> list[SportsPrediction]:
        """Refresh live sports feed; model predictions only if explicitly enabled."""

        if not settings.sports_model_enabled and not settings.sports_enabled:
            return []

        try:
            games = await self._espn.fetch_scoreboards()
        except Exception as exc:
            log.warning("ESPN feed unreachable: %s", exc)
            self._live_feed = []
            return list(self._predictions)

        live = self._espn.live_games
        feed: list[dict[str, Any]] = []

        for game in live:
            _, ticker = match_game_to_kalshi(
                game, kalshi_events, manual_map=self._match_map
            )
            feed.append(
                {
                    "sport": game.league,
                    "home_team": game.home_team,
                    "away_team": game.away_team,
                    "kalshi_ticker": ticker or None,
                    "game_state": game.to_game_state(),
                }
            )

        self._live_feed = feed

        if not settings.sports_model_enabled:
            self._predictions = []
            return []

        now = now or time.time()
        out: list[SportsPrediction] = []
        for game in live:
            market, _ = match_game_to_kalshi(
                game, kalshi_events, manual_map=self._match_map
            )
            if market is None:
                continue
            pred = predict_game(game, market, now=now)
            if pred is None:
                continue
            if pred.confidence < settings.sports_model_min_confidence:
                continue
            if pred.edge_pct < settings.sports_model_min_edge_pct:
                continue
            out.append(pred)

        self._predictions = out
        return out

    def live_snapshot(self) -> list[dict[str, Any]]:
        """In-progress sports only — scores and clock, no model predictions."""

        return list(self._live_feed)
