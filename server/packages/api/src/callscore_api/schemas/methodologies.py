"""Schemas for the methodology registry endpoints (``/api/methodologies``)."""

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from callscore_api.schemas.common import PaginationMeta
from callscore_core.methodology import Methodology


class MethodologyVersionSummary(BaseModel):
    """One immutable ``methodology_version`` row."""

    id: int
    fingerprint: str
    is_baseline: bool
    created_at: datetime
    params: Methodology | None = None
    """Stored parameters re-validated as :class:`Methodology`; ``None`` only if
    the stored JSON fails validation (should not happen via the API)."""


class MethodologyListResponse(BaseModel):
    data: list[MethodologyVersionSummary]
    pagination: PaginationMeta


class MethodologyCreateRequest(BaseModel):
    """Body of ``POST /api/methodologies``.

    ``methodology`` must be a complete, valid :class:`Methodology` payload —
    partial overrides are not accepted (use ``Methodology.with_overrides``
    client-side or submit an inline methodology to ``POST /api/runs``).
    ``is_baseline`` may only be set when no baseline exists yet; stored
    versions are immutable, so this endpoint never replaces a baseline.
    """

    model_config = ConfigDict(extra="forbid")

    methodology: Methodology
    is_baseline: bool = False


class FieldChange(BaseModel):
    """One leaf-level difference between two methodology payloads.

    ``a_missing``/``b_missing`` mark keys present on only one side; the
    corresponding value is then ``None``.
    """

    path: str
    """Dot/bracket path, e.g. ``position_weights.buy`` or ``fixed_horizons_days[0]``."""
    a: Any = None
    b: Any = None
    a_missing: bool = False
    b_missing: bool = False


class MethodologyDiffResponse(BaseModel):
    """Field-level diff of two stored methodology versions."""

    a_fingerprint: str
    b_fingerprint: str
    identical: bool
    changes: list[FieldChange] = Field(default_factory=list)


__all__ = [
    "FieldChange",
    "MethodologyCreateRequest",
    "MethodologyDiffResponse",
    "MethodologyListResponse",
    "MethodologyVersionSummary",
]
