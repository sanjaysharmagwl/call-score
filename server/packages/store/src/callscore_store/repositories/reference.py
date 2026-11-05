"""Bulk ingest for reference data (firms, analysts, spells, securities, scales)."""

from collections.abc import Iterable
from typing import Any

from sqlalchemy import Connection, Engine, select
from sqlalchemy.orm import Session

from callscore_store.models import (
    Analyst,
    AnalystCoverage,
    AnalystEmployment,
    Exchange,
    Firm,
    RatingScale,
    RatingScaleMap,
    Sector,
    Security,
    SecurityClassification,
    SecurityIdentifier,
    TradingSession,
)
from callscore_store.repositories.base import (
    Context,
    normalize_rows,
    table_of,
    unit_of_work,
)
from callscore_store.validation import ValidationError, validate_table

# Insert order respects FK dependencies (immediate enforcement in SQLite).
_REFERENCE_MODELS = (
    Firm,
    Analyst,
    Exchange,
    Sector,
    Security,
    TradingSession,  # needs exchange
    AnalystEmployment,  # needs analyst + firm
    AnalystCoverage,  # needs analyst + security
    SecurityIdentifier,  # needs security
    SecurityClassification,  # needs security + exchange + sector
    RatingScale,  # needs firm
    RatingScaleMap,  # needs rating_scale (scale_id, effective_from)
)

_KWARG_TO_MODEL = {
    "firms": Firm,
    "analysts": Analyst,
    "exchanges": Exchange,
    "sectors": Sector,
    "securities": Security,
    "sessions": TradingSession,
    "employment": AnalystEmployment,
    "coverage": AnalystCoverage,
    "identifiers": SecurityIdentifier,
    "classifications": SecurityClassification,
    "rating_scales": RatingScale,
    "scale_maps": RatingScaleMap,
}


def insert_reference(ctx: Context, **kwargs: Iterable[object]) -> dict[str, int]:
    """Bulk-insert reference rows, grouped by keyword.

    Keywords (all optional, each an iterable of mappings/dataclasses/Polars
    row-dicts)::

        firms, analysts, exchanges, sectors, securities, sessions,
        employment, coverage, identifiers, classifications,
        rating_scales, scale_maps

    Everything is validated first and inserted in ONE transaction in FK-safe
    order; any invalid row rejects the whole call with
    :class:`~callscore_store.validation.ValidationError` and inserts nothing.
    Returns ``{table_name: inserted_count}``.
    """
    unknown = set(kwargs) - set(_KWARG_TO_MODEL)
    if unknown:
        raise ValueError(f"unknown reference groups: {sorted(unknown)}")

    normalized: dict[Any, list[dict[str, Any]]] = {}
    for group, rows in kwargs.items():
        model = _KWARG_TO_MODEL[group]
        normalized_rows = normalize_rows(model, rows)
        if not normalized_rows:
            continue
        table_name = table_of(model).name
        errors, _warnings = validate_table(table_name, normalized_rows)
        if errors:
            raise ValidationError(table_name, errors)
        normalized[model] = normalized_rows

    counts: dict[str, int] = {}
    with unit_of_work(ctx) as conn:
        for model in _REFERENCE_MODELS:
            model_rows = normalized.get(model)
            if not model_rows:
                continue
            table = table_of(model)
            conn.execute(table.insert(), model_rows)
            counts[table.name] = len(model_rows)
    return counts


def known_scale_ids(ctx: Context) -> set[str]:
    """Distinct ``scale_id`` values registered in ``rating_scale``."""
    stmt = select(RatingScale.scale_id).distinct()
    if isinstance(ctx, Session):
        return {r[0] for r in ctx.execute(stmt)}
    if isinstance(ctx, Engine):
        with ctx.connect() as conn:
            return {r[0] for r in conn.execute(stmt)}
    if isinstance(ctx, Connection):
        return {r[0] for r in ctx.execute(stmt)}
    raise TypeError(f"unsupported context type: {type(ctx).__name__}")
