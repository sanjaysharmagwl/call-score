"""Custom-benchmark definitions and materialization.

``benchmark_def`` rows of ``kind='custom'`` carry their whole state in
``rule_json`` — the store model has no ``name``/``status``/``config_json``
columns (contract gap; see handoff). The JSON document shape is::

    {
      "name": str | null,
      "status": "draft" | "building" | "ready" | "failed",
      "spec": <BenchmarkSpec.model_dump(mode="json")>,
      "created_at": iso datetime,
      "materialization": {
        "as_of_date": iso date | null,
        "row_count": int,
        "exclusion_counts": {reason: int},
        "materialized_at": iso datetime,
        "revision": int          # increments on every successful build
      } | null,
      "error": {"type": str, "message": str} | null
    }

Custom benchmark ids are ``CSTM_<16 hex>``: the hash is over the canonical
spec dump *excluding* ``benchmark_id``, so identical specs map to the same
id (idempotent create). A client-supplied ``spec.benchmark_id`` is honored
verbatim instead.

Materialization is synchronous (``materialize_benchmark``): it runs
``callscore_engine.benchmarks.build_custom_benchmark`` on a point-in-time
snapshot bounded by ``spec.end_date`` (or the latest trading session), then
replaces that benchmark's ``benchmark_daily`` rows and flips the status —
all in ONE transaction, so a failed build never leaves partial levels.
"""

import hashlib
import json
from collections.abc import Mapping
from contextlib import suppress
from datetime import UTC, date, datetime, time, timedelta
from typing import Any

import polars as pl
from sqlalchemy import Connection, Engine, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from callscore_core.methodology import canonical_dumps
from callscore_engine.benchmarks import (
    BenchmarkFrames,
    BenchmarkSpec,
    build_custom_benchmark,
)
from callscore_store.models import BenchmarkDaily, BenchmarkDef, TradingSession
from callscore_store.repositories.base import Context, table_of, unit_of_work
from callscore_store.snapshots import load_input_snapshot
from callscore_store.validation import ValidationError, validate_table

BENCHMARK_STATUSES = ("draft", "building", "ready", "failed")

_LIQUIDITY_SCHEMA: dict[str, pl.DataType] = {
    "security_id": pl.String(),
    "session_date": pl.Date(),
    "adv_20d": pl.Float64(),
    "free_float": pl.Float64(),
}


# ---------------------------------------------------------------------------
# rule_json document helpers
# ---------------------------------------------------------------------------


def _json_object(raw: object) -> dict[str, Any]:
    if not isinstance(raw, str) or not raw:
        return {}
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return value if isinstance(value, dict) else {}


def _doc(row: Mapping[str, Any]) -> dict[str, Any]:
    """Parse a ``benchmark_def`` row into its ``rule_json`` document."""
    doc = _json_object(row.get("rule_json"))
    if row.get("kind") != "custom":
        # Market/sector defs have no document; surface minimal fields.
        return {
            "name": None,
            "status": None,
            "spec": None,
            "created_at": None,
            "materialization": None,
            "error": None,
        } | doc
    return {
        "name": doc.get("name"),
        "status": doc.get("status") or "draft",
        "spec": doc.get("spec"),
        "created_at": doc.get("created_at"),
        "materialization": doc.get("materialization"),
        "error": doc.get("error"),
    }


def _dumps(doc: Mapping[str, Any]) -> str:
    return json.dumps(doc, sort_keys=True)


def _validated_spec(
    spec: BenchmarkSpec | Mapping[str, Any],
) -> tuple[BenchmarkSpec, dict[str, Any]]:
    """Validate ``spec`` and return ``(spec_obj, spec_map)``.

    ``benchmark_id`` may be omitted from a mapping spec: a deterministic
    ``CSTM_<hash>`` id is generated from the spec *without* the id field and
    injected before validation (``BenchmarkSpec`` requires the field).
    """
    if isinstance(spec, BenchmarkSpec):
        return spec, spec.model_dump(mode="json")
    spec_map: dict[str, Any] = dict(spec)
    if spec_map.get("benchmark_id") is None:
        spec_map = {k: v for k, v in spec_map.items() if k != "benchmark_id"}
        spec_map["benchmark_id"] = benchmark_id_for(spec_map)
    spec_obj = BenchmarkSpec.model_validate(spec_map)
    return spec_obj, spec_obj.model_dump(mode="json")


