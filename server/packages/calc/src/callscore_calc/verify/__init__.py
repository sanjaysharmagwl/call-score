"""Cross-check the engine against the pure-Python reference evaluator.

``verify(engine, request)`` resolves the request exactly as ``engine.run`` would, takes the
dataset rows (a random sample of ``max_rows`` if there are more), runs the Polars engine and the
reference evaluator (including every plugin transform's reference implementation) on those same
rows and reports any difference. Use it in tests, in a
canary job, or before trusting a new Polars version in production.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from callscore_calc.compile.query import TOTAL, leaf_frame
from callscore_calc.config import CalcContext
from callscore_calc.spec.canonical import fingerprint
from callscore_calc.spec.query import CalcRequest

from . import reference
from .invariants import check_pages, check_rollup_totals

if TYPE_CHECKING:
    from callscore_calc.engine import CalcEngine

__all__ = ["VerifyReport", "check_pages", "check_rollup_totals", "reference", "verify"]


@dataclass(frozen=True)
class VerifyReport:
    ok: bool
    fingerprint: str
    input_rows: int
    sampled: bool
    result_rows: int
    problems: tuple[str, ...]


def verify(
    engine: CalcEngine,
    request: CalcRequest | Mapping[str, Any],
    *,
    ctx: CalcContext | None = None,
    max_rows: int = 20_000,
    seed: int = 0,
    rel_tol: float = 1e-9,
    abs_tol: float = 1e-9,
) -> VerifyReport:
    kernel = engine.kernel
    req = kernel.parse(CalcRequest, request)
    ctx = ctx or CalcContext()
    query = req.query.model_copy(update={"page": None})
    with kernel.slot():
        view = kernel.resolve_view(req.dataset, req.extensions, ctx, deadline=None)
        logical = kernel.plan_query(query, view)
        base = view.base.collect()
        sampled = base.height > max_rows
        if sampled:
            base = base.sample(n=max_rows, seed=seed)
        frame = base.lazy()
        for t in view.transforms:
            frame = t.plan.apply(frame)
        if logical.aggregated:
            finished = kernel.evaluate_aggregate(
                frame,
                logical,
                engine="in-memory",
                deadline=None,
                deterministic=req.options.deterministic,
            )
            actual = finished.frame.to_dicts()
            columns = finished.columns
            fields = finished.pivot_fields
        else:
            limits = kernel.config.limits
            df = leaf_frame(frame, logical, limits, row_limit=max_rows + 1).collect()
            actual = df.drop(TOTAL).to_dicts()
            columns = dict(logical.output)
            fields = None
    rows = base.to_dicts()
    for t in view.transforms:
        rows = t.plan.apply_reference(rows)
    expected = reference.run_query(rows, logical)
    problems = []
    if fields != expected.pivot_fields:
        problems.append(f"pivot fields differ: engine {fields}, reference {expected.pivot_fields}")
    if list(columns) != list(expected.columns):
        problems.append(
            f"columns differ: engine {list(columns)}, reference {list(expected.columns)}"
        )
    problems += reference.diff_rows(
        actual, expected.rows, expected.columns, rel_tol=rel_tol, abs_tol=abs_tol
    )
    identity = {"dataset": view.identity(), "query": query, "sample": [sampled, seed, max_rows]}
    return VerifyReport(
        ok=not problems,
        fingerprint=fingerprint(identity),
        input_rows=base.height,
        sampled=sampled,
        result_rows=len(actual),
        problems=tuple(problems),
    )
