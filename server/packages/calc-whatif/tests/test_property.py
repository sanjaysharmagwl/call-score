"""Property tests: random what-if steps and queries agree with the reference evaluator."""

from typing import Any

from hypothesis import given
from hypothesis import strategies as st

from callscore_calc import CalcEngine, Catalog
from callscore_calc.testing import (
    GROUPS,
    assert_matches_reference,
    dec_expr,
    dec_literal,
    frames,
    predicate,
    queries,
)
from callscore_calc_whatif import WhatIfPlugin


@st.composite
def what_if(draw: st.DrawFn, n: int) -> list[dict[str, Any]]:
    """Up to three random overrides, shocks and formulas over a frame of ``n`` rows."""
    steps: list[dict[str, Any]] = []
    for _ in range(draw(st.integers(0, 3))):
        kind = draw(st.sampled_from(["override", "shock", "formula"]))
        if kind == "override" and n:
            edits = [
                {
                    "key": {"k": draw(st.integers(0, n - 1))},
                    "column": draw(st.sampled_from(["d", "f", "i", "g1"])),
                    "value": None,
                }
                for _ in range(draw(st.integers(1, 3)))
            ]
            for e in edits:
                e["value"] = {
                    "d": draw(st.one_of(st.none(), dec_literal())),
                    "f": draw(st.one_of(st.none(), st.integers(-1000, 1000).map(lambda v: v / 8))),
                    "i": draw(st.one_of(st.none(), st.integers(-9, 9))),
                    "g1": draw(st.sampled_from(GROUPS)),
                }[e["column"]]
            steps.append({"kind": "override", "edits": edits})
        elif kind == "shock":
            column = draw(st.sampled_from(["d", "f", "i"]))
            step = {
                "kind": "shock",
                "column": column,
                "op": draw(st.sampled_from(["add", "mul", "pct"])),
                "value": draw(dec_literal()),
                "round": True,
            }
            if draw(st.booleans()):
                step["where"] = draw(predicate())
            steps.append(step)
        else:
            name = f"x{len(steps)}"
            steps.append({"kind": "formula", "name": name, "expr": draw(dec_expr())})
    return steps


@given(data=st.data())
def test_what_if_matches_reference(data: st.DataObject) -> None:
    frame = data.draw(frames())
    catalog = Catalog()
    catalog.register_frame("t", frame, key_columns=["k"])
    engine = CalcEngine(catalog, plugins=[WhatIfPlugin()])
    request = {
        "dataset": "t",
        "extensions": {
            "whatif": {"steps": data.draw(what_if(frame.height)), "strict_edits": False}
        },
        "query": data.draw(queries()),
        "options": {"deterministic": data.draw(st.booleans())},
    }
    assert_matches_reference(engine, request)