def benchmark_id_for(spec: Mapping[str, Any]) -> str:
    """Deterministic ``CSTM_<hash>`` id for a spec dict (``benchmark_id`` ignored)."""
    payload = {k: v for k, v in spec.items() if k != "benchmark_id"}
    digest = hashlib.sha256(canonical_dumps(payload).encode()).hexdigest()
    return f"CSTM_{digest[:16]}"


def _get_def(ctx: Context, benchmark_id: str) -> dict[str, Any]:
    stmt = select(table_of(BenchmarkDef)).where(BenchmarkDef.benchmark_id == benchmark_id)
    if isinstance(ctx, Engine):
        with ctx.connect() as conn:
            row = conn.execute(stmt).mappings().first()
    elif isinstance(ctx, Session | Connection):
        row = ctx.execute(stmt).mappings().first()
    else:
        raise TypeError(f"unsupported context type: {type(ctx).__name__}")
    if row is None:
        raise KeyError(f"benchmark {benchmark_id!r} not found")
    return dict(row)


def _update_doc(ctx: Context, benchmark_id: str, doc: Mapping[str, Any]) -> None:
    with unit_of_work(ctx) as conn:
        conn.execute(
            table_of(BenchmarkDef)
            .update()
            .where(table_of(BenchmarkDef).c.benchmark_id == benchmark_id)
            .values(rule_json=_dumps(doc))
        )


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------


def create_benchmark(
    ctx: Context,
    *,
    name: str | None,
    spec: BenchmarkSpec | Mapping[str, Any],
) -> dict[str, Any]:
    """Create a ``kind='custom'`` benchmark definition (status ``draft``).

    ``spec`` may omit ``benchmark_id`` — a deterministic ``CSTM_<hash>`` id is
    then assigned. Re-creating an identical spec returns the existing row with
    ``created=False``; a taken id with a *different* spec is a 422.
    """
    spec_obj, spec_map = _validated_spec(spec)
    benchmark_id = spec_obj.benchmark_id
    existing = _get_def_or_none(ctx, benchmark_id)
    if existing is not None:
        old_spec = _doc(existing).get("spec") or {}
        canonical_new = canonical_dumps(spec_map)
        canonical_old = canonical_dumps(old_spec) if old_spec else None
        if canonical_old == canonical_new:
            return _summary(existing) | {"created": False}
        raise ValueError(f"benchmark_id {benchmark_id!r} already exists with a different spec")

    doc = {
        "name": name,
        "status": "draft",
        "spec": spec_map,
        "created_at": datetime.now(UTC).isoformat(),
        "materialization": None,
        "error": None,
    }
    row = {
        "benchmark_id": benchmark_id,
        "kind": "custom",
        "rule_json": _dumps(doc),
    }
    with unit_of_work(ctx) as conn:
        try:
            conn.execute(table_of(BenchmarkDef).insert().values(**row))
        except IntegrityError as exc:
            raise ValueError(f"benchmark_id {benchmark_id!r} already exists") from exc
    return {
        "benchmark_id": benchmark_id,
        "kind": "custom",
        "name": name,
        "status": "draft",
        "created": True,
    }


def _get_def_or_none(ctx: Context, benchmark_id: str) -> dict[str, Any] | None:
    stmt = select(table_of(BenchmarkDef)).where(BenchmarkDef.benchmark_id == benchmark_id)
    if isinstance(ctx, Engine):
        with ctx.connect() as conn:
            row = conn.execute(stmt).mappings().first()
    elif isinstance(ctx, Session | Connection):
        row = ctx.execute(stmt).mappings().first()
    else:
        raise TypeError(f"unsupported context type: {type(ctx).__name__}")
    return dict(row) if row is not None else None


def _summary(row: Mapping[str, Any]) -> dict[str, Any]:
    doc = _doc(row)
    spec = doc.get("spec") or {}
    return {
        "benchmark_id": str(row["benchmark_id"]),
        "kind": str(row["kind"]),
        "name": doc.get("name"),
        "status": doc.get("status"),
        "preset": spec.get("preset") if isinstance(spec, dict) else None,
        "weighting": spec.get("weighting") if isinstance(spec, dict) else None,
        "rebalance": spec.get("rebalance") if isinstance(spec, dict) else None,
        "row_count": (doc.get("materialization") or {}).get("row_count")
        if isinstance(doc.get("materialization"), dict)
        else None,
    }


