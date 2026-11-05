"""Response models for ``GET /api/leaderboard``."""

from pydantic import BaseModel, Field

from callscore_api.schemas.analysts import FamilyScores, InlineMetric
from callscore_api.schemas.common import Provenance


class LeaderboardRow(BaseModel):
    rank: int | None = Field(default=None, ge=1)
    analyst_id: str
    name: str | None = None
    firm_id: str | None = None
    peer_group: str | None = None
    composite_score: float | None = None
    rank_in_peer_group: int | None = None
    is_ranked: bool = False
    unranked_reason: str | None = None
    n_investable: int = Field(ge=0)
    family_scores: FamilyScores = Field(default_factory=FamilyScores)
    key_metrics: dict[str, InlineMetric] = Field(default_factory=dict)


class LeaderboardResponse(BaseModel):
    items: list[LeaderboardRow]
    total: int = Field(ge=0)
    limit: int = Field(ge=1)
    offset: int = Field(ge=0)
    provenance: Provenance
