"""Hypothesis strategies for fuzzing the engine, and plugins, against the reference.

Needs the ``testing`` extra (``pip install 'callscore-calc[testing]'``)::

    @given(data=st.data())
    def test_my_plugin(data):
        frame = data.draw(frames())
        catalog = Catalog()
        catalog.register_frame("t", frame, key_columns=["k"])
        engine = CalcEngine(catalog, plugins=[MyPlugin()])
        request = {"dataset": "t", "extensions": {...}, "query": data.draw(queries())}
        assert_matches_reference(engine, request)

The strategies draw from the columns :func:`frames` generates; :func:`assert_matches_reference`
runs :func:`callscore_calc.verify.verify`, which executes the plugin transforms' Polars and
reference implementations on the same rows.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Mapping
from decimal import Decimal
from typing import Any

import polars as pl

try:
    from hypothesis import strategies as st
except ImportError as exc:  # pragma: no cover - exercised only without the extra
    raise ImportError(
        "callscore_calc.testing needs hypothesis: pip install 'callscore-calc[testing]'"
    ) from exc

from callscore_calc.config import CalcContext
from callscore_calc.engine import CalcEngine
from callscore_calc.verify import verify

__all__ = [
    "GROUPS",
    "assert_matches_reference",
    "dec_expr",
    "dec_literal",
    "float_expr",
    "frames",
    "measures",
    "predicate",
    "queries",
]

GROUPS = ["a", "b", "c", None]


@st.composite
def frames(draw: st.DrawFn, max_rows: int = 25) -> pl.DataFrame:
    """A random table: key ``k``, groups ``g1`` (string or categorical) and ``g2``, decimal
    ``d``, float ``f``, integer ``i``, ``flag`` and ``day``, with nulls everywhere but ``k``."""
    n = draw(st.integers(0, max_rows))

    def column(values: st.SearchStrategy[Any]) -> list[Any]:
        return draw(st.lists(st.one_of(st.none(), values), min_size=n, max_size=n))

    cents = st.integers(-100_000, 100_000)
    return pl.DataFrame(
        {
            "k": list(range(n)),
            "g1": draw(st.lists(st.sampled_from(GROUPS), min_size=n, max_size=n)),
            "g2": column(st.integers(0, 3)),
            "d": [None if v is None else Decimal(v).scaleb(-2) for v in column(cents)],
            "f": [None if v is None else v / 100 for v in column(cents)],
            "i": column(st.integers(-50, 50)),
            "flag": column(st.booleans()),
            "day": column(st.dates(dt.date(2026, 1, 1), dt.date(2026, 1, 20))),
        },
        schema={
            "k": pl.Int64,
            "g1": draw(st.sampled_from([pl.String, pl.Categorical])),
            "g2": pl.Int64,
            "d": pl.Decimal(12, 2),
            "f": pl.Float64,
            "i": pl.Int64,
            "flag": pl.Boolean,
            "day": pl.Date,
        },
    )


def dec_literal() -> st.SearchStrategy[str]:
    """A decimal literal with one place, e.g. ``"-12.5"``."""
    return st.integers(-500, 500).map(lambda v: str(Decimal(v).scaleb(-1)))


@st.composite
def dec_expr(draw: st.DrawFn, depth: int = 0) -> str:
    """A formula with a decimal result."""
    leaves = ["d", "i", "k", "coalesce(d, 0)", "round(d, 1)", "abs(d)"]
    if depth >= 2 or draw(st.booleans()):
        leaf = draw(st.sampled_from(leaves))
        return draw(st.sampled_from([leaf, f"{leaf} * {draw(dec_literal())}"]))
    op = draw(st.sampled_from(["+", "-", "*", "/"]))
    left = draw(dec_expr(depth + 1))
    right = draw(dec_expr(depth + 1))
    if op == "/":
        return f"({left}) / ({right})"
    return f"({left}) {op} ({right})"


@st.composite
def float_expr(draw: st.DrawFn, depth: int = 0) -> str:
    """A formula with a float result."""
    leaves = ["f", "float(d)", "i * 0.5", "sqrt(abs(f))", "coalesce(f, 1.5)", "round(f, 1)"]
    if depth >= 2 or draw(st.booleans()):
        return draw(st.sampled_from(leaves))
    op = draw(st.sampled_from(["+", "-", "*", "/"]))
    return f"({draw(float_expr(depth + 1))}) {op} ({draw(float_expr(depth + 1))})"


@st.composite
def predicate(draw: st.DrawFn) -> str:
    """A true/false/null condition."""
    atoms = [
        f"d > {draw(dec_literal())}",
        f"f <= {draw(dec_literal())}",
        f"i >= {draw(st.integers(-50, 50))}",
        "g1 == 'a'",
        "g1 in ('b', 'c')",
        "g2 is None",
        "flag",
        "not flag",
        "day >= date('2026-01-10')",
        "d is not None and i > 0",
        "contains(coalesce(g1, ''), 'b')",
    ]
    parts = draw(st.lists(st.sampled_from(atoms), min_size=1, max_size=3))
    joiner = draw(st.sampled_from([" and ", " or "]))
    return joiner.join(f"({p})" for p in parts)


@st.composite
def measures(draw: st.DrawFn) -> list[dict[str, Any]]:
    """One to four measures ``m0``, ``m1``... with every built-in function."""
    out = []
    for i in range(draw(st.integers(1, 4))):
        fn = draw(
            st.sampled_from(
                ["sum", "mean", "min", "max", "count", "count_rows", "count_distinct", "wavg"]
            )
        )
        of = draw(st.one_of(dec_expr(), float_expr()))
        m: dict[str, Any] = {"name": f"m{i}", "fn": fn}
        if fn != "count_rows":
            m["of"] = of
        if fn == "wavg":
            m["of"] = draw(float_expr())
            m["weight"] = draw(st.sampled_from(["abs(f)", "float(i)", "coalesce(f, 1.0)"]))
        if draw(st.booleans()):
            m["where"] = draw(predicate())
        out.append(m)
    return out


@st.composite
def queries(draw: st.DrawFn) -> dict[str, Any]:
    """A random query over :func:`frames` columns: leaf, aggregated, rollup or pivot."""
    query: dict[str, Any] = {}
    if draw(st.booleans()):
        query["filter"] = draw(predicate())
    if draw(st.booleans()):
        query["derive"] = [{"name": "dv", "expr": draw(dec_expr())}]
        if draw(st.booleans()):
            query["derive"][0]["where"] = draw(predicate())
    shape = draw(st.sampled_from(["leaf", "agg", "rollup", "pivot"]))
    if shape == "leaf":
        query["sort"] = draw(
            st.lists(
                st.fixed_dictionaries(
                    {"by": st.sampled_from(["d", "i", "g1", "day"]), "desc": st.booleans()}
                ),
                max_size=2,
                unique_by=lambda s: s["by"],
            )
        )
    else:
        group_by = draw(st.lists(st.sampled_from(["g1", "g2", "flag"]), max_size=2, unique=True))
        if shape in ("rollup", "pivot") and not group_by:
            group_by = ["g1"] if shape == "rollup" else []
        if shape == "pivot" and "g2" in group_by:
            group_by.remove("g2")
        query["group_by"] = group_by
        query["measures"] = draw(measures())
        if draw(st.booleans()):
            ratio = draw(
                st.sampled_from(["float(m0) / 3", "float(m0) / float(total(m0))", "total(m0) - m0"])
            )
            query["post"] = [{"name": "ratio", "expr": ratio}]
        if shape == "rollup":
            query["rollup"] = True
        elif shape == "pivot":
            on = draw(st.sampled_from(["g2", "g1"]))
            if on in group_by:
                group_by.remove(on)
            query["pivot"] = {"on": [on], "totals": draw(st.booleans())}
        elif draw(st.booleans()):
            query["having"] = draw(st.sampled_from(["m0 is not None", "total(m0) is not None"]))
        if shape != "rollup" and group_by:
            query["sort"] = [{"by": group_by[0], "desc": draw(st.booleans())}]
    return query


def assert_matches_reference(
    engine: CalcEngine, request: Mapping[str, Any], ctx: CalcContext | None = None
) -> None:
    """Fail unless the engine and the reference evaluator agree on ``request``."""
    report = verify(engine, request, ctx=ctx)
    assert report.ok, (request, report.problems)
