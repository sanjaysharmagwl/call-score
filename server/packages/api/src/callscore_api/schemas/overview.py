"""Response models for GET /api/overview."""

from datetime import date, datetime

from pydantic import BaseModel, Field

from callscore_api.schemas.common import Provenance
from callscore_api.schemas.securities import SourceProvenance


class ScoreBucket(BaseModel):
    bucket: str
    low: float
    high: float
    count: int = Field(ge=0)


class AnalystLeaderboardEntry(BaseModel):
    analyst_id: str
    peer_group: str | None = None
    composite_score: float | None = None
    rank_overall: int | None = None
    rank_in_peer_group: int | None = None
    is_ranked: bool = False
    unranked_reason: str | None = None


class AnalystsOverview(BaseModel):
    n_analysts: int = Field(ge=0)
    n_ranked: int = Field(ge=0)
    n_scored: int = Field(ge=0)
    score_distribution: list[ScoreBucket] = []
    top: list[AnalystLeaderboardEntry] = []
    bottom: list[AnalystLeaderboardEntry] = []


class DataCoverage(BaseModel):
    n_sessions: int = Field(ge=0)
    first_session: date | None = None
    last_session: date | None = None
    n_exchanges: int = Field(ge=0)
    n_securities: int = Field(ge=0)
    n_delisted_securities: int = Field(ge=0)
    n_rating_events: int = Field(ge=0)
    latest_event_ts: datetime | None = None
    n_consensus_rows: int = Field(ge=0)
    latest_consensus_date: date | None = None
    n_security_daily_rows: int = Field(ge=0)


class OverviewResponse(BaseModel):
    run: Provenance | None = None
    analysts: AnalystsOverview
    run_counts_by_status: dict[str, int] = {}
    data_coverage: DataCoverage
    provenance: SourceProvenance