def list_benchmarks(
    ctx: Context,
    *,
    status: str | None = None,
    limit: int = 100,
    offset: int = 0,
) -> tuple[list[dict[str, Any]], int]:
    """List custom benchmark definitions (``status`` filters on lifecycle)."""
    tbl = table_of(BenchmarkDef)
    stmt = select(tbl).where(tbl.c.kind == "custom").order_by(tbl.c.benchmark_id.asc())
    if isinstance(ctx, Engine):
        with ctx.connect() as conn:
            rows = [dict(r) for r in conn.execute(stmt).mappings()]
    elif isinstance(ctx, Session | Connection):
        rows = [dict(r) for r in ctx.execute(stmt).mappings()]
    else:
        raise TypeError(f"unsupported context type: {type(ctx).__name__}")
    total = len(rows)
    out = [_summary(r) for r in rows]
    if status is not None:
        out = [r for r in out if r["status"] == status]
        total = len(out)
    return out[offset : offset + limit], total


def get_benchmark(ctx: Context, benchmark_id: str) -> dict[str, Any]:
    """Full detail for one benchmark def; raises ``KeyError`` when unknown."""
    row = _get_def(ctx, benchmark_id)
    doc = _doc(row)
    return _summary(row) | {
        "spec": doc.get("spec"),
        "materialization": doc.get("materialization"),
        "error": doc.get("error"),
        "created_at": doc.get("created_at"),
    }


def benchmark_series(
    ctx: Context,
    benchmark_id: str,
    *,
    from_date: date | None = None,
    to_date: date | None = None,
) -> dict[str, Any]:
    """Materialized ``benchmark_daily`` rows for charts."""
    _get_def(ctx, benchmark_id)  # 404 guard
    tbl = table_of(BenchmarkDaily)
    conds = [tbl.c.benchmark_id == benchmark_id]
    if from_date is not None:
        conds.append(tbl.c.session_date >= from_date)
    if to_date is not None:
        conds.append(tbl.c.session_date <= to_date)
    stmt = (
        select(tbl.c.session_date, tbl.c.open_level, tbl.c.close_level)
        .where(*conds)
        .order_by(tbl.c.session_date.asc())
    )
    if isinstance(ctx, Engine):
        with ctx.connect() as conn:
            rows = [dict(r) for r in conn.execute(stmt).mappings()]
    elif isinstance(ctx, Session | Connection):
        rows = [dict(r) for r in conn.execute(stmt).mappings()]
    else:
        raise TypeError(f"unsupported context type: {type(ctx).__name__}")
    return {"rows": rows, "total": len(rows)}


# ---------------------------------------------------------------------------
# Aux frame + materialization
# ---------------------------------------------------------------------------


def load_liquidity_daily(ctx: Context, as_of_date: date) -> pl.DataFrame:
    """``liquidity_daily`` aux frame (store shape) clipped to ``as_of_date``.

    Columns ``(security_id, session_date, adv_20d, free_float)`` — the shape
    ``callscore_engine.benchmarks.BenchmarkFrames`` expects; there is no frame
    contract for it.
    """
    from callscore_store.models import LiquidityDaily

    stmt = (
        select(LiquidityDaily)
        .where(LiquidityDaily.session_date <= as_of_date)
        .order_by(LiquidityDaily.security_id.asc(), LiquidityDaily.session_date.asc())
    )
    if isinstance(ctx, Engine):
        with ctx.connect() as conn:
            rows = [dict(r) for r in conn.execute(stmt).mappings()]
    elif isinstance(ctx, Session | Connection):
        rows = [dict(r) for r in ctx.execute(stmt).mappings()]
    else:
        raise TypeError(f"unsupported context type: {type(ctx).__name__}")
    records = [
        {
            "security_id": str(r["security_id"]),
            "session_date": r["session_date"],
            "adv_20d": r["adv_20d"],
            "free_float": r["free_float"],
        }
        for r in rows
    ]
    return pl.DataFrame(records, schema=_LIQUIDITY_SCHEMA)


