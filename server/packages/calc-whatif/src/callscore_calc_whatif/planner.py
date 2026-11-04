"""Planning what-if steps: validate them against a dataset schema into :class:`LogicalMutations`.

The Polars executor (:mod:`.polars`) and the pure-Python reference (:mod:`.reference`) both
execute these plans, so their meaning is defined once.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Literal

from callscore_calc import DatasetSchema, FunctionDef, LimitExceeded, Limits, SpecError
from callscore_calc.ext import (
    NUMERIC,
    Budget,
    Kind,
    LType,
    Node,
    NumericConfig,
    Typed,
    check,
    check_name,
    check_predicate,
    coerce_value,
    columns,
    decimal_places,
    join_path,
    json_value,
    materializable,
    replace_columns,
    to_canonical,
)

from .spec import Disable, Formula, Override, ScenarioStep, Shock


@dataclass(frozen=True)
class WhatIfLimits:
    """Caps on what-if work; exceeding one is a 413."""

    max_steps: int = 500  # extra steps per request
    max_scenario_steps: int = 10_000  # steps in a saved scenario's log
    max_edits: int = 200_000  # overridden cells, per request (scenario plus extra steps)


@dataclass(frozen=True)
class LabeledStep:
    """A scenario step plus where it came from (for error paths and lineage)."""

    label: str
    step: ScenarioStep


@dataclass(frozen=True)
class OverrideBatch:
    """Consecutive overrides, compacted: column -> {key tuple: new value}. Last write wins."""

    edits: dict[str, dict[tuple[Any, ...], Any]]
    labels: tuple[str, ...]


@dataclass(frozen=True)
class ShockOp:
    column: str
    ltype: LType
    op: Literal["add", "mul", "pct"]
    amount: Decimal  # the value to add, or the factor to multiply by (pct already converted)
    where: Typed | None
    round: bool
    label: str


@dataclass(frozen=True)
class FormulaDef:
    name: str
    typed: Typed
    ltype: LType
    label: str


@dataclass
class LogicalMutations:
    ops: list[OverrideBatch | ShockOp]
    formulas: list[FormulaDef]
    base_env: dict[str, LType]
    env: dict[str, LType]
    key_columns: tuple[str, ...]
    edit_keys: list[tuple[Any, ...]]
    canonical: list[Any]
    lineage: dict[str, list[str]] = field(default_factory=dict)

    @property
    def empty(self) -> bool:
        return not self.ops and not self.formulas


def effective_steps(steps: Sequence[LabeledStep], first_seq: int = 1) -> list[LabeledStep]:
    """Drop ``Disable`` steps and the steps they disable.

    Sequence numbers start at ``first_seq``.
    """
    disabled: set[int] = set()
    for offset, item in enumerate(steps):
        if isinstance(item.step, Disable):
            target = item.step.seq
            if not first_seq <= target < first_seq + offset:
                raise SpecError(
                    f"step {first_seq + offset} can only disable an earlier step, not {target}",
                    code="invalid_disable",
                    path=item.label,
                )
            if isinstance(steps[target - first_seq].step, Disable):
                raise SpecError(
                    "a disable step cannot itself be disabled",
                    code="invalid_disable",
                    path=item.label,
                )
            disabled.add(target)
    return [
        item
        for offset, item in enumerate(steps)
        if not isinstance(item.step, Disable) and first_seq + offset not in disabled
    ]


def plan_mutations(
    steps: Sequence[LabeledStep],
    schema: DatasetSchema,
    cfg: NumericConfig,
    limits: WhatIfLimits,
    *,
    expr_limits: Limits | None = None,
    env: Mapping[str, LType] | None = None,
    functions: Mapping[str, FunctionDef] | None = None,
) -> LogicalMutations:
    """Validate scenario steps (``Disable`` already resolved) against a dataset schema.

    ``env`` is the incoming column types (default: the schema's); ``functions`` are plugin
    functions usable in ``where`` and formulas; ``expr_limits`` caps expression size (default:
    the engine defaults).
    """
    expr_limits = expr_limits or Limits()
    base_env = dict(env) if env is not None else schema.ltypes()
    keys = schema.key_columns
    key_types = [base_env[k] for k in keys]
    ops: list[OverrideBatch | ShockOp] = []
    batch: dict[str, dict[tuple[Any, ...], Any]] = {}
    batch_labels: list[str] = []
    definitions: dict[str, tuple[Node, str]] = {}
    expanded: dict[str, Node] = {}
    edit_keys: dict[tuple[Any, ...], None] = {}
    canonical: list[Any] = []
    lineage: dict[str, list[str]] = {}
    n_edits = 0

    def flush() -> None:
        if batch:
            ops.append(OverrideBatch({c: dict(v) for c, v in batch.items()}, tuple(batch_labels)))
            batch.clear()
            batch_labels.clear()

    for item in steps:
        step, label = item.step, item.label
        if isinstance(step, Override):
            if not keys:
                raise SpecError(
                    "this dataset has no key columns, so cells cannot be overridden",
                    code="no_key_columns",
                    path=label,
                )
            n_edits += len(step.edits)
            if n_edits > limits.max_edits:
                raise LimitExceeded(f"more than {limits.max_edits} edits", path=label)
            normalized = []
            for i, edit in enumerate(step.edits):
                path = join_path(label, "edits", i)
                target = _editable(schema, edit.column, join_path(path, "column"))
                if set(edit.key) != set(keys):
                    raise SpecError(
                        f"an edit key needs exactly the key columns {list(keys)}",
                        code="invalid_key",
                        path=join_path(path, "key"),
                    )
                key = tuple(
                    coerce_value(edit.key[k], t, what=f"key {k}")
                    for k, t in zip(keys, key_types, strict=True)
                )
                if any(v is None for v in key):
                    raise SpecError("key values cannot be null", code="invalid_key", path=path)
                value = coerce_value(edit.value, target, what=f"value for {edit.column}")
                batch.setdefault(edit.column, {})[key] = value
                edit_keys[key] = None
                lineage.setdefault(edit.column, []).append(label)
                normalized.append(
                    {
                        "key": {
                            k: json_value(v, t)
                            for k, v, t in zip(keys, key, key_types, strict=True)
                        },
                        "column": edit.column,
                        "value": json_value(value, target),
                    }
                )
            batch_labels.append(label)
            canonical.append({"kind": "override", "edits": normalized})
            continue
        flush()
        if isinstance(step, Shock):
            ltype = _editable(schema, step.column, join_path(label, "column"))
            if ltype.kind not in NUMERIC:
                raise SpecError(
                    f"cannot shock non-numeric column {step.column}",
                    code="type_mismatch",
                    path=join_path(label, "column"),
                )
            amount = step.value if step.op == "add" else step.factor()
            if ltype.kind is Kind.INT and not step.round and amount != amount.to_integral_value():
                raise SpecError(
                    f"{step.column} holds whole numbers; set round=true to round shocked values",
                    code="needs_rounding",
                    path=label,
                )
            if ltype.kind is Kind.DECIMAL and (ltype.scale or 0) + decimal_places(amount) > 38:
                raise SpecError("shock value has too many decimal places", code="invalid_value")
            where = None
            if step.where is not None:
                node = replace_columns(step.where, expanded)
                budget = Budget(expr_limits.max_expr_depth, expr_limits.max_expr_nodes)
                where = check_predicate(
                    node, base_env, cfg, join_path(label, "where"), budget, functions=functions
                )
            ops.append(ShockOp(step.column, ltype, step.op, amount, where, step.round, label))
            lineage.setdefault(step.column, []).append(label)
            canonical.append(to_canonical(step))
        elif isinstance(step, Formula):
            name = step.name
            if name in base_env:
                raise SpecError(
                    f"{name} is a dataset column; a formula needs a new name",
                    code="name_conflict",
                    path=join_path(label, "name"),
                )
            check_name(name, join_path(label, "name"))
            definitions[name] = (step.expr, label)
            expanded[name] = replace_columns(step.expr, expanded)
            lineage.setdefault(name, []).append(label)
            canonical.append(to_canonical(step))
        else:  # Disable steps are resolved before planning.
            raise SpecError("unresolved disable step", code="invalid_disable", path=label)
    flush()

    formulas = _order_formulas(definitions, base_env, cfg, expr_limits, functions or {})
    env = dict(base_env)
    for f in formulas:
        env[f.name] = f.ltype
    return LogicalMutations(
        ops=ops,
        formulas=formulas,
        base_env=base_env,
        env=env,
        key_columns=keys,
        edit_keys=list(edit_keys),
        canonical=canonical,
        lineage=lineage,
    )


def _editable(schema: DatasetSchema, column: str, path: str) -> LType:
    meta = schema.column(column) if column in schema.ltypes() else None
    if meta is None:
        raise SpecError(
            f"unknown column: {column}", code="unknown_column", path=path, detail={"column": column}
        )
    if meta.role == "key":
        raise SpecError(f"key column {column} cannot be changed", code="not_editable", path=path)
    if not meta.editable:
        raise SpecError(f"column {column} is not editable", code="not_editable", path=path)
    return meta.ltype


def _order_formulas(
    definitions: Mapping[str, tuple[Node, str]],
    base_env: Mapping[str, LType],
    cfg: NumericConfig,
    expr_limits: Limits,
    functions: Mapping[str, FunctionDef],
) -> list[FormulaDef]:
    """Type-check formulas in dependency order (ties keep definition order); reject cycles."""
    deps = {name: columns(expr) & set(definitions) for name, (expr, _) in definitions.items()}
    order: list[str] = []
    done: set[str] = set()
    remaining = list(definitions)
    while remaining:
        ready = [n for n in remaining if deps[n] <= done]
        if not ready:
            cycle = sorted(remaining)
            raise SpecError(
                f"formulas depend on each other in a cycle: {cycle}",
                code="formula_cycle",
                path=definitions[cycle[0]][1],
                detail={"formulas": cycle},
            )
        for name in ready:
            order.append(name)
            done.add(name)
        remaining = [n for n in remaining if n not in done]
    env = dict(base_env)
    out = []
    for name in order:
        expr, label = definitions[name]
        budget = Budget(expr_limits.max_expr_depth, expr_limits.max_expr_nodes)
        typed = check(expr, env, cfg, join_path(label, "expr"), budget, functions=functions)
        ltype = materializable(typed, join_path(label, "expr"))
        env[name] = ltype
        out.append(FormulaDef(name, typed, ltype, label))
    return out
