"""AG Grid cell edits as what-if overrides (grid set up with ``readOnlyEdit: true``)."""

from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel, ConfigDict

from callscore_calc import DatasetSchema, SpecError
from callscore_calc.ext import coerce_value, json_value

from .spec import Edit, Override


class CellEdit(BaseModel):
    """The useful part of AG Grid's ``CellEditRequestEvent``."""

    model_config = ConfigDict(extra="ignore")

    colId: str
    newValue: Any = None
    data: dict[str, Any] | None = None
    rowId: str | None = None


def edit_to_override(edit: CellEdit, schema: DatasetSchema) -> Override:
    """Turn a grid cell edit into an override step (leaf rows only)."""
    keys = schema.key_columns
    if not keys:
        raise SpecError("editing needs a dataset with key columns", code="no_key_columns")
    data = edit.data or {}
    if "__group_key" in data:
        raise SpecError("group rows cannot be edited; edit the rows inside", code="not_editable")
    types = schema.ltypes()
    if edit.colId not in types:
        raise SpecError(f"column {edit.colId} cannot be edited", code="not_editable")
    meta = schema.column(edit.colId)
    if not meta.editable:
        raise SpecError(f"column {edit.colId} is not editable", code="not_editable")
    if all(k in data for k in keys):
        raw = [data[k] for k in keys]
    elif edit.rowId and edit.rowId.startswith("r:"):
        raw = json.loads(edit.rowId[2:])
    else:
        raise SpecError("the edit carries no row key", code="invalid_key")
    key = {
        k: json_value(coerce_value(v, types[k], what=f"key {k}"), types[k])
        for k, v in zip(keys, raw, strict=True)
    }
    value = coerce_value(edit.newValue, meta.ltype, what=f"value for {edit.colId}")
    return Override(edits=(Edit(key=key, column=edit.colId, value=json_value(value, meta.ltype)),))