def _max_session_date(ctx: Context) -> date | None:
    stmt = select(func.max(TradingSession.session_date))
    if isinstance(ctx, Engine):
        with ctx.connect() as conn:
            value = conn.execute(stmt).scalar_one_or_none()
    elif isinstance(ctx, Session | Connection):
        value = ctx.execute(stmt).scalar_one_or_none()
    else:
        raise TypeError(f"unsupported context type: {type(ctx).__name__}")
    return value if isinstance(value, date) else None


def _knowledge_cutoff(as_of: date) -> datetime:
    return datetime.combine(as_of + timedelta(days=1), time.min, tzinfo=UTC)


def materialize_benchmark(ctx: Context, benchmark_id: str) -> dict[str, Any]:
    """Build and store ``benchmark_daily`` rows for a custom benchmark.

    Synchronous (documented choice over a job): the build is bounded by the
    spec's date range and dataset size. Idempotent — existing rows for the
    benchmark are deleted and replaced in the same transaction that records
    ``status='ready'``. On engine failure the def is marked ``failed`` with
    the error and the exception propagates (no partial levels are published).
    """
    row = _get_def(ctx, benchmark_id)
    doc = _doc(row)
    spec_raw = doc.get("spec")
    if not isinstance(spec_raw, dict):
        raise ValueError(f"benchmark {benchmark_id!r} has no stored spec")
    spec = BenchmarkSpec.model_validate(spec_raw)
    if str(spec.benchmark_id) != benchmark_id:
        raise ValueError(
            f"stored spec benchmark_id {spec.benchmark_id!r} != row id {benchmark_id!r}"
        )

    as_of = spec.end_date or _max_session_date(ctx)
    if as_of is None:
        raise ValueError("cannot materialize: no trading sessions in store")
    if spec.start_date is not None and spec.start_date > as_of:
        raise ValueError(f"spec.start_date {spec.start_date} is after data end {as_of}")

    doc["status"] = "building"
    doc["error"] = None
    _update_doc(ctx, benchmark_id, doc)

    try:
        snapshot = load_input_snapshot(ctx, as_of, _knowledge_cutoff(as_of))
        frames = snapshot.frames
        build = build_custom_benchmark(
            spec,
            BenchmarkFrames(
                security_daily=frames["security_daily"],
                security_classifications=frames["security_classifications"],
                sessions=frames["sessions"],
                liquidity_daily=load_liquidity_daily(ctx, as_of),
            ),
        )
        records = [
            {
                "benchmark_id": benchmark_id,
                "session_date": r["session_date"],
                "open_level": float(r["open_level"]),
                "close_level": float(r["close_level"]),
            }
            for r in build.frame.iter_rows(named=True)
        ]
        errors, _warnings = validate_table("benchmark_daily", records)
        if errors:
            raise ValidationError("benchmark_daily", errors)

        materialization = {
            "as_of_date": as_of.isoformat(),
            "row_count": len(records),
            "exclusion_counts": dict(build.exclusion_counts),
            "materialized_at": datetime.now(UTC).isoformat(),
            "revision": int((doc.get("materialization") or {}).get("revision") or 0) + 1,
        }
        doc["status"] = "ready"
        doc["materialization"] = materialization
        doc["error"] = None
        with unit_of_work(ctx) as conn:
            conn.execute(
                table_of(BenchmarkDaily)
                .delete()
                .where(table_of(BenchmarkDaily).c.benchmark_id == benchmark_id)
            )
            if records:
                conn.execute(table_of(BenchmarkDaily).insert(), records)
            conn.execute(
                table_of(BenchmarkDef)
                .update()
                .where(table_of(BenchmarkDef).c.benchmark_id == benchmark_id)
                .values(rule_json=_dumps(doc))
            )
    except Exception as exc:
        doc["status"] = "failed"
        doc["error"] = {"type": type(exc).__name__, "message": str(exc)}
        # Best-effort: recording the failure must not mask the real error.
        with suppress(Exception):
            _update_doc(ctx, benchmark_id, doc)
        raise

    return {
        "benchmark_id": benchmark_id,
        "status": "ready",
        "row_count": materialization["row_count"],
        "as_of_date": materialization["as_of_date"],
        "exclusion_counts": materialization["exclusion_counts"],
        "revision": materialization["revision"],
    }


__all__ = [
    "BENCHMARK_STATUSES",
    "benchmark_id_for",
    "benchmark_series",
    "create_benchmark",
    "get_benchmark",
    "list_benchmarks",
    "load_liquidity_daily",
    "materialize_benchmark",
]
