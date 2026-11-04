"""Type checking: turns an expression tree into a :class:`Typed` tree against a column schema.

The typed tree is the single source of truth for types; the Polars compiler and the pure-Python
reference evaluator both consume it, so they cannot disagree about what an expression means.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from callscore_calc.dtypes import (
    BOOL,
    DATE,
    FLOAT,
    INT,
    NULL,
    ORDERABLE,
    STR,
    Kind,
    LType,
    NumericConfig,
    arith_result,
    common_type,
    decimal,
)
from callscore_calc.errors import SpecError, join_path
from callscore_calc.spec.expr import (
    FUNC_ARITY,
    Binary,
    Cast,
    ColRef,
    Compare,
    Func,
    IfElse,
    InList,
    IsNull,
    Lit,
    Logic,
    Node,
    Unary,
)

if TYPE_CHECKING:
    from callscore_calc.plugins import FunctionDef

_LIT_TYPES: dict[str, LType] = {
    "null": NULL,
    "bool": LType(Kind.BOOL, flex=True),
    "int": LType(Kind.INT, flex=True),
    "float": LType(Kind.FLOAT, flex=True),
    "str": LType(Kind.STR, flex=True),
    "date": LType(Kind.DATE, flex=True),
    "datetime": LType(Kind.DATETIME, flex=True),
}


@dataclass(frozen=True)
class Typed:
    """An expression node with its result type and the type its operands are cast to."""

    node: Node
    ltype: LType
    args: tuple[Typed, ...] = ()
    operand: LType | None = None
    impl: Any = None  # the plugin FunctionDef of a plugin function call


@dataclass
class Budget:
    max_depth: int = 64
    max_nodes: int = 5000
    nodes: int = field(default=0)


def lit_type(node: Lit) -> LType:
    if node.type == "num":
        text = str(node.value)
        scale = len(text.split(".", 1)[1]) if "." in text else 0
        return decimal(scale, flex=True)
    return _LIT_TYPES[node.type]


def total_column(measure: str) -> str:
    """The internal column that holds ``total(measure)``: the measure over all rows."""
    return f"__grand__{measure}"


def check(
    node: Node,
    env: Mapping[str, LType],
    cfg: NumericConfig,
    path: str = "",
    budget: Budget | None = None,
    *,
    functions: Mapping[str, FunctionDef] | None = None,
    totals: Mapping[str, LType] | None = None,
) -> Typed:
    """Type-check ``node`` against the columns in ``env`` or raise :class:`SpecError`.

    ``functions`` are plugin functions (see :class:`~callscore_calc.plugins.Registry`).
    ``totals`` are the measures ``total()`` may name; without it ``total()`` is an error (it
    only means something after aggregation, in ``post`` and ``having``).
    """
    budget = budget or Budget()
    return _Checker(env, cfg, budget, functions or {}, totals).check(node, path, 1)


def check_predicate(
    node: Node,
    env: Mapping[str, LType],
    cfg: NumericConfig,
    path: str = "",
    budget: Budget | None = None,
    *,
    functions: Mapping[str, FunctionDef] | None = None,
    totals: Mapping[str, LType] | None = None,
) -> Typed:
    typed = check(node, env, cfg, path, budget, functions=functions, totals=totals)
    if typed.ltype.kind not in (Kind.BOOL, Kind.NULL):
        raise SpecError(
            f"expected a true/false condition, got {typed.ltype}", code="type_mismatch", path=path
        )
    return typed


class _Checker:
    def __init__(
        self,
        env: Mapping[str, LType],
        cfg: NumericConfig,
        budget: Budget,
        functions: Mapping[str, FunctionDef],
        totals: Mapping[str, LType] | None,
    ) -> None:
        self.env = env
        self.cfg = cfg
        self.budget = budget
        self.functions = functions
        self.totals = totals

    def check(self, node: Node, path: str, level: int) -> Typed:
        self.budget.nodes += 1
        if level > self.budget.max_depth or self.budget.nodes > self.budget.max_nodes:
            raise SpecError("expression is too complex", code="expression_too_complex", path=path)
        try:
            return self._check(node, path, level + 1)
        except SpecError as exc:
            if exc.path is None:
                exc.path = path
            raise

    def _check(self, node: Node, path: str, level: int) -> Typed:
        if isinstance(node, ColRef):
            return self._col(node)
        if isinstance(node, Lit):
            return Typed(node, lit_type(node))
        if isinstance(node, Binary):
            left = self.check(node.left, join_path(path, "left"), level)
            right = self.check(node.right, join_path(path, "right"), level)
            result = arith_result(node.op, left.ltype, right.ltype, self.cfg)
            operand = common_type(left.ltype, right.ltype, "arithmetic")
            return Typed(node, result, (left, right), operand)
        if isinstance(node, Unary):
            arg = self.check(node.arg, join_path(path, "arg"), level)
            if node.op == "neg":
                if not (arg.ltype.numeric or arg.ltype.kind is Kind.NULL):
                    raise _mismatch(f"cannot negate {arg.ltype}")
                return Typed(node, arg.ltype, (arg,))
            _need_bool(arg, "not")
            return Typed(node, BOOL, (arg,))
        if isinstance(node, Compare):
            left = self.check(node.left, join_path(path, "left"), level)
            right = self.check(node.right, join_path(path, "right"), level)
            operand = common_type(left.ltype, right.ltype, "a comparison")
            if node.op not in ("eq", "ne") and operand.kind not in ORDERABLE | {Kind.NULL}:
                raise _mismatch(f"{operand} values cannot be ordered")
            return Typed(node, BOOL, (left, right), operand)
        if isinstance(node, Logic):
            args = tuple(
                self.check(a, join_path(path, "args", i), level) for i, a in enumerate(node.args)
            )
            for arg in args:
                _need_bool(arg, node.op)
            return Typed(node, BOOL, args)
        if isinstance(node, InList):
            arg = self.check(node.arg, join_path(path, "arg"), level)
            operand = arg.ltype
            values = []
            for i, value in enumerate(node.values):
                typed = self.check(value, join_path(path, "values", i), level)
                operand = common_type(operand, typed.ltype, "an 'in' list")
                values.append(typed)
            return Typed(node, BOOL, (arg, *values), operand.rigid())
        if isinstance(node, IsNull):
            arg = self.check(node.arg, join_path(path, "arg"), level)
            return Typed(node, BOOL, (arg,))
        if isinstance(node, IfElse):
            cond = self.check(node.cond, join_path(path, "cond"), level)
            _need_bool(cond, "if")
            then = self.check(node.then, join_path(path, "then"), level)
            if node.otherwise is None:
                return Typed(node, then.ltype, (cond, then), then.ltype)
            other = self.check(node.otherwise, join_path(path, "otherwise"), level)
            result = common_type(then.ltype, other.ltype, "the branches of an if")
            return Typed(node, result, (cond, then, other), result)
        if isinstance(node, Func):
            if node.name == "total":
                return self._total(node)
            args = tuple(
                self.check(a, join_path(path, "args", i), level) for i, a in enumerate(node.args)
            )
            return self._func(node, args)
        return self._cast(node, self.check(node.arg, join_path(path, "arg"), level))

    def _col(self, node: ColRef) -> Typed:
        ltype = self.env.get(node.name)
        if ltype is None:
            known = sorted(self.env)
            raise SpecError(
                f"unknown column: {node.name}",
                code="unknown_column",
                detail={"column": node.name, "available": known[:100]},
            )
        if ltype.kind is Kind.OTHER:
            raise SpecError(
                f"column {node.name} has a type the engine cannot compute with",
                code="unsupported_type",
                detail={"column": node.name},
            )
        return Typed(node, ltype)

    def _total(self, node: Func) -> Typed:
        if self.totals is None:
            raise SpecError(
                "total() is only allowed in post and having, after aggregation",
                code="invalid_total",
            )
        arg = node.args[0]
        if not isinstance(arg, ColRef) or arg.name not in self.totals:
            raise SpecError(
                "total() takes the name of a measure, e.g. total(mv)",
                code="invalid_total",
                detail={"measures": sorted(self.totals)},
            )
        return Typed(node, self.totals[arg.name])

    def _func(self, node: Func, args: tuple[Typed, ...]) -> Typed:
        name = node.name
        if name not in FUNC_ARITY:
            return self._plugin_func(node, args)
        first = args[0].ltype
        if name == "abs":
            _need_numeric(first, name)
            return Typed(node, first, args)
        if name == "round":
            _need_numeric(first, name)
            places = 0
            if len(args) == 2:
                digits = args[1].node
                if not (isinstance(digits, Lit) and digits.type == "int"):
                    raise _mismatch("round(x, n) needs a whole-number literal n")
                assert isinstance(digits.value, int)
                places = digits.value
                if not 0 <= places <= 38:
                    raise _mismatch("round(x, n) needs 0 <= n <= 38")
            if first.kind is Kind.DECIMAL:
                return Typed(node, decimal(min(first.scale or 0, places), flex=first.flex), args)
            return Typed(node, first, args)
        if name in ("floor", "ceil"):
            _need_numeric(first, name)
            result = decimal(0, flex=first.flex) if first.kind is Kind.DECIMAL else first
            return Typed(node, result, args)
        if name in ("sqrt", "log", "exp"):
            _need_numeric(first, name)
            if first.kind is Kind.DECIMAL and not first.flex:
                raise _mismatch(f"{name}() needs a float; wrap the argument in float()")
            return Typed(node, LType(Kind.FLOAT, flex=first.flex), args, FLOAT)
        if name in ("min", "max", "coalesce"):
            result = first
            for arg in args[1:]:
                result = common_type(result, arg.ltype, f"{name}()")
            if name != "coalesce" and result.kind not in ORDERABLE | {Kind.NULL}:
                raise _mismatch(f"{name}() needs values that can be ordered")
            return Typed(node, result, args, result)
        if name in ("lower", "upper"):
            _need_kind(first, Kind.STR, name)
            return Typed(node, STR, args)
        for arg in args:
            _need_kind(arg.ltype, Kind.STR, name)
        return Typed(node, BOOL, args)

    def _plugin_func(self, node: Func, args: tuple[Typed, ...]) -> Typed:
        fdef = self.functions.get(node.name)
        if fdef is None:
            raise SpecError(
                f"unknown function: {node.name}",
                code="unknown_function",
                detail={"function": node.name, "available": sorted([*FUNC_ARITY, *self.functions])},
            )
        n = len(args)
        if n < fdef.min_args or (fdef.max_args is not None and n > fdef.max_args):
            raise _mismatch(f"{node.name}() takes {fdef.arity_text()} arguments, got {n}")
        try:
            result = fdef.typecheck([a.ltype for a in args])
        except (TypeError, ValueError) as exc:
            raise _mismatch(f"{node.name}(): {exc}") from None
        if result.kind in (Kind.NULL, Kind.OTHER):
            raise _mismatch(f"{node.name}() must return a concrete type, not {result}")
        return Typed(node, result.rigid(), args, impl=fdef)

    def _cast(self, node: Cast, arg: Typed) -> Typed:
        source = arg.ltype.kind
        allowed: dict[str, set[Kind]] = {
            "int": {Kind.INT, Kind.FLOAT, Kind.DECIMAL, Kind.STR, Kind.BOOL},
            "float": {Kind.INT, Kind.FLOAT, Kind.DECIMAL, Kind.STR},
            "decimal": {Kind.INT, Kind.FLOAT, Kind.DECIMAL, Kind.STR},
            "str": {Kind.INT, Kind.DECIMAL, Kind.STR, Kind.DATE, Kind.BOOL},
            "date": {Kind.DATE, Kind.DATETIME, Kind.STR},
        }
        if source is not Kind.NULL and source not in allowed[node.to]:
            raise _mismatch(f"cannot cast {arg.ltype} to {node.to}")
        target = {
            "int": INT,
            "float": FLOAT,
            "str": STR,
            "date": DATE,
        }.get(node.to) or decimal(node.scale or 0)
        return Typed(node, target, (arg,))


def _mismatch(message: str) -> SpecError:
    return SpecError(message, code="type_mismatch")


def _need_bool(arg: Typed, what: str) -> None:
    if arg.ltype.kind not in (Kind.BOOL, Kind.NULL):
        raise _mismatch(f"'{what}' needs true/false values, got {arg.ltype}")


def _need_numeric(ltype: LType, what: str) -> None:
    if not (ltype.numeric or ltype.kind is Kind.NULL):
        raise _mismatch(f"{what}() needs a number, got {ltype}")


def _need_kind(ltype: LType, kind: Kind, what: str) -> None:
    if ltype.kind not in (kind, Kind.NULL):
        raise _mismatch(f"{what}() needs {kind.value} values, got {ltype}")
