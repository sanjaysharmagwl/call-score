import polars as pl
import pytest

from callscore_calc import CalcEngine, Catalog, SpecError


def test_compare_two_dataset_versions(catalog: Catalog, frame: pl.DataFrame) -> None:
    catalog.register_frame(
        "pos",
        frame.with_columns(
            pl.when(pl.col("id") == 2).then(25).otherwise(pl.col("qty")).alias("qty")
        ),
        key_columns=["id"],
        version="v2",
    )
    engine = CalcEngine(catalog)
    result = engine.compare(
        {
            "dataset": {"id": "pos", "version": "v2"},
            "base": {"version": "v1"},
            "query": {
                "group_by": ["sector"],
                "measures": [{"name": "q", "fn": "sum", "of": "qty"}],
            },
        }
    )
    rows = {r["sector"]: r for r in result.frame.to_dicts()}
    assert rows["Fin"]["q"] == 85 and rows["Fin"]["q__base"] == 80 and rows["Fin"]["q__delta"] == 5
    assert rows["Tech"]["q__delta"] == 0


def test_compare_with_itself_has_no_deltas(engine: CalcEngine) -> None:
    result = engine.compare({"dataset": "pos", "query": {"select": ["id", "qty"]}})
    assert result.frame["qty__delta"].to_list() == [0] * 6


def test_compare_rejects_pivot(engine: CalcEngine) -> None:
    with pytest.raises(SpecError):
        engine.compare(
            {
                "dataset": "pos",
                "query": {
                    "measures": [{"name": "q", "fn": "sum", "of": "qty"}],
                    "pivot": {"on": ["desk"]},
                },
            }
        )


def test_unknown_extensions_are_rejected(engine: CalcEngine) -> None:
    with pytest.raises(SpecError) as info:
        engine.compare({"dataset": "pos", "base": {"extensions": {"nope": {}}}})
    assert info.value.code == "unknown_extension" and info.value.path == "/base/extensions/nope"
