import json

import pytest

from callscore_calc import CalcEngine, SpecError
from callscore_calc.adapters.aggrid import AgGridAdapter
from callscore_calc_whatif import CellEdit, edit_to_override


def test_cell_edits_become_overrides(engine: CalcEngine) -> None:
    schema = engine.schema("pos")
    step = edit_to_override(
        CellEdit(colId="price", newValue="105.5", data={"id": 2, "price": 99.5}), schema
    )
    assert step.edits[0].key == {"id": 2} and step.edits[0].value == "105.50"
    by_row_id = edit_to_override(CellEdit(colId="qty", newValue=7, rowId="r:[3]"), schema)
    assert by_row_id.edits[0].key == {"id": 3}
    for bad in (
        CellEdit(colId="id", newValue=9, data={"id": 1}),
        CellEdit(colId="qty", newValue=1, data={"__group_key": json.dumps("rates")}),
        CellEdit(colId="qty", newValue="many", data={"id": 1}),
        CellEdit(colId="rates_qty", newValue=1, data={"id": 1}),
    ):
        with pytest.raises(SpecError):
            edit_to_override(bad, schema)


def test_grid_rows_see_a_scenario(engine: CalcEngine) -> None:
    extensions = {
        "whatif": {"steps": [{"kind": "shock", "column": "qty", "op": "add", "value": 100}]}
    }
    out = AgGridAdapter().rows(
        engine,
        {
            "startRow": 0,
            "endRow": 10,
            "rowGroupCols": [{"id": "desk", "field": "desk"}],
            "valueCols": [{"id": "qty", "field": "qty", "aggFunc": "sum"}],
            "groupKeys": [],
        },
        dataset="pos",
        extensions=extensions,
    )
    assert {r["desk"]: r["qty"] for r in out.rowData}["rates"] == 230
