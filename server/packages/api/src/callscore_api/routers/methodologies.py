"""Methodology registry endpoints (``/api/methodologies``).

    GET  /methodologies            list stored versions + fingerprints
    POST /methodologies            validate + store a new immutable version
    GET  /methodologies/diff?a=&b= field-level diff of two versions
    GET  /methodologies/{fingerprint}  one version's full payload

``/methodologies/diff`` is registered BEFORE ``/methodologies/{fingerprint}``
so the literal path wins.

Immutability: ``methodology_version`` rows are never updated. ``POST`` is
idempotent on the canonical fingerprint — re-posting identical params returns
the existing row (200) rather than inserting a duplicate. ``is_baseline`` can
only be set when no baseline exists; it is never used to replace one.
"""

import json
from typing import Annotated, Any

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from sqlalchemy import Engine, func, select

from callscore_api.deps import get_engine
from callscore_api.errors import ApiError
from callscore_api.schemas.common import PaginationMeta, PaginationParams, get_pagination
from callscore_api.schemas.methodologies import (
    FieldChange,
    MethodologyCreateRequest,
    MethodologyDiffResponse,
    MethodologyListResponse,
    MethodologyVersionSummary,
)
from callscore_core.methodology import Methodology
from callscore_store.models import MethodologyVersion
from callscore_store.repositories import runs as runs_repo
from callscore_store.repositories.base import table_of

router = APIRouter(prefix="/methodologies", tags=["methodologies"])

EngineDep = Annotated[Engine, Depends(get_engine)]
PaginationDep = Annotated[PaginationParams, Depends(get_pagination)]


def _params_dict(raw: object) -> dict[str, Any]:
    """Parse stored ``params_json`` into a plain dict ({} on any anomaly)."""
    if not isinstance(raw, str) or not raw:
        return {}
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return value if isinstance(value, dict) else {}


def _methodology_params(raw: object) -> Methodology | None:
    payload = _params_dict(raw)
    if not payload:
        return None
    try:
        return Methodology.model_validate(payload)
    except Exception:  # noqa: BLE001 - degrade to params=None rather than 500
        return None


def _version_summary(row: dict[str, Any]) -> MethodologyVersionSummary:
    return MethodologyVersionSummary(
        id=int(row["id"]),
        fingerprint=str(row["fingerprint"]),
        is_baseline=bool(row["is_baseline"]),
        created_at=row["created_at"],
        params=_methodology_params(row.get("params_json")),
    )


def _version_by_fingerprint(engine: Engine, fingerprint: str) -> dict[str, Any] | None:
    with engine.connect() as conn:
        row = (
            conn.execute(
                select(MethodologyVersion).where(MethodologyVersion.fingerprint == fingerprint)
            )
            .mappings()
            .first()
        )
    return dict(row) if row is not None else None


def _params_for_fingerprint(engine: Engine, fingerprint: str) -> dict[str, Any]:
    row = _version_by_fingerprint(engine, fingerprint)
    if row is None:
        raise KeyError(f"methodology {fingerprint!r} not found")
    return _params_dict(row.get("params_json"))


def _baseline_exists(engine: Engine, *, exclude_fingerprint: str | None = None) -> bool:
    tbl = table_of(MethodologyVersion)
    stmt = select(func.count()).select_from(tbl).where(tbl.c.is_baseline.is_(True))
    if exclude_fingerprint is not None:
        stmt = stmt.where(tbl.c.fingerprint != exclude_fingerprint)
    with engine.connect() as conn:
        return int(conn.execute(stmt).scalar_one()) > 0


# ---------------------------------------------------------------------------
# Field-level diff
# ---------------------------------------------------------------------------


