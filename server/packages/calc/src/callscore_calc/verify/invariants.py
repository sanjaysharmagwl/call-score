"""Invariants any correct result must satisfy, independent of the reference evaluator."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from callscore_calc.compile.logical import LEVEL
from callscore_calc.result import CalcResult

if TYPE_CHECKING:
    from callscore_calc.engine import CalcEngine


def check_rollup_totals(
    result: CalcResult, group_by: Sequence[str], additive: Sequence[str], *, tol: float = 1e-6
) -> list[str]:
    """Each subtotal of an additive measure (sum, count) equals the sum of its children."""
    rows = result.frame.to_dicts()
    problems = []
    for depth in range(len(group_by)):
        parents = [r for r in rows if r[LEVEL] == depth]
        children = [r for r in rows if r[LEVEL] == depth + 1]
        for parent in parents:
            prefix = [parent[g] for g in group_by[:depth]]
            members = [c for c in children if [c[g] for g in group_by[:depth]] == prefix]
            for measure in additive:
                values = [c[measure] for c in members if c[measure] is not None]
                expected = sum(
                    values, Decimal(0) if any(isinstance(v, Decimal) for v in values) else 0
                )
                actual = parent[measure]
                if not values:
                    if actual not in (None, 0):
                        problems.append(
                            f"level {depth} {prefix} {measure}: {actual} but no children"
                        )
                    continue
                if abs(float(actual) - float(expected)) > tol * max(1.0, abs(float(expected))):
                    problems.append(
                        f"level {depth} {prefix} {measure}: subtotal {actual}, "
                        f"children sum {expected}"
                    )
    return problems


def check_pages(engine: CalcEngine, request: Mapping[str, Any], page_size: int) -> list[str]:
    """Paging through a view yields each row exactly once, in the unpaged order."""
    full_request = {**request, "query": {**request.get("query", {}), "page": None}}
    full = engine.run(full_request).frame.to_dicts()
    seen: list[dict[str, Any]] = []
    offset = 0
    while True:
        query = {**request.get("query", {}), "page": {"offset": offset, "limit": page_size}}
        page = engine.run({**request, "query": query})
        if page.meta.total_rows != len(full):
            return [f"total_rows {page.meta.total_rows} but the view has {len(full)} rows"]
        rows = page.frame.to_dicts()
        seen.extend(rows)
        offset += page_size
        if offset >= len(full):
            break
    if seen != full:
        return ["pages do not add up to the unpaged view"]
    return []
