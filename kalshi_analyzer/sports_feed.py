"""ESPN live scoreboard feed + Kalshi market matching.

Polls ``site.api.espn.com`` scoreboards for major leagues and normalizes
in-progress games into a stable structure for the sports model engine.
"""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

import httpx

from .config import settings
from .models import Event, Market

log = logging.getLogger(__name__)

ESPN_BASE = "https://site.api.espn.com/apis/site/v2/sports"

# sport path, league path, Kalshi series prefix hints
LEAGUE_CONFIGS: tuple[dict[str, str], ...] = (
    {"sport": "football", "league": "nfl", "kalshi_prefix": "KXNFL"},
    {"sport": "basketball", "league": "nba", "kalshi_prefix": "KXNBA"},
    {"sport": "baseball", "league": "mlb", "kalshi_prefix": "KXMLB"},
    {"sport": "hockey", "league": "nhl", "kalshi_prefix": "KXNHL"},
    {"sport": "mma", "league": "ufc", "kalshi_prefix": "KXMMA"},
    {"sport": "soccer", "league": "fifa.world", "kalshi_prefix": "KXWC"},
)


def _normalize_name(text: str) -> str:
    text = text.lower()
    text = re.sub(r"[^a-z0-9 ]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _name_similarity(a: str, b: str) -> float:
    na, nb = _normalize_name(a), _normalize_name(b)
    if not na or not nb:
        return 0.0
    if na in nb or nb in na:
        return 0.92
    set_a, set_b = set(na.split()), set(nb.split())
    jacc = len(set_a & set_b) / len(set_a | set_b) if (set_a | set_b) else 0.0
    ratio = SequenceMatcher(None, na, nb).ratio()
    return 0.5 * jacc + 0.5 * ratio


@dataclass
class LiveGame:
    """Normalized in-progress (or recently live) game from ESPN."""

    game_id: str
    sport: str
    league: str
    home_team: str
    away_team: str
    home_score: int
    away_score: int
    status: str
    period: int
    clock: str
    possession: str | None
    situation: str | None
    is_live: bool
    last_update: float
    raw: dict[str, Any] = field(default_factory=dict)

    def to_game_state(self) -> dict[str, Any]:
        return {
            "game_id": self.game_id,
            "sport": self.sport,
            "league": self.league,
            "home_team": self.home_team,
            "away_team": self.away_team,
            "home_score": self.home_score,
            "away_score": self.away_score,
            "score": f"{self.away_team} {self.away_score} – {self.home_score} {self.home_team}",
            "status": self.status,
            "period": self.period,
            "clock": self.clock,
            "possession": self.possession,
            "situation": self.situation,
            "is_live": self.is_live,
            "last_update": self.last_update,
        }


def load_sports_match_map(path: str | None = None) -> dict[str, str]:
    """Load ``{espn_game_id: kalshi_ticker}`` manual overrides."""

    p = path or settings.sports_match_path
    if not p or not Path(p).exists():
        return {}
    try:
        with open(p) as f:
            data = json.load(f)
        return {str(k): str(v) for k, v in data.items()}
    except Exception as exc:
        log.warning("sports match map %s unreadable: %s", p, exc)
        return {}


def normalize_espn_event(event: dict[str, Any], *, sport: str, league: str) -> LiveGame | None:
    """Convert one ESPN scoreboard event into a :class:`LiveGame`."""

    competitions = event.get("competitions") or []
    if not competitions:
        return None
    comp = competitions[0]
    competitors = comp.get("competitors") or []
    home = away = None
    for c in competitors:
        ha = (c.get("homeAway") or "").lower()
        team = c.get("team") or {}
        name = team.get("displayName") or team.get("name") or team.get("abbreviation") or ""
        score_raw = c.get("score")
        try:
            score = int(score_raw) if score_raw not in (None, "") else 0
        except (TypeError, ValueError):
            score = 0
        row = {"name": name, "score": score, "abbrev": team.get("abbreviation") or ""}
        if ha == "home":
            home = row
        elif ha == "away":
            away = row
    if not home or not away:
        return None

    status_obj = (comp.get("status") or event.get("status") or {})
    status_type = (status_obj.get("type") or {})
    state = (status_type.get("state") or status_type.get("name") or "").lower()
    detail = status_type.get("shortDetail") or status_type.get("detail") or ""
    period = int(status_obj.get("period") or 0)
    clock = str(status_obj.get("displayClock") or status_obj.get("clock") or "")

    is_live = state in {"in", "live"} or "progress" in state or status_type.get("completed") is False and period > 0 and state not in {"post", "final"}

    situation = None
    possession = None
    sit = comp.get("situation") or {}
    if sit:
        possession = sit.get("possession") or sit.get("team") or sit.get("lastPlay", {}).get("team", {}).get("abbreviation")
        down = sit.get("down")
        dist = sit.get("distance")
        yard = sit.get("yardLine")
        parts = [p for p in (down and f"{down} & {dist}", yard and f"@{yard}", detail) if p]
        situation = " · ".join(str(p) for p in parts) if parts else detail or None
    elif detail:
        situation = detail

    return LiveGame(
        game_id=str(event.get("id") or comp.get("id") or ""),
        sport=sport,
        league=league,
        home_team=home["name"],
        away_team=away["name"],
        home_score=home["score"],
        away_score=away["score"],
        status=state or "unknown",
        period=period,
        clock=clock,
        possession=str(possession) if possession else None,
        situation=situation,
        is_live=is_live,
        last_update=time.time(),
        raw=event,
    )


def normalize_espn_scoreboard(
    payload: dict[str, Any], *, sport: str, league: str
) -> list[LiveGame]:
    games: list[LiveGame] = []
    for event in payload.get("events") or []:
        g = normalize_espn_event(event, sport=sport, league=league)
        if g is not None:
            games.append(g)
    return games


def _market_search_text(market: Market, event: Event | None = None) -> str:
    parts = [market.ticker, market.event_ticker, market.title, market.subtitle]
    if event:
        parts.append(event.title)
    return " ".join(p for p in parts if p)


def match_game_to_kalshi(
    game: LiveGame,
    kalshi_events: list[Event],
    *,
    manual_map: dict[str, str] | None = None,
    min_similarity: float = 0.55,
) -> tuple[Market | None, str]:
    """Return ``(matched_market, kalshi_ticker)`` for a live ESPN game."""

    manual = manual_map or {}
    if game.game_id in manual:
        ticker = manual[game.game_id]
        for ev in kalshi_events:
            for m in ev.markets:
                if m.ticker == ticker:
                    return m, ticker
        return None, ticker

    prefix_hint = next(
        (c["kalshi_prefix"] for c in LEAGUE_CONFIGS if c["league"] == game.league),
        game.league.upper(),
    )

    best_market: Market | None = None
    best_ticker = ""
    best_score = 0.0

    for ev in kalshi_events:
        for m in ev.markets:
            text = _market_search_text(m, ev).upper()
            if prefix_hint not in text and not any(
                p in text for p in (f"KX{game.league.upper()}", game.league.upper())
            ):
                continue
            for team in (game.home_team, game.away_team):
                sim_home = _name_similarity(team, m.title)
                sim_event = _name_similarity(f"{game.away_team} {game.home_team}", ev.title)
                sim = max(sim_home, sim_event)
                if sim > best_score:
                    best_score = sim
                    best_market = m
                    best_ticker = m.ticker

    if best_market and best_score >= min_similarity:
        return best_market, best_ticker
    return None, ""


class ESPNClient:
    """Async ESPN scoreboard poller with live/offseason backoff."""

    def __init__(
        self,
        *,
        poll_seconds: float | None = None,
        backoff_seconds: float | None = None,
        timeout: float = 15.0,
    ) -> None:
        self._poll_seconds = poll_seconds or float(settings.espn_poll_seconds)
        self._backoff_seconds = backoff_seconds or float(settings.espn_backoff_seconds)
        self._client = httpx.AsyncClient(
            timeout=timeout,
            headers={"User-Agent": "kalshi-edge-analyzer/0.1"},
        )
        self._last_fetch: float = 0.0
        self._last_games: list[LiveGame] = []
        self._current_interval = self._poll_seconds

    async def close(self) -> None:
        await self._client.aclose()

    @property
    def games(self) -> list[LiveGame]:
        return list(self._last_games)

    @property
    def live_games(self) -> list[LiveGame]:
        return [g for g in self._last_games if g.is_live]

    def _should_fetch(self) -> bool:
        return (time.time() - self._last_fetch) >= self._current_interval

    async def fetch_scoreboards(self) -> list[LiveGame]:
        if not self._should_fetch() and self._last_games:
            return self._last_games

        all_games: list[LiveGame] = []
        for cfg in LEAGUE_CONFIGS:
            url = f"{ESPN_BASE}/{cfg['sport']}/{cfg['league']}/scoreboard"
            try:
                resp = await self._client.get(url)
                resp.raise_for_status()
                payload = resp.json()
                all_games.extend(
                    normalize_espn_scoreboard(
                        payload, sport=cfg["sport"], league=cfg["league"]
                    )
                )
            except Exception as exc:
                log.warning("ESPN fetch failed for %s/%s: %s", cfg["sport"], cfg["league"], exc)

        live = [g for g in all_games if g.is_live]
        self._current_interval = (
            self._poll_seconds if live else self._backoff_seconds
        )
        self._last_games = all_games
        self._last_fetch = time.time()
        return all_games
