"""Response models for the sectors endpoints.

Pagination/run provenance reuse ``callscore_api.schemas.common``;
``SourceProvenance`` (source-data revisions) comes from
``schemas.securities``.
"""

from pydantic import BaseModel, Field

from callscore_api.schemas.common import PaginationMeta, Provenance
from callscore_api.schemas.securities import SourceProvenance


class SectorItem(BaseModel):
    sector_id: str
    name: str
    parent_sector_id: str | None = None
    security_count: int = Field(ge=0)
    analyst_count: int = Field(ge=0)
    delisted_count: int = Field(ge=0)


class SectorListResponse(BaseModel):
    data: list[SectorItem]
    pagination: PaginationMeta
    provenance: SourceProvenance | None = None


class ScoreBucket(BaseModel):
    bucket: str
    low: float
    high: float
    count: int = Field(ge=0)


class SectorPerformanceResponse(BaseModel):
    sector_id: str
    sector_name: str
    parent_sector_id: str | None = None
    run: Provenance
    n_securities: int = Field(ge=0)
    n_delisted: int = Field(ge=0)
    n_with_prices: int = Field(ge=0)
    n_analysts: int = Field(ge=0)
    n_ranked: int = Field(ge=0)
    mean_composite_score: float | None = None
    composite_score_distribution: list[ScoreBucket] = []
    provenance: SourceProvenance | None = None
