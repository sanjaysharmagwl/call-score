"""A small formula language, parsed safely into the expression tree.

The syntax borrows Python's expression grammar, but only a whitelist of it; nothing is ever
evaluated. Supported:

* column names (``price``, even keywords such as ``yield``), or ``col('Market Value')`` for
  names that aren't identifiers
* numbers (``1.05`` is an exact decimal), strings, ``True``/``False``/``None``,
  ``date('2026-01-31')``, ``datetime('2026-01-31T12:00:00')``
* ``+ - * / **``, comparisons (chains like ``0 < x <= 1`` included), ``and``/``or``/``not``
* ``x in (1, 2)``, ``x not in ('a', 'b')``, ``x is None``, ``x is not None``
* ``a if cond else b``
* functions: ``abs round floor ceil sqrt log exp min max coalesce lower upper contains
  starts_with ends_with``, casts ``int() float() str() decimal(x, scale) to_date()``
"""

from __future__ import annotations

import ast
import io
import keyword
import re
import tokenize

from pydantic import ValidationError

from callscore_calc.errors import SpecError
from callscore_calc.spec.expr import (
    FUNC_ARITY,
    FUNC_NAME_PATTERN,
    Binary,
    Cast,
    CastTarget,
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

MAX_FORMULA_LENGTH = 4000
MAX_DEPTH = 64
MAX_NODES = 2000

_BINOPS: dict[type[ast.operator], str] = {
    ast.Add: "add",
    ast.Sub: "sub",
    ast.Mult: "mul",
    ast.Div: "div",
    ast.Pow: "pow",
}
_CMPOPS: dict[type[ast.cmpop], str] = {
    ast.Eq: "eq",
    ast.NotEq: "ne",
    ast.Lt: "lt",
    ast.LtE: "le",
    ast.Gt: "gt",
    ast.GtE: "ge",
}
_CASTS: dict[str, CastTarget] = {"int": "int", "float": "float", "str": "str", "to_date": "date"}
# Keywords that are part of the formula grammar; every other Python keyword is a column name.
_GRAMMAR_KEYWORDS = frozenset(
    {"and", "or", "not", "in", "is", "if", "else", "None", "True", "False", "lambda"}
)


def parse_formula(text: str) -> Node:
    """Parse a formula into an expression tree, or raise :class:`SpecError`."""
    if len(text) > MAX_FORMULA_LENGTH:
        raise SpecError(
            f"formula is longer than {MAX_FORMULA_LENGTH} characters", code="formula_too_long"
        )
    source = text.strip()
    if not source:
        raise SpecError("formula is empty", code="formula_syntax")
    if source.isidentifier() and source not in ("True", "False", "None"):
        # A bare name is always a column, even a Python keyword such as ``yield``.
        return _Converter(source, text).name(source, ast.Name(id=source))
    source, aliases = _alias_keywords(source)
    try:
        tree = ast.parse(source, mode="eval")
    except SyntaxError as exc:
        raise SpecError(
            f"formula syntax error: {exc.msg}",
            code="formula_syntax",
            detail={"formula": text, "offset": exc.offset},
        ) from None
    except (RecursionError, MemoryError):
        raise SpecError("formula is nested too deeply", code="formula_too_complex") from None
    return _Converter(source, text, aliases).convert(tree.body, 1)


def _alias_keywords(source: str) -> tuple[str, dict[str, str]]:
    """Rename keyword tokens used as column names (``yield``) so Python's parser accepts them."""
    try:
        tokens = list(tokenize.generate_tokens(io.StringIO(source).readline))
    except (tokenize.TokenError, SyntaxError):
        return source, {}  # let ast.parse report the problem
    hits = [
        t
        for t in tokens
        if t.type == tokenize.NAME
        and keyword.iskeyword(t.string)
        and t.string not in _GRAMMAR_KEYWORDS
    ]
    if not hits:
        return source, {}
    lines = source.splitlines(keepends=True)
    starts = [0]
    for line in lines:
        starts.append(starts[-1] + len(line))
    aliases: dict[str, str] = {}
    out = source
    for tok in reversed(hits):
        alias = next(
            f"kw{i}_{tok.string}" for i in range(1000) if f"kw{i}_{tok.string}" not in source
        )
        alias = next((a for a, name in aliases.items() if name == tok.string), alias)
        aliases[alias] = tok.string
        begin = starts[tok.start[0] - 1] + tok.start[1]
        out = out[:begin] + alias + out[begin + len(tok.string) :]
    return out, aliases


class _Converter:
    def __init__(self, source: str, original: str, aliases: dict[str, str] | None = None) -> None:
        self.source = source
        self.original = original
        self.aliases = aliases or {}
        self.nodes = 0

    def fail(self, message: str, node: ast.AST | None = None) -> SpecError:
        detail: dict[str, object] = {"formula": self.original}
        if node is not None and hasattr(node, "col_offset"):
            detail["offset"] = node.col_offset + 1
        return SpecError(message, code="formula_syntax", detail=detail)

    def convert(self, node: ast.expr, level: int) -> Node:
        self.nodes += 1
        if level > MAX_DEPTH or self.nodes > MAX_NODES:
            raise SpecError(
                "formula is too complex",
                code="formula_too_complex",
                detail={"formula": self.original},
            )
        nxt = level + 1
        if isinstance(node, ast.Constant):
            return self.constant(node)
        if isinstance(node, ast.Name):
            return self.name(node.id, node)
        if isinstance(node, ast.UnaryOp):
            return self.unary(node, nxt)
        if isinstance(node, ast.BinOp):
            op = _BINOPS.get(type(node.op))
            if op is None:
                raise self.fail(f"operator {type(node.op).__name__} is not supported", node)
            return Binary(
                op=op,
                left=self.convert(node.left, nxt),
                right=self.convert(node.right, nxt),
            )
        if isinstance(node, ast.BoolOp):
            op_name = "and" if isinstance(node.op, ast.And) else "or"
            args: list[Node] = []
            for value in node.values:
                child = self.convert(value, nxt)
                # Flatten a and (b and c) into one list.
                if isinstance(child, Logic) and child.op == op_name:
                    args.extend(child.args)
                else:
                    args.append(child)
            return Logic(op=op_name, args=tuple(args))
        if isinstance(node, ast.Compare):
            return self.compare(node, nxt)
        if isinstance(node, ast.IfExp):
            return IfElse(
                cond=self.convert(node.test, nxt),
                then=self.convert(node.body, nxt),
                otherwise=self.convert(node.orelse, nxt),
            )
        if isinstance(node, ast.Call):
            return self.call(node, nxt)
        raise self.fail(f"{type(node).__name__} is not allowed in a formula", node)

    def constant(self, node: ast.Constant) -> Lit:
        value = node.value
        if value is None:
            return Lit(type="null")
        if isinstance(value, bool):
            return Lit(type="bool", value=value)
        if isinstance(value, int):
            return Lit(type="int", value=value)
        if isinstance(value, float):
            # Keep exactly what was written: 1.05 means the decimal 1.05, not a binary float.
            text = ast.get_source_segment(self.source, node) or repr(value)
            return Lit(type="num", value=text.replace("_", ""))
        if isinstance(value, str):
            return Lit(type="str", value=value)
        raise self.fail(f"{type(value).__name__} literals are not supported", node)

    def name(self, name: str, node: ast.AST) -> ColRef:
        name = self.aliases.get(name, name)
        if name.startswith("__"):
            raise self.fail(f"names starting with '__' are reserved: {name}", node)
        return ColRef(name=name)

    def unary(self, node: ast.UnaryOp, level: int) -> Node:
        operand = self.convert(node.operand, level)
        if isinstance(node.op, ast.UAdd):
            return operand
        if isinstance(node.op, ast.Not):
            return Unary(op="not", arg=operand)
        if isinstance(node.op, ast.USub):
            if isinstance(operand, Lit) and operand.type in ("int", "num"):
                value = operand.value
                if operand.type == "int":
                    assert isinstance(value, int)
                    return Lit(type="int", value=-value)
                assert isinstance(value, str)
                negated = value[1:] if value.startswith("-") else f"-{value}"
                return Lit(type="num", value=negated)
            return Unary(op="neg", arg=operand)
        raise self.fail("bitwise operators are not supported", node)

    def compare(self, node: ast.Compare, level: int) -> Node:
        left = self.convert(node.left, level)
        special = (ast.In, ast.NotIn, ast.Is, ast.IsNot)
        if len(node.ops) > 1 and any(isinstance(op, special) for op in node.ops):
            raise self.fail("'in' and 'is' cannot be chained with other comparisons", node)
        op, right_ast = node.ops[0], node.comparators[0]
        if isinstance(op, ast.In | ast.NotIn):
            values = self.literal_list(right_ast)
            return InList(arg=left, values=values, negate=isinstance(op, ast.NotIn))
        if isinstance(op, ast.Is | ast.IsNot):
            if not (isinstance(right_ast, ast.Constant) and right_ast.value is None):
                raise self.fail("'is' only works with None: use 'x is None'", right_ast)
            return IsNull(arg=left, negate=isinstance(op, ast.IsNot))
        # a < b <= c means (a < b) and (b <= c).
        parts: list[Node] = []
        for op, right_ast in zip(node.ops, node.comparators, strict=True):
            cmp = _CMPOPS.get(type(op))
            if cmp is None:
                raise self.fail(f"comparison {type(op).__name__} is not supported", node)
            if isinstance(right_ast, ast.Constant) and right_ast.value is None:
                raise self.fail("compare with None using 'is None' / 'is not None'", right_ast)
            right = self.convert(right_ast, level)
            parts.append(Compare(op=cmp, left=left, right=right))
            left = right
        return parts[0] if len(parts) == 1 else Logic(op="and", args=tuple(parts))

    def literal_list(self, node: ast.expr) -> tuple[Lit, ...]:
        if not isinstance(node, ast.Tuple | ast.List | ast.Set):
            raise self.fail("'in' needs a literal list, e.g. x in ('a', 'b')", node)
        values: list[Lit] = []
        for element in node.elts:
            converted = self.convert(element, 2)
            if not isinstance(converted, Lit) or converted.type == "null":
                raise self.fail("'in' lists may only hold non-null literals", element)
            values.append(converted)
        if not values:
            raise self.fail("'in' needs at least one value", node)
        return tuple(values)

    def call(self, node: ast.Call, level: int) -> Node:
        if not isinstance(node.func, ast.Name):
            raise self.fail("only plain function calls are allowed", node)
        if node.keywords:
            raise self.fail("keyword arguments are not supported", node)
        if any(isinstance(a, ast.Starred) for a in node.args):
            raise self.fail("*args are not supported", node)
        fname = node.func.id
        raw = node.args
        if fname in ("col", "date", "datetime") and len(raw) == 1:
            arg = raw[0]
            if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                if fname == "col":
                    return self.name(arg.value, arg)
                try:
                    return Lit(type=fname, value=arg.value)
                except ValueError:
                    raise self.fail(f"invalid {fname} literal: {arg.value!r}", arg) from None
            if fname == "col":
                raise self.fail("col() takes a quoted column name", node)
            if fname == "date":
                return Cast(arg=self.convert(arg, level), to="date")
        if fname in _CASTS:
            if len(raw) != 1:
                raise self.fail(f"{fname}() takes one argument", node)
            return Cast(arg=self.convert(raw[0], level), to=_CASTS[fname])
        if fname == "decimal":
            if len(raw) != 2 or not (
                isinstance(raw[1], ast.Constant) and type(raw[1].value) is int
            ):
                raise self.fail("decimal(x, scale) needs an integer scale", node)
            return Cast(arg=self.convert(raw[0], level), to="decimal", scale=raw[1].value)
        if fname not in FUNC_ARITY and not re.match(FUNC_NAME_PATTERN, fname):
            raise self.fail(f"unknown function: {fname}", node)
        args = tuple(self.convert(a, level) for a in raw)
        try:
            return Func(name=fname, args=args)
        except ValidationError as exc:
            message = str(exc.errors()[0]["msg"]).removeprefix("Value error, ")
            raise self.fail(message, node) from None


# --- Printing ---------------------------------------------------------------------------------

_BIN_SYMBOL = {"add": "+", "sub": "-", "mul": "*", "div": "/", "pow": "**"}
_BIN_PREC = {"add": 5, "sub": 5, "mul": 6, "div": 6, "pow": 8}
_CMP_SYMBOL = {"eq": "==", "ne": "!=", "lt": "<", "le": "<=", "gt": ">", "ge": ">="}


def to_formula(node: Node) -> str:
    """Render an expression tree as formula text (for display, lineage and error messages)."""
    return _fmt(node)[0]


def _wrap(part: tuple[str, int], min_prec: int) -> str:
    text, prec = part
    return f"({text})" if prec < min_prec else text


def _fmt(node: Node) -> tuple[str, int]:
    if isinstance(node, ColRef):
        name = node.name
        if name.isidentifier() and not keyword.iskeyword(name) and not name.startswith("__"):
            return name, 9
        return f"col({name!r})", 9
    if isinstance(node, Lit):
        return _fmt_lit(node)
    if isinstance(node, Binary):
        prec = _BIN_PREC[node.op]
        if node.op == "pow":
            left, right = _wrap(_fmt(node.left), prec + 1), _wrap(_fmt(node.right), prec)
        else:
            left, right = _wrap(_fmt(node.left), prec), _wrap(_fmt(node.right), prec + 1)
        return f"{left} {_BIN_SYMBOL[node.op]} {right}", prec
    if isinstance(node, Unary):
        if node.op == "neg":
            return f"-{_wrap(_fmt(node.arg), 7)}", 7
        return f"not {_wrap(_fmt(node.arg), 3)}", 3
    if isinstance(node, Compare):
        left, right = _wrap(_fmt(node.left), 5), _wrap(_fmt(node.right), 5)
        return f"{left} {_CMP_SYMBOL[node.op]} {right}", 4
    if isinstance(node, Logic):
        prec = 2 if node.op == "and" else 1
        return f" {node.op} ".join(_wrap(_fmt(a), prec + 1) for a in node.args), prec
    if isinstance(node, InList):
        values = ", ".join(_fmt_lit(v)[0] for v in node.values)
        if len(node.values) == 1:
            values += ","
        op = "not in" if node.negate else "in"
        return f"{_wrap(_fmt(node.arg), 5)} {op} ({values})", 4
    if isinstance(node, IsNull):
        op = "is not None" if node.negate else "is None"
        return f"{_wrap(_fmt(node.arg), 5)} {op}", 4
    if isinstance(node, IfElse):
        otherwise = "None" if node.otherwise is None else _wrap(_fmt(node.otherwise), 1)
        return f"{_wrap(_fmt(node.then), 1)} if {_wrap(_fmt(node.cond), 1)} else {otherwise}", 0
    if isinstance(node, Func):
        return f"{node.name}({', '.join(_fmt(a)[0] for a in node.args)})", 9
    inner = _fmt(node.arg)[0]
    if node.to == "decimal":
        return f"decimal({inner}, {node.scale})", 9
    fname = {"int": "int", "float": "float", "str": "str", "date": "to_date"}[node.to]
    return f"{fname}({inner})", 9


def _fmt_lit(node: Lit) -> tuple[str, int]:
    value = node.value
    if node.type == "null":
        return "None", 9
    if node.type == "bool":
        return ("True" if value else "False"), 9
    if node.type == "num":
        text = str(value)
        if "." not in text:
            text += ".0"
        return text, (7 if text.startswith("-") else 9)
    if node.type in ("int", "float"):
        text = repr(value)
        return text, (7 if text.startswith("-") else 9)
    if node.type == "str":
        return repr(value), 9
    return f"{node.type}({str(value)!r})", 9
