"""Results: the frame plus metadata that makes it reproducible and auditable."""

from __future__ import annotations

import datetime as dt
import io
from decimal import Decimal
from typing import Any, Literal

import polars as pl
from pydantic import BaseModel, ConfigDict

from callscore_calc.dtypes import Kind, LType
from callscore_calc.spec.query import DatasetRef

_JS_SAFE_INT = 2**53


class ColumnInfo(BaseModel):
    model_config = ConfigDict(frozen=True)

    name: str
    type: str
    kind: Kind
    scale: int | None = None

    @classmethod
    def of(cls, name: str, ltype: LType) -> ColumnInfo:
        return cls(name=name, type=str(ltype), kind=ltype.kind, scale=ltype.scale)


class ResultMeta(BaseModel):
    """Everything needed to reproduce, audit or cache a result.

    ``fingerprint`` is the SHA-256 of the canonical, fully resolved request (dataset version,
    effective transform steps, query, result-affecting options and numeric settings). The same
    fingerprint on the same library versions means the same answer. ``extensions`` holds what
    each plugin transform reports, e.g. ``extensions["whatif"]["unmatched_edits"]``.
    """

    model_config = ConfigDict(frozen=True)

    fingerprint: str
    dataset: DatasetRef
    total_rows: int
    offset: int = 0
    rows: int
    columns: list[ColumnInfo]
    pivot_fields: list[str] | None = None
    engine: str
    cached: bool = False
    timings_ms: dict[str, float]
    extensions: dict[str, Any] = {}
    stage_rows: dict[str, int] | None = None
    versions: dict[str, str]


class CalcResult:
    def __init__(self, frame: pl.DataFrame, meta: ResultMeta) -> None:
        self.frame = frame
        self.meta = meta

    def __repr__(self) -> str:
        meta = self.meta
        return (
            f"CalcResult(rows={meta.rows}, total_rows={meta.total_rows}, "
            f"fingerprint={meta.fingerprint[:12]})"
        )

    def to_records(self, *, decimals: Literal["float", "str"] = "float") -> list[dict[str, Any]]:
        """JSON-safe rows: decimals as floats (or exact strings), dates as ISO strings, and
        integers beyond 2**53 as strings so JavaScript clients don't lose digits."""
        rows = self.frame.to_dicts()
        return [{k: json_safe(v, decimals) for k, v in row.items()} for row in rows]

    def to_arrow_ipc(self) -> bytes:
        """The frame as an Arrow IPC stream (exact decimals, no JSON overhead)."""
        buffer = io.BytesIO()
        self.frame.write_ipc_stream(buffer)
        return buffer.getvalue()

    def to_dict(self, *, decimals: Literal["float", "str"] = "float") -> dict[str, Any]:
        return {
            "rows": self.to_records(decimals=decimals),
            "meta": self.meta.model_dump(mode="json"),
        }


def json_safe(value: Any, decimals: Literal["float", "str"] = "float") -> Any:
    if isinstance(value, Decimal):
        return float(value) if decimals == "float" else format(value, "f")
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, int):
        return str(value) if abs(value) >= _JS_SAFE_INT else value
    if isinstance(value, dt.date | dt.datetime):
        return value.isoformat()
    return value
