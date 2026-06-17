"""ESPN live scoreboard feed + Kalshi market matching."""

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
from .sports_catalog import LEAGUE_CONFIGS, kalshi_prefixes_for_league

log = logging.getLogger(__name__)

ESPN_BASE = "https://site.api.espn.com/apis/site/v2/sports"


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
    # Last-name match (tennis players)
    if na.split()[-1] == nb.split()[-1]:
        return 0.88
    set_a, set_b = set(na.split()), set(nb.split())
    jacc = len(set_a & set_b) / len(set_a | set_b) if (set_a | set_b) else 0.0
    ratio = SequenceMatcher(None, na, nb).ratio()
    return 0.5 * jacc + 0.5 * ratio


def _competitor_name(c: dict[str, Any]) -> str:
    ent = c.get("athlete") or c.get("team") or {}
    return (
        ent.get("displayName")
        or ent.get("shortName")
        or ent.get("name")
        or ent.get("abbreviation")
        or ""
    ).strip()


def _parse_int_score(raw: Any) -> int:
    if raw in (None, ""):
        return 0
    try:
        return int(raw)
    except (TypeError, ValueError):
        return 0


def _tennis_sets_from_linescores(
    home_lines: list[dict], away_lines: list[dict]
) -> tuple[int, int]:
    home_sets = away_sets = 0
    for h, a in zip(home_lines, away_lines):
        hv = _parse_int_score(h.get("value"))
        av = _parse_int_score(a.get("value"))
        if hv > av:
            home_sets += 1
        elif av > hv:
            away_sets += 1
    return home_sets, away_sets


def _is_placeholder_name(name: str) -> bool:
    n = (name or "").strip().upper()
    return n in {"", "TBD", "?"}


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
    tournament: str = ""
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
            "tournament": self.tournament,
        }


def load_sports_match_map(path: str | None = None) -> dict[str, str]:
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


def normalize_espn_competition(
    comp: dict[str, Any],
    *,
    sport: str,
    league: str,
    event: dict[str, Any] | None = None,
    tournament: str = "",
    bracket: str = "",
) -> LiveGame | None:
    """Normalize one ESPN competition (team game or tennis match)."""

    competitors = comp.get("competitors") or []
    if len(competitors) < 2:
        return None

    home = away = None
    home_lines: list[dict] = []
    away_lines: list[dict] = []
    for c in competitors:
        ha = (c.get("homeAway") or "").lower()
        name = _competitor_name(c)
        score = _parse_int_score(c.get("score"))
        row = {"name": name, "score": score, "linescores": c.get("linescores") or []}
        if ha == "home":
            home = row
            home_lines = row["linescores"]
        elif ha == "away":
            away = row
            away_lines = row["linescores"]
    if not home or not away:
        # Tennis sometimes lists competitors without homeAway — use order
        if sport == "tennis" and len(competitors) >= 2:
            away = {
                "name": _competitor_name(competitors[0]),
                "score": _parse_int_score(competitors[0].get("score")),
                "linescores": competitors[0].get("linescores") or [],
            }
            home = {
                "name": _competitor_name(competitors[1]),
                "score": _parse_int_score(competitors[1].get("score")),
                "linescores": competitors[1].get("linescores") or [],
            }
            away_lines = away["linescores"]
            home_lines = home["linescores"]
        else:
            return None

    if _is_placeholder_name(home["name"]) or _is_placeholder_name(away["name"]):
        status_obj = comp.get("status") or (event or {}).get("status") or {}
        state = ((status_obj.get("type") or {}).get("state") or "").lower()
        if state not in {"in", "live"}:
            return None

    status_obj = comp.get("status") or (event or {}).get("status") or {}
    status_type = status_obj.get("type") or {}
    state = (status_type.get("state") or status_type.get("name") or "").lower()
    detail = status_type.get("shortDetail") or status_type.get("detail") or ""
    period = int(status_obj.get("period") or 0)
    clock = str(status_obj.get("displayClock") or status_obj.get("clock") or detail or "")

    is_live = (
        state in {"in", "live"}
        or "progress" in state
        or (
            status_type.get("completed") is False
            and state not in {"post", "final", "status_final"}
            and period > 0
        )
    )

    home_score = home["score"]
    away_score = away["score"]
    home_sets = away_sets = 0
    if sport == "tennis" and (home_lines or away_lines):
        home_sets, away_sets = _tennis_sets_from_linescores(home_lines, away_lines)
        home_score = home_sets if home_sets or away_sets else home_score
        away_score = away_sets if home_sets or away_sets else away_score

    situation = detail or bracket or None
    possession = None
    sit = comp.get("situation") or {}
    if sit:
        possession = sit.get("possession") or sit.get("team")
        down = sit.get("down")
        dist = sit.get("distance")
        yard = sit.get("yardLine")
        parts = [p for p in (down and f"{down} & {dist}", yard and f"@{yard}", detail) if p]
        if parts:
            situation = " · ".join(str(p) for p in parts)

    game_id = str(comp.get("id") or comp.get("uid") or "")
    if event and not game_id:
        game_id = f"{event.get('id')}-{home['name']}-{away['name']}"

    return LiveGame(
        game_id=game_id,
        sport=sport,
        league=league,
        home_team=home["name"],
        away_team=away["name"],
        home_score=home_score,
        away_score=away_score,
        status=state or "unknown",
        period=period,
        clock=clock,
        possession=str(possession) if possession else None,
        situation=situation,
        is_live=is_live,
        last_update=time.time(),
        tournament=tournament or (event or {}).get("shortName") or "",
        raw={
            "competition": comp,
            "event": event or {},
            "_home_sets": home_sets,
            "_away_sets": away_sets,
        },
    )


