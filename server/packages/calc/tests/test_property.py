"""Property tests: on random data and random specs, the engine agrees with the reference."""

from hypothesis import given
from hypothesis import strategies as st

from callscore_calc import CalcEngine, Catalog
from callscore_calc.testing import assert_matches_reference, frames, predicate, queries


@given(data=st.data())
def test_engine_matches_reference(data: st.DataObject) -> None:
    frame = data.draw(frames())
    catalog = Catalog()
    catalog.register_frame("t", frame, key_columns=["k"])
    engine = CalcEngine(catalog)
    request = {
        "dataset": "t",
        "query": data.draw(queries()),
        "options": {"deterministic": data.draw(st.booleans())},
    }
    assert_matches_reference(engine, request)


@given(data=st.data())
def test_keyless_rows_match_reference(data: st.DataObject) -> None:
    frame = data.draw(frames()).drop("k")
    catalog = Catalog()
    catalog.register_frame("t", frame)
    engine = CalcEngine(catalog)
    request = {
        "dataset": "t",
        "query": {"filter": data.draw(predicate()), "derive": [{"name": "v", "expr": "f * 2"}]},
    }
    assert_matches_reference(engine, request)
