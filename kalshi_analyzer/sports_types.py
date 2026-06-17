"""Shared sports prediction types (no heavy deps)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .models import Market


@dataclass
class SportsPrediction:
    kalshi_ticker: str
    sport: str
    home_team: str
    away_team: str
    model_yes_prob: float
    kalshi_yes_ask: float
    edge_pct: float
    confidence: float
    game_state: dict[str, Any] = field(default_factory=dict)
    market: Market | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "kalshi_ticker": self.kalshi_ticker,
            "sport": self.sport,
            "home_team": self.home_team,
            "away_team": self.away_team,
            "model_yes_prob": round(self.model_yes_prob, 4),
            "kalshi_yes_ask": round(self.kalshi_yes_ask, 4),
            "edge_pct": round(self.edge_pct, 2),
            "confidence": round(self.confidence, 3),
            "game_state": self.game_state,
        }
