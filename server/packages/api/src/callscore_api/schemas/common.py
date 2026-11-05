"""Shared schema primitives for callscore API request/response models.

Every list endpoint returns ``{data, pagination}``; detail endpoints may add a
``provenance`` block (run id, methodology fingerprint, as-of/knowledge
timestamps) so clients can trace results back to inputs. Error bodies are
``ErrorBody`` — see ``callscore_api.errors``.
"""

from datetime import date, datetime
from typing import Annotated, Any, Generic, TypeVar

from fastapi import Query
from pydantic import BaseModel, Field

# Offset pagination; ``limit`` is hard-capped at 500.
DEFAULT_PAGE_LIMIT = 50
MAX_PAGE_LIMIT = 500


class PaginationParams(BaseModel):
    """Resolved ``limit``/``offset`` query parameters."""

    limit: int = Field(default=DEFAULT_PAGE_LIMIT, ge=1, le=MAX_PAGE_LIMIT)
    offset: int = Field(default=0, ge=0)


def get_pagination(
    limit: Annotated[
        int,
        Query(ge=1, le=MAX_PAGE_LIMIT, description=f"Page size (max {MAX_PAGE_LIMIT})"),
    ] = DEFAULT_PAGE_LIMIT,
    offset: Annotated[int, Query(ge=0, description="Row offset")] = 0,
) -> PaginationParams:
    """FastAPI dependency resolving ``?limit=&offset=`` into ``PaginationParams``."""
    return PaginationParams(limit=limit, offset=offset)


class PaginationMeta(BaseModel):
    """Pagination block echoed back on list responses."""

    limit: int
    offset: int
    count: int
    """Number of items in this page (``len(data)``)."""
    total: int | None = None
    """Total matching rows when cheaply countable, else ``None``."""


# Free-form sort column name; routers whitelist against their own column sets.
SortParam = Annotated[str, Field(min_length=1, max_length=64)]


class Provenance(BaseModel):
    """Optional input-provenance block for detail/compare responses."""

    run_id: str | None = None
    methodology_fingerprint: str | None = None
    as_of_date: date | None = None
    knowledge_cutoff: datetime | None = None
    source_revision: str | None = None


T = TypeVar("T")


class Envelope(BaseModel, Generic[T]):  # noqa: UP046 - pydantic generic; PEP-695 params unverified
    """Generic list envelope: ``{data, pagination?, provenance?}``."""

    data: T
    pagination: PaginationMeta | None = None
    provenance: Provenance | None = None


class ErrorBody(BaseModel):
    """Shape of every error response produced by ``callscore_api.errors``."""

    detail: str
    code: str
    errors: list[dict[str, Any]] | None = None


__all__ = [
    "DEFAULT_PAGE_LIMIT",
    "MAX_PAGE_LIMIT",
    "Envelope",
    "ErrorBody",
    "PaginationMeta",
    "PaginationParams",
    "Provenance",
    "SortParam",
    "get_pagination",
]
