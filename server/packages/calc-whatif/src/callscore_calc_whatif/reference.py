"""Pure-Python reference for what-if steps: row at a time, exact ``Decimal`` arithmetic."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from decimal import Decimal
from typing import Any

from callscore_calc.ext import Kind, convert, evaluate, quantize

from .planner import LogicalMutations, OverrideBatch, ShockOp

Row = dict[str, Any]


def apply_mutations(rows: Iterable[Mapping[str, Any]], plan: LogicalMutations) -> list[Row]:
    out = [dict(r) for r in rows]
    keys = plan.key_columns
    index = {tuple(r[k] for k in keys): r for r in out} if keys else {}
    for op in plan.ops:
        if isinstance(op, OverrideBatch):
            for column, edits in op.edits.items():
                for key, value in edits.items():
                    target = index.get(key)
                    if target is not None:
                        target[column] = value
        else:
            for r in out:
                if op.where is None or evaluate(op.where, r) is True:
                    r[op.column] = shock(r[op.column], op)
    for f in plan.formulas:
        for r in out:
            r[f.name] = convert(evaluate(f.typed, r), f.typed.ltype, f.ltype)
    return out


def shock(value: Any, op: ShockOp) -> Any:
    if value is None:
        return None
    kind = op.ltype.kind
    if kind is Kind.FLOAT:
        amount = float(op.amount)
        return value + amount if op.op == "add" else value * amount
    exact = Decimal(value) + op.amount if op.op == "add" else Decimal(value) * op.amount
    if kind is Kind.INT:
        return int(quantize(exact, 0))
    return quantize(exact, op.ltype.scale or 0)
