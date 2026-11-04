"""Expression tree: the canonical, JSON-serializable form of every formula.

Anywhere the spec takes an expression it also accepts a formula string (``"price * qty"``), which
is parsed into this tree by :func:`callscore_calc.spec.formula.parse_formula`. Fingerprints are
always taken over the tree, never over the text.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterator, Mapping
from decimal import Decimal
from typing import Annotated, Any, Literal

from pydantic import BeforeValidator, Field, model_validator
from pydantic_core import PydanticCustomError

from callscore_calc.dtypes import BinaryOp, canonical_decimal, parse_decimal
from callscore_calc.spec.base import Model

LitType = Literal["null", "bool", "int", "num", "float", "str", "date", "datetime"]
CmpOp = Literal["eq", "ne", "lt", "le", "gt", "ge"]
CastTarget = Literal["int", "float", "decimal", "str", "date"]
FuncName = Literal[
    "abs",
    "round",
    "floor",
    "ceil",
    "sqrt",
    "log",
    "exp",
    "min",
    "max",
    "coalesce",
    "lower",
    "upper",
    "contains",
    "starts_with",
    "ends_with",
    "total",
]

# Plugin function names: lower-case identifiers, never starting with an underscore.
FUNC_NAME_PATTERN = r"^[a-z][a-z0-9_]*$"

# (min, max) argument counts of the built-in functions; None means unbounded.
FUNC_ARITY: dict[str, tuple[int, int | None]] = {
    "abs": (1, 1),
    "round": (1, 2),
    "floor": (1, 1),
    "ceil": (1, 1),
    "sqrt": (1, 1),
    "log": (1, 1),
    "exp": (1, 1),
    "min": (2, None),
    "max": (2, None),
    "coalesce": (2, None),
    "lower": (1, 1),
    "upper": (1, 1),
    "contains": (2, 2),
    "starts_with": (2, 2),
    "ends_with": (2, 2),
    "total": (1, 1),
}


class ColRef(Model):
    kind: Literal["col"] = "col"
    name: str = Field(min_length=1)


class Lit(Model):
    """A literal. ``num`` is an exact decimal written as a string, e.g. ``"1.05"``."""

    kind: Literal["lit"] = "lit"
    type: LitType
    value: str | int | float | bool | None = None

    @model_validator(mode="before")
    @classmethod
    def _normalize(cls, data: Any) -> Any:
        if isinstance(data, dict) and "type" in data:
            return {**data, "value": _normalize_literal(data["type"], data.get("value"))}
        return data


def _normalize_literal(kind: object, value: object) -> object:
    if kind == "null":
        if value is not None:
            raise ValueError("a null literal has no value")
        return None
    if value is None:
        raise ValueError(f"a {kind} literal needs a value")
    if kind == "bool":
        if not isinstance(value, bool):
            raise ValueError("expected true or false")
        return value
    if kind == "int":
        if isinstance(value, bool):
            raise ValueError("expected an integer")
        if isinstance(value, int):
            return value
        if isinstance(value, str) and value.strip().lstrip("-").isdigit():
            return int(value)
        raise ValueError("expected an integer")
    if kind == "num":
        return canonical_decimal(parse_decimal(value))
    if kind == "float":
        return float(parse_decimal(value))
    if kind == "str":
        if not isinstance(value, str):
            raise ValueError("expected a string")
        return value
    if kind == "date":
        if isinstance(value, dt.date) and not isinstance(value, dt.datetime):
            return value.isoformat()
        if isinstance(value, str):
            return dt.date.fromisoformat(value.strip()).isoformat()
        raise ValueError("expected an ISO date")
    if kind == "datetime":
        parsed = value if isinstance(value, dt.datetime) else None
        if isinstance(value, str):
            parsed = dt.datetime.fromisoformat(value.strip())
        if parsed is None or parsed.tzinfo is not None:
            raise ValueError("expected an ISO datetime without a time zone")
        return parsed.isoformat()
    return value


class Binary(Model):
    kind: Literal["binary"] = "binary"
    op: BinaryOp
    left: Expr
    right: Expr


class Unary(Model):
    kind: Literal["unary"] = "unary"
    op: Literal["neg", "not"]
    arg: Expr


class Compare(Model):
    kind: Literal["cmp"] = "cmp"
    op: CmpOp
    left: Expr
    right: Expr


class Logic(Model):
    kind: Literal["logic"] = "logic"
    op: Literal["and", "or"]
    args: tuple[Expr, ...] = Field(min_length=2)


class InList(Model):
    """``arg in (values...)``; the values are non-null literals."""

    kind: Literal["in"] = "in"
    arg: Expr
    values: tuple[Lit, ...] = Field(min_length=1)
    negate: bool = False

    @model_validator(mode="after")
    def _no_null(self) -> InList:
        if any(v.type == "null" for v in self.values):
            raise ValueError("'in' lists cannot contain null; use 'is None' instead")
        return self


class IsNull(Model):
    kind: Literal["is_null"] = "is_null"
    arg: Expr
    negate: bool = False


class IfElse(Model):
    """``then if cond else otherwise``; a null condition picks ``otherwise``."""

    kind: Literal["if"] = "if"
    cond: Expr
    then: Expr
    otherwise: Expr | None = None


class Func(Model):
    """A function call. Built-in names are :data:`FuncName`; any other name must be a function
    registered by a plugin, which is checked when the expression is type-checked."""

    kind: Literal["func"] = "func"
    name: str = Field(pattern=FUNC_NAME_PATTERN)
    args: tuple[Expr, ...]

    @model_validator(mode="after")
    def _arity(self) -> Func:
        if self.name not in FUNC_ARITY:
            return self
        low, high = FUNC_ARITY[self.name]
        n = len(self.args)
        if n < low or (high is not None and n > high):
            want = f"{low}" if low == high else f"{low}+" if high is None else f"{low}-{high}"
            raise ValueError(f"{self.name}() takes {want} arguments, got {n}")
        return self


class Cast(Model):
    kind: Literal["cast"] = "cast"
    arg: Expr
    to: CastTarget
    scale: int | None = Field(default=None, ge=0, le=38)

    @model_validator(mode="after")
    def _scale(self) -> Cast:
        if (self.to == "decimal") != (self.scale is not None):
            raise ValueError("a decimal cast needs a scale, and only a decimal cast takes one")
        return self


Node = ColRef | Lit | Binary | Unary | Compare | Logic | InList | IsNull | IfElse | Func | Cast


def _coerce(value: Any) -> Any:
    if isinstance(value, str):
        from callscore_calc.errors import SpecError
        from callscore_calc.spec.formula import parse_formula

        try:
            return parse_formula(value)
        except SpecError as exc:
            # Re-raise as a pydantic error so the location of the bad formula is reported.
            message = exc.message.replace("{", "{{").replace("}", "}}")
            raise PydanticCustomError(exc.code, message) from None
    if value is None or isinstance(value, bool | int | float | Decimal):
        return lit(value)
    return value


Expr = Annotated[Node, Field(discriminator="kind"), BeforeValidator(_coerce)]
"""An expression field: a tree node, a formula string, or a bare number/bool/None literal."""

for _model in (Binary, Unary, Compare, Logic, InList, IsNull, IfElse, Func, Cast):
    _model.model_rebuild()


# --- Builders ---------------------------------------------------------------------------------


def col(name: str) -> ColRef:
    return ColRef(name=name)


def lit(value: object) -> Lit:
    """Literal from a Python value. Floats become exact decimals as written (``0.1`` is 0.1)."""
    if value is None:
        return Lit(type="null")
    if isinstance(value, bool):
        return Lit(type="bool", value=value)
    if isinstance(value, int):
        return Lit(type="int", value=value)
    if isinstance(value, float | Decimal):
        return Lit(type="num", value=canonical_decimal(parse_decimal(value)))
    if isinstance(value, str):
        return Lit(type="str", value=value)
    if isinstance(value, dt.datetime):
        return Lit(type="datetime", value=value.isoformat())
    if isinstance(value, dt.date):
        return Lit(type="date", value=value.isoformat())
    raise TypeError(f"cannot make a literal from {type(value).__name__}")


# --- Traversal --------------------------------------------------------------------------------


def children(node: Node) -> tuple[Node, ...]:
    if isinstance(node, Binary | Compare):
        return (node.left, node.right)
    if isinstance(node, Unary | IsNull | Cast):
        return (node.arg,)
    if isinstance(node, InList):
        return (node.arg, *node.values)
    if isinstance(node, Logic | Func):
        return node.args
    if isinstance(node, IfElse):
        rest = () if node.otherwise is None else (node.otherwise,)
        return (node.cond, node.then, *rest)
    return ()


def walk(node: Node) -> Iterator[Node]:
    stack = [node]
    while stack:
        current = stack.pop()
        yield current
        stack.extend(reversed(children(current)))


def columns(node: Node) -> set[str]:
    """Names of all columns an expression reads."""
    return {n.name for n in walk(node) if isinstance(n, ColRef)}


def replace_columns(node: Node, mapping: Mapping[str, Node]) -> Node:
    """Substitute column references (used to inline formula definitions)."""
    if isinstance(node, ColRef):
        return mapping.get(node.name, node)
    if isinstance(node, Lit):
        return node
    if isinstance(node, Binary | Compare):
        return node.model_copy(
            update={
                "left": replace_columns(node.left, mapping),
                "right": replace_columns(node.right, mapping),
            }
        )
    if isinstance(node, Unary | IsNull | Cast | InList):
        return node.model_copy(update={"arg": replace_columns(node.arg, mapping)})
    if isinstance(node, Logic | Func):
        return node.model_copy(
            update={"args": tuple(replace_columns(a, mapping) for a in node.args)}
        )
    otherwise = None if node.otherwise is None else replace_columns(node.otherwise, mapping)
    return node.model_copy(
        update={
            "cond": replace_columns(node.cond, mapping),
            "then": replace_columns(node.then, mapping),
            "otherwise": otherwise,
        }
    )


def depth(node: Node) -> int:
    kids = children(node)
    return 1 + (max(depth(k) for k in kids) if kids else 0)
