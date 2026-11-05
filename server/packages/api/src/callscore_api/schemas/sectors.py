"""Response models for the sectors endpoints.

NOTE (merge): shares ``Pagination``/``Provenance``/``RunProvenanceBlock``
shapes with ``schemas.securities``; expected to converge on
``callscore_api.schemas.common`` once T13's foundation lands.
"""

from pydantic import BaseModel, Field

from callscore_api.schemas.securities import Pagination, Provenance, RunProvenanceBlock


class SectorItem(BaseModel):
    sector_id: str
    name: str
    parent_sector_id: str | None = None
    security_count: int = Field(ge=0)
    analyst_count: int = Field(ge=0)
    delisted_count: int = Field(ge=0)


class SectorListResponse(BaseModel):
    data: list[SectorItem]
    pagination: Pagination
    provenance: Provenance | None = None


class ScoreBucket(BaseModel):
    bucket: str
    low: float
    high: float
    count: int = Field(ge=0)


class SectorPerformanceResponse(BaseModel):
    sector_id: str
    sector_name: str
    parent_sector_id: str | None = None
    run: RunProvenanceBlock
    n_securities: int = Field(ge=0)
    n_delisted: int = Field(ge=0)
    n_with_prices: int = Field(ge=0)
    n_analysts: int = Field(ge=0)
    n_ranked: int = Field(ge=0)
    mean_composite_score: float | None = None
    composite_score_distribution: list[ScoreBucket] = []
    provenance: Provenance | None = None
