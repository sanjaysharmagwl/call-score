"""Bulk ingest for market data (daily prices, benchmarks, delistings, FX, ...)."""

from collections.abc import Iterable
from typing import Any

from callscore_store.models import (
    BenchmarkDaily,
    BenchmarkDef,
    ConsensusDaily,
    CorporateAction,
    Delisting,
    FactorReturnDaily,
    FxRateDaily,
    LiquidityDaily,
    SecurityBeta,
    SecurityDaily,
)
from callscore_store.repositories.base import (
    Context,
    normalize_rows,
    table_of,
    unit_of_work,
)
from callscore_store.validation import ValidationError, validate_table

# FK-safe insert order (securities/analyst reference data must already exist).
_MARKET_MODELS = (
    BenchmarkDef,
    SecurityDaily,
    CorporateAction,
    Delisting,
    BenchmarkDaily,  # needs benchmark_def
    FactorReturnDaily,
    SecurityBeta,
    FxRateDaily,
    LiquidityDaily,
    ConsensusDaily,
)

_KWARG_TO_MODEL = {
    "benchmark_defs": BenchmarkDef,
    "security_daily": SecurityDaily,
    "corporate_actions": CorporateAction,
    "delistings": Delisting,
    "benchmark_daily": BenchmarkDaily,
    "factor_returns": FactorReturnDaily,
    "security_beta": SecurityBeta,
    "fx_daily": FxRateDaily,
    "liquidity_daily": LiquidityDaily,
    "consensus_daily": ConsensusDaily,
}


def insert_market(ctx: Context, **kwargs: Iterable[object]) -> dict[str, int]:
    """Bulk-insert market rows, grouped by keyword.

    Keywords::

        benchmark_defs, security_daily, corporate_actions, delistings,
        benchmark_daily, factor_returns, security_beta, fx_daily,
        liquidity_daily, consensus_daily

    Validated first, inserted in ONE transaction in FK-safe order; any invalid
    row rejects the whole call. Returns ``{table_name: inserted_count}``.
    """
    unknown = set(kwargs) - set(_KWARG_TO_MODEL)
    if unknown:
        raise ValueError(f"unknown market groups: {sorted(unknown)}")

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
        for model in _MARKET_MODELS:
            model_rows = normalized.get(model)
            if not model_rows:
                continue
            table = table_of(model)
            conn.execute(table.insert(), model_rows)
            counts[table.name] = len(model_rows)
    return counts
