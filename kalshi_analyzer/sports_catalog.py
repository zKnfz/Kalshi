"""Kalshi sports/league catalog — prefixes and ESPN scoreboard mappings.

Kalshi tickers commonly use ``KX{LEAGUE}`` series codes (e.g. ``KXNBA``,
``KXNBAGAME``). This module centralizes prefix detection and ESPN feed
coverage so filters, matching, and the dashboard stay in sync.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class LeagueConfig:
    """One ESPN scoreboard feed mapped to Kalshi series prefixes."""

    espn_sport: str
    espn_league: str
    kalshi_prefixes: tuple[str, ...]
    display_name: str
    sport_key: str


# Prefixes used on Kalshi sports markets (filter chips, volume floors, matching).
# Includes both short codes (NBA) and Kalshi series stems (KXNBA, KXNBAGAME).
DEFAULT_KALSHI_SPORTS_PREFIXES: tuple[str, ...] = (
    # Pro US leagues
    "KXNBA",
    "KXNBAGAME",
    "NBA",
    "KXNFL",
    "KXNFLGAME",
    "NFL",
    "KXMLB",
    "KXMLBGAME",
    "MLB",
    "KXNHL",
    "KXNHLGAME",
    "NHL",
    # College
    "KXCFB",
    "CFB",
    "KXCBB",
    "KXNCAAB",
    "KXNCAAM",
    "CBB",
    "NCAA",
    "KXNCAAW",
    "NCAAW",
    # Soccer
    "KXWC",
    "KXMLS",
    "KXEPL",
    "KXUCL",
    "KXSOCCER",
    "SOC",
    "FIFA",
    # Tennis
    "KXTEN",
    "KXATP",
    "KXWTA",
    "TEN",
    "ATP",
    "WTA",
    # Golf
    "KXGOLF",
    "KXPGA",
    "KXLPGA",
    "GOLF",
    "PGA",
    # Combat / racing
    "KXMMA",
    "KXUFC",
    "MMA",
    "UFC",
    "KXF1",
    "F1",
    # WNBA
    "KXWNBA",
    "WNBA",
    # Esports (Kalshi-only — no ESPN scoreboard)
    "KXCS2",
    "KXCSGO",
    "CS2",
    "KXVAL",
    "VALORANT",
    "KXLOL",
    "LOL",
    "KXDOTA",
    "DOTA",
    "ESPORTS",
)


# ESPN scoreboards polled when SPORTS_MODEL_ENABLED=true.
LEAGUE_CONFIGS: tuple[LeagueConfig, ...] = (
    LeagueConfig("football", "nfl", ("KXNFL", "KXNFLGAME", "NFL"), "NFL", "nfl"),
    LeagueConfig(
        "football",
        "college-football",
        ("KXCFB", "CFB"),
        "College Football",
        "cfb",
    ),
    LeagueConfig("basketball", "nba", ("KXNBA", "KXNBAGAME", "NBA"), "NBA", "nba"),
    LeagueConfig(
        "basketball",
        "wnba",
        ("KXWNBA", "WNBA"),
        "WNBA",
        "wnba",
    ),
    LeagueConfig(
        "basketball",
        "mens-college-basketball",
        ("KXCBB", "KXNCAAB", "KXNCAAM", "CBB", "NCAA"),
        "NCAA Men's Basketball",
        "cbb",
    ),
    LeagueConfig(
        "basketball",
        "womens-college-basketball",
        ("KXNCAAW", "NCAAW"),
        "NCAA Women's Basketball",
        "cbb",
    ),
    LeagueConfig("baseball", "mlb", ("KXMLB", "KXMLBGAME", "MLB"), "MLB", "mlb"),
    LeagueConfig("hockey", "nhl", ("KXNHL", "KXNHLGAME", "NHL"), "NHL", "nhl"),
    LeagueConfig("mma", "ufc", ("KXMMA", "KXUFC", "MMA", "UFC"), "UFC/MMA", "mma"),
    LeagueConfig(
        "soccer",
        "fifa.world",
        ("KXWC", "FIFA", "KXSOCCER", "SOC"),
        "FIFA / World Cup",
        "soccer",
    ),
    LeagueConfig(
        "soccer",
        "usa.1",
        ("KXMLS", "MLS"),
        "MLS",
        "soccer",
    ),
    LeagueConfig(
        "soccer",
        "eng.1",
        ("KXEPL", "EPL"),
        "Premier League",
        "soccer",
    ),
    LeagueConfig(
        "soccer",
        "uefa.champions",
        ("KXUCL", "UCL"),
        "Champions League",
        "soccer",
    ),
    LeagueConfig("golf", "pga", ("KXGOLF", "KXPGA", "GOLF", "PGA"), "PGA Golf", "golf"),
    LeagueConfig(
        "golf",
        "lpga",
        ("KXLPGA", "LPGA"),
        "LPGA Golf",
        "golf",
    ),
    LeagueConfig("tennis", "atp", ("KXTEN", "KXATP", "TEN", "ATP"), "ATP Tennis", "tennis"),
    LeagueConfig("tennis", "wta", ("KXWTA", "WTA"), "WTA Tennis", "tennis"),
    LeagueConfig("racing", "f1", ("KXF1", "F1"), "Formula 1", "f1"),
)


def kalshi_prefixes_for_league(espn_league: str) -> tuple[str, ...]:
    for cfg in LEAGUE_CONFIGS:
        if cfg.espn_league == espn_league:
            return cfg.kalshi_prefixes
    return (espn_league.upper(),)


def sport_key_for_ticker(ticker: str, event_ticker: str = "") -> str | None:
    """Return a sport key (nba, tennis, …) from Kalshi tickers."""

    text = f"{ticker} {event_ticker}".upper()
    for cfg in LEAGUE_CONFIGS:
        for prefix in cfg.kalshi_prefixes:
            if prefix in text or text.startswith(f"{prefix}-"):
                return cfg.sport_key
    for prefix in DEFAULT_KALSHI_SPORTS_PREFIXES:
        if text.startswith(prefix) or f"{prefix}-" in text:
            upper = prefix.upper()
            if "TEN" in upper or "ATP" in upper or "WTA" in upper:
                return "tennis"
            if "GOLF" in upper or "PGA" in upper:
                return "golf"
            if "CS" in upper or "VAL" in upper or "LOL" in upper or "DOTA" in upper:
                return "esports"
    return None


def sport_icon_for_key(sport_key: str | None) -> str:
    return {
        "nba": "🏀",
        "wnba": "🏀",
        "cbb": "🏀",
        "nfl": "🏈",
        "cfb": "🏈",
        "mlb": "⚾",
        "nhl": "🏒",
        "soccer": "⚽",
        "tennis": "🎾",
        "golf": "⛳",
        "mma": "🥊",
        "f1": "🏎",
        "esports": "🎮",
    }.get(sport_key or "", "🏟")
