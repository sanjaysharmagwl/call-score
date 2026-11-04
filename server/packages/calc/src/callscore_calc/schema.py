"""Dataset schema: column types plus the metadata a UI needs to build column definitions."""

from __future__ import annotations

from collections.abc import Collection, Mapping
from typing import Literal

import polars as pl
from pydantic import BaseModel, ConfigDict

from callscore_calc.dtypes import NUMERIC, Kind, LType, from_polars
from callscore_calc.errors import SpecError

Role = Literal["key", "dimension", "measure"]


class ColumnMeta(BaseModel):
    model_config = ConfigDict(frozen=True)

    name: str
    dtype: str
    type: str
    kind: Kind
    scale: int | None = None
    role: Role
    editable: bool
    source: Literal["base", "formula"] = "base"

    @property
    def ltype(self) -> LType:
        return LType(self.kind, self.scale)


class DatasetSchema(BaseModel):
    model_config = ConfigDict(frozen=True)

    dataset: str
    version: str
    key_columns: tuple[str, ...]
    columns: tuple[ColumnMeta, ...]

    def ltypes(self) -> dict[str, LType]:
        return {c.name: c.ltype for c in self.columns}

    def column(self, name: str) -> ColumnMeta:
        for c in self.columns:
            if c.name == name:
                return c
        raise SpecError(f"unknown column: {name}", code="unknown_column", detail={"column": name})

    def restrict(self, allowed: Collection[str] | None) -> DatasetSchema:
        """The schema as seen by a caller limited to ``allowed`` columns (keys always stay)."""
        if allowed is None:
            return self
        keep = set(allowed) | set(self.key_columns)
        return self.model_copy(update={"columns": tuple(c for c in self.columns if c.name in keep)})


def build_schema(
    dataset: str,
    version: str,
    polars_schema: Mapping[str, pl.DataType],
    key_columns: tuple[str, ...],
    *,
    editable: Collection[str] | None = None,
    roles: Mapping[str, Role] | None = None,
) -> DatasetSchema:
    """Describe a dataset. Numeric columns default to measures, the rest to dimensions.

    ``editable`` limits which columns overrides and shocks may change (default: every non-key
    column the engine can compute with).
    """
    for key in key_columns:
        if key not in polars_schema:
            raise SpecError(f"key column {key!r} is not in the dataset", code="unknown_column")
    unknown = set(editable or ()) - set(polars_schema)
    unknown |= set(roles or {}) - set(polars_schema)
    if unknown:
        raise SpecError(f"unknown columns: {sorted(unknown)}", code="unknown_column")
    columns = []
    for name, dtype in polars_schema.items():
        ltype = from_polars(dtype)
        if name in key_columns:
            role: Role = "key"
        elif roles and name in roles:
            role = roles[name]
        else:
            role = "measure" if ltype.kind in NUMERIC else "dimension"
        can_edit = role != "key" and ltype.kind not in (Kind.OTHER, Kind.NULL)
        if editable is not None:
            can_edit = can_edit and name in editable
        columns.append(
            ColumnMeta(
                name=name,
                dtype=str(dtype),
                type=str(ltype),
                kind=ltype.kind,
                scale=ltype.scale,
                role=role,
                editable=can_edit,
            )
        )
    return DatasetSchema(
        dataset=dataset, version=version, key_columns=key_columns, columns=tuple(columns)
    )
