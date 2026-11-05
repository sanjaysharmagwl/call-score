"""Meta/catalog endpoints (``/api/meta/*``) plus ``GET /api/ready``.

GET /meta/enums               every callscore_core.enums StrEnum
GET /meta/metrics             metric catalog + composite spec + fingerprint
GET /meta/methodology-defaults  built-in methodology presets
GET /ready                    readiness probe (db, schema, counts, last run)
"""

from enum import StrEnum
from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy import Engine, func, select, text

import callscore_core.enums as core_enums
from callscore_api.deps import get_engine
from callscore_api.schemas.meta import (
    EnumCatalog,
    LastRunInfo,
    MethodologyDefaultsResponse,
    MetricCatalogEntry,
    MetricCatalogResponse,
    ReadinessCheck,
    ReadinessResponse,
)
from callscore_core.enums import RunStatus
from callscore_core.methodology import Methodology
from callscore_core.metrics import (
    COMPOSITE_SPEC,
    METRIC_CATALOG_FINGERPRINT,
    METRIC_DEFINITIONS,
    MetricDefinition,
)
from callscore_store.models import CalcRun, MethodologyVersion
from callscore_store.repositories.base import table_by_name
from callscore_store.schema import SCHEMA_VERSION, current_version

router = APIRouter(tags=["meta"])

EngineDep = Annotated[Engine, Depends(get_engine)]

# Tables summarized by the readiness probe (kept small and stable).
_READINESS_TABLES = (
    "firm",
    "analyst",
    "security",
    "rating_event",
    "security_daily",
    "benchmark_daily",
    "methodology_version",
    "calc_run",
)


def _enum_catalog() -> EnumCatalog:
    enums: dict[str, list[str]] = {}
    for name, obj in vars(core_enums).items():
        if isinstance(obj, type) and obj is not StrEnum and issubclass(obj, StrEnum):
            enums[name] = [member.value for member in obj]
    return EnumCatalog(enums=enums)


def _metric_entry(metric: MetricDefinition) -> MetricCatalogEntry:
    return MetricCatalogEntry(
        code=metric.code,
        family=metric.family,
        unit=metric.unit,
        direction=metric.direction,
        horizon=metric.horizon,
        required_inputs=list(metric.required_inputs),
        formula=metric.formula,
        estimand=metric.estimand,
        unavailable_when=metric.unavailable_when,
        min_obs=metric.minimum_observations,
        scored=metric.inference != "none",
        contributes_to_composite=metric.in_composite,
        inference=metric.inference,
        shrink=metric.shrink,
        aggregate=metric.aggregate,
        null_value=metric.null_value,
    )


def _table_counts(engine: Engine) -> dict[str, int]:
    counts: dict[str, int] = {}
    with engine.connect() as conn:
        for name in _READINESS_TABLES:
            try:
                table = table_by_name(name)
            except ValueError:
                continue
            counts[name] = int(conn.execute(select(func.count()).select_from(table)).scalar_one())
    return counts


def _last_successful_run(engine: Engine) -> LastRunInfo | None:
    with engine.connect() as conn:
        row = (
            conn.execute(
                select(
                    CalcRun.run_id,
                    CalcRun.as_of_date,
                    CalcRun.finished_at,
                    MethodologyVersion.fingerprint,
                )
                .join(MethodologyVersion, CalcRun.methodology_version_id == MethodologyVersion.id)
                .where(CalcRun.status == RunStatus.SUCCEEDED.value)
                .order_by(CalcRun.finished_at.desc().nullslast(), CalcRun.run_id.desc())
                .limit(1)
            )
            .mappings()
            .first()
        )
    if row is None:
        return None
    return LastRunInfo(
        run_id=str(row["run_id"]),
        as_of_date=row["as_of_date"],
        finished_at=row["finished_at"],
        methodology_fingerprint=(
            str(row["fingerprint"]) if row["fingerprint"] is not None else None
        ),
    )


@router.get("/meta/enums", response_model=EnumCatalog)
def get_enums() -> EnumCatalog:
    """All enums from ``callscore_core.enums`` as ``{name: [values]}``."""
    return _enum_catalog()


@router.get("/meta/metrics", response_model=MetricCatalogResponse)
def get_metrics() -> MetricCatalogResponse:
    """The metric catalog: definitions plus the composite spec fingerprint."""
    return MetricCatalogResponse(
        metrics=[_metric_entry(d) for d in METRIC_DEFINITIONS],
        composite_spec=COMPOSITE_SPEC.model_dump(mode="json"),
        fingerprint=METRIC_CATALOG_FINGERPRINT,
    )


@router.get("/meta/methodology-defaults", response_model=MethodologyDefaultsResponse)
def get_methodology_defaults() -> MethodologyDefaultsResponse:
    """Built-in methodology presets (``"default"`` -> ``Methodology()``)."""
    default = Methodology()
    return MethodologyDefaultsResponse(
        methodology=default,
        fingerprint=default.fingerprint(),
        presets=["default"],
    )


@router.get("/ready", response_model=ReadinessResponse)
def readiness(engine: EngineDep) -> ReadinessResponse:
    """Readiness probe: db reachable, schema current, row counts, last run.

    Distinct from ``/api/health`` (cheap liveness used by the web shell) —
    this endpoint touches the database. Always returns HTTP 200; consumers
    should check ``status == "ready"``.
    """
    checks: list[ReadinessCheck] = []
    db_ok = False
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        checks.append(ReadinessCheck(name="database", ok=True))
        db_ok = True
    except Exception as exc:  # noqa: BLE001 - report, don't propagate
        checks.append(ReadinessCheck(name="database", ok=False, detail=type(exc).__name__))

    schema_version: int | None = None
    if db_ok:
        try:
            version = current_version(engine)
            schema_version = version
            checks.append(
                ReadinessCheck(
                    name="schema_version",
                    ok=version == SCHEMA_VERSION,
                    detail=f"{version} (expected {SCHEMA_VERSION})",
                )
            )
        except Exception as exc:  # noqa: BLE001 - e.g. UnmanagedSchemaError
            checks.append(ReadinessCheck(name="schema_version", ok=False, detail=str(exc)[:200]))

    row_counts: dict[str, int] = {}
    last_run: LastRunInfo | None = None
    if db_ok:
        try:
            row_counts = _table_counts(engine)
        except Exception as exc:  # noqa: BLE001
            checks.append(ReadinessCheck(name="row_counts", ok=False, detail=type(exc).__name__))
        try:
            last_run = _last_successful_run(engine)
        except Exception as exc:  # noqa: BLE001
            checks.append(ReadinessCheck(name="last_run", ok=False, detail=type(exc).__name__))

    return ReadinessResponse(
        status="ready" if all(c.ok for c in checks) else "degraded",
        checks=checks,
        schema_version=schema_version,
        expected_schema_version=SCHEMA_VERSION,
        row_counts=row_counts,
        last_successful_run=last_run,
    )


__all__ = ["router"]
