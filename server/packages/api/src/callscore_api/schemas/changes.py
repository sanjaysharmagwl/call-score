"""Response models for GET /api/changes (audit/change log)."""

from datetime import datetime

from pydantic import BaseModel

from callscore_api.schemas.common import PaginationMeta
from callscore_api.schemas.securities import SourceProvenance


class ChangeItem(BaseModel):
    """One audit entry: a rating event or a run lifecycle transition.

    ``kind`` is ``"rating"`` or ``"run"``. For ratings, ``type`` is the
    ``RatingAction`` and ``old_rating``/``new_rating`` carry the canonical
    transition; ``is_correction`` flags corrected events explicitly. For runs,
    ``type`` is ``run_created``/``run_succeeded``/``run_failed``/
    ``run_interrupted`` and ``run_id``/``status`` are set.
    """

    change_id: str
    kind: str
    type: str
    ts: datetime
    run_id: str | None = None
    analyst_id: str | None = None
    analyst_name: str | None = None
    firm_id: str | None = None
    security_id: str | None = None
    security_name: str | None = None
    old_rating: str | None = None
    new_rating: str | None = None
    is_correction: bool = False
    status: str | None = None
    detail: str | None = None


class ChangesResponse(BaseModel):
    data: list[ChangeItem]
    pagination: PaginationMeta
    provenance: SourceProvenance | None = None