def _diff_values(a: Any, b: Any, path: str, out: list[FieldChange]) -> None:
    if isinstance(a, dict) and isinstance(b, dict):
        for key in sorted(set(a) | set(b), key=str):
            child = f"{path}.{key}" if path else str(key)
            if key not in a:
                out.append(FieldChange(path=child, a=None, b=b[key], a_missing=True))
            elif key not in b:
                out.append(FieldChange(path=child, a=a[key], b=None, b_missing=True))
            else:
                _diff_values(a[key], b[key], child, out)
        return
    if isinstance(a, list) and isinstance(b, list):
        for i in range(max(len(a), len(b))):
            child = f"{path}[{i}]"
            if i >= len(a):
                out.append(FieldChange(path=child, a=None, b=b[i], a_missing=True))
            elif i >= len(b):
                out.append(FieldChange(path=child, a=a[i], b=None, b_missing=True))
            else:
                _diff_values(a[i], b[i], child, out)
        return
    if a != b:
        out.append(FieldChange(path=path or "$", a=a, b=b))


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.get("", response_model=MethodologyListResponse)
def list_methodologies(
    engine: EngineDep,
    pagination: PaginationDep,
) -> MethodologyListResponse:
    """List stored methodology versions (creation order) with fingerprints."""
    tbl = table_of(MethodologyVersion)
    with engine.connect() as conn:
        total = int(conn.execute(select(func.count()).select_from(tbl)).scalar_one())
        rows = [
            dict(r)
            for r in conn.execute(
                select(MethodologyVersion)
                .order_by(MethodologyVersion.id)
                .limit(pagination.limit)
                .offset(pagination.offset)
            ).mappings()
        ]
    return MethodologyListResponse(
        data=[_version_summary(r) for r in rows],
        pagination=PaginationMeta(
            limit=pagination.limit,
            offset=pagination.offset,
            count=len(rows),
            total=total,
        ),
    )


@router.post(
    "",
    status_code=201,
    response_model=MethodologyVersionSummary,
    responses={
        200: {
            "model": MethodologyVersionSummary,
            "description": "Identical methodology already stored (idempotent)",
        }
    },
)
def create_methodology(
    body: MethodologyCreateRequest, engine: EngineDep
) -> MethodologyVersionSummary | JSONResponse:
    """Validate and store a methodology version (immutable, fingerprint-keyed)."""
    methodology = body.methodology
    fingerprint = methodology.fingerprint()

    existing = _version_by_fingerprint(engine, fingerprint)
    if existing is not None:
        if body.is_baseline and not existing["is_baseline"]:
            raise ApiError(
                409,
                "baseline_conflict",
                "methodology already exists but is not the baseline; "
                "versions are immutable and cannot be promoted",
            )
        return JSONResponse(
            status_code=200,
            content=_version_summary(existing).model_dump(mode="json"),
        )

    if body.is_baseline and _baseline_exists(engine):
        raise ApiError(
            409,
            "baseline_exists",
            "a baseline methodology version already exists; refusing to replace it",
        )

    version_id = runs_repo.register_methodology_version(
        engine, methodology, is_baseline=body.is_baseline
    )
    row = _version_by_fingerprint(engine, fingerprint)
    if row is None:  # pragma: no cover - register just wrote it
        raise KeyError(f"methodology_version {version_id} not found after insert")
    return _version_summary(row)


@router.get("/diff", response_model=MethodologyDiffResponse)
def diff_methodologies(a: str, b: str, engine: EngineDep) -> MethodologyDiffResponse:
    """Field-level diff between two stored methodology versions (404 if either
    fingerprint is unknown)."""
    params_a = _params_for_fingerprint(engine, a)
    params_b = _params_for_fingerprint(engine, b)
    changes: list[FieldChange] = []
    _diff_values(params_a, params_b, "", changes)
    return MethodologyDiffResponse(
        a_fingerprint=a,
        b_fingerprint=b,
        identical=not changes,
        changes=changes,
    )


@router.get("/{fingerprint}", response_model=MethodologyVersionSummary)
def get_methodology(fingerprint: str, engine: EngineDep) -> MethodologyVersionSummary:
    """Fetch one stored methodology version by fingerprint (404 if unknown)."""
    row = _version_by_fingerprint(engine, fingerprint)
    if row is None:
        raise KeyError(f"methodology {fingerprint!r} not found")
    return _version_summary(row)


__all__ = ["router"]