def normalize_espn_event(event: dict[str, Any], *, sport: str, league: str) -> LiveGame | None:
    competitions = event.get("competitions") or []
    if not competitions:
        return None
    return normalize_espn_competition(
        competitions[0], sport=sport, league=league, event=event
    )


def normalize_espn_scoreboard(
    payload: dict[str, Any], *, sport: str, league: str
) -> list[LiveGame]:
    games: list[LiveGame] = []
    for event in payload.get("events") or []:
        tournament = event.get("shortName") or event.get("name") or ""
        if sport == "tennis":
            for grouping in event.get("groupings") or []:
                bracket = (grouping.get("grouping") or {}).get("displayName") or ""
                for comp in grouping.get("competitions") or []:
                    g = normalize_espn_competition(
                        comp,
                        sport=sport,
                        league=league,
                        event=event,
                        tournament=tournament,
                        bracket=bracket,
                    )
                    if g is not None:
                        games.append(g)
            continue
        for comp in event.get("competitions") or []:
            g = normalize_espn_competition(
                comp, sport=sport, league=league, event=event, tournament=tournament
            )
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
    manual = manual_map or {}
    if game.game_id in manual:
        ticker = manual[game.game_id]
        for ev in kalshi_events:
            for m in ev.markets:
                if m.ticker == ticker:
                    return m, ticker
        return None, ticker

    prefix_hint = kalshi_prefixes_for_league(game.league)
    tennis = game.sport == "tennis" or game.league in {"atp", "wta"}
    min_sim = 0.45 if tennis else min_similarity

    best_market: Market | None = None
    best_ticker = ""
    best_score = 0.0

    for ev in kalshi_events:
        for m in ev.markets:
            text = _market_search_text(m, ev).upper()
            if not any(p in text for p in prefix_hint):
                continue
            sims = [
                _name_similarity(game.home_team, m.title),
                _name_similarity(game.away_team, m.title),
                _name_similarity(f"{game.away_team} {game.home_team}", m.title),
                _name_similarity(f"{game.away_team} vs {game.home_team}", ev.title),
            ]
            if game.tournament:
                sims.append(_name_similarity(game.tournament, ev.title))
            sim = max(sims)
            if sim > best_score:
                best_score = sim
                best_market = m
                best_ticker = m.ticker

    if best_market and best_score >= min_sim:
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
            url = f"{ESPN_BASE}/{cfg.espn_sport}/{cfg.espn_league}/scoreboard"
            try:
                resp = await self._client.get(url)
                resp.raise_for_status()
                payload = resp.json()
                games = normalize_espn_scoreboard(
                    payload, sport=cfg.espn_sport, league=cfg.espn_league
                )
                live_n = sum(1 for g in games if g.is_live)
                log.debug(
                    "ESPN %s/%s: %d matches (%d live)",
                    cfg.espn_sport,
                    cfg.espn_league,
                    len(games),
                    live_n,
                )
                all_games.extend(games)
            except Exception as exc:
                log.warning(
                    "ESPN fetch failed for %s/%s: %s",
                    cfg.espn_sport,
                    cfg.espn_league,
                    exc,
                )

        live = [g for g in all_games if g.is_live]
        self._current_interval = (
            self._poll_seconds if live else self._backoff_seconds
        )
        self._last_games = all_games
        self._last_fetch = time.time()
        log.info("ESPN feed: %d total matches, %d live", len(all_games), len(live))
        return all_games
