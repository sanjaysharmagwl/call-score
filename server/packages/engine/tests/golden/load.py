"""Loaders for the golden fixture (data/fixtures/golden).

Builds polars DataFrames that match each ``callscore_core.frames.FrameContract``
exactly (column names/order, dtypes, primary-key uniqueness, sort order) and
returns the committed expected outputs as python-native row dicts.
"""

import csv
import json
import re
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import polars as pl

from callscore_core.contracts import RunContext
from callscore_core.frames import FRAME_CONTRACTS, FrameContract
from callscore_core.methodology import Methodology

FIXTURE_DIR = Path(__file__).resolve().parents[5] / "data" / "fixtures" / "golden"
INPUT_DIR = FIXTURE_DIR / "inputs"
EXPECTED_DIR = FIXTURE_DIR / "expected"

PL_DTYPES: dict[str, Any] = {
    "String": pl.String,
    "Date": pl.Date,
    "Datetime[us, UTC]": pl.Datetime("us", "UTC"),
    "Float64": pl.Float64,
    "Int64": pl.Int64,
    "Boolean": pl.Boolean,
}

_FRAME_FILES = {
    "sessions": "sessions.csv",
    "rating_events": "rating_events.csv",
    "coverage_spells": "coverage_spells.csv",
    "employment_spells": "employment_spells.csv",
    "security_classifications": "security_classifications.csv",
    "security_daily": "security_daily.csv",
    "benchmark_daily": "benchmark_daily.csv",
    "security_beta": "security_beta.csv",
    "delistings": "delistings.csv",
}


def _parse(value: str, dtype: str) -> Any:
    if value == "":
        return None
    if dtype == "String":
        return value
    if dtype == "Date":
        return date.fromisoformat(value)
    if dtype == "Datetime[us, UTC]":
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return dt.astimezone(UTC)
    if dtype == "Float64":
        return float(value)
    if dtype == "Int64":
        return int(value)
    if dtype == "Boolean":
        return value == "true"
    raise ValueError(f"unknown dtype {dtype}")


def load_frame(name: str, path: Path | None = None) -> pl.DataFrame:
    """Load one fixture CSV into a DataFrame matching its FrameContract."""
    contract = FRAME_CONTRACTS[name]
    file = path or INPUT_DIR / _FRAME_FILES[name]
    with file.open(newline="") as f:
        reader = csv.DictReader(f)
        rows = [
            {col.name: _parse(raw[col.name], col.dtype) for col in contract.columns}
            for raw in reader
        ]
    schema = {col.name: PL_DTYPES[col.dtype] for col in contract.columns}
    return pl.DataFrame(rows, schema=schema)


def load_inputs() -> dict[str, pl.DataFrame]:
    return {name: load_frame(name) for name in _FRAME_FILES}


def load_benchmark_with_gap() -> pl.DataFrame:
    """benchmark_daily minus SECTOR_TECH on 2025-04-01 (optional failure input)."""
    return load_frame("benchmark_daily", INPUT_DIR / "benchmark_daily_missing_day.csv")


def load_methodologies() -> dict[str, Methodology]:
    raw = json.loads((FIXTURE_DIR / "methodologies.json").read_text())
    return {name: Methodology.model_validate(payload) for name, payload in raw.items()}


def load_context() -> RunContext:
    return RunContext(
        as_of_date=date(2025, 5, 23),
        knowledge_cutoff=datetime(2025, 5, 24, tzinfo=UTC),
        source_revision="golden",
        source_fingerprint="0" * 64,
        universe_fingerprint="0" * 64,
        algorithm_version="golden-v1",
    )


_DT_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _parse_expected(value: Any) -> Any:
    if isinstance(value, str):
        if _DT_RE.match(value):
            return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)
        if _DATE_RE.match(value):
            return date.fromisoformat(value)
        return value
    if isinstance(value, list):
        return [_parse_expected(v) for v in value]
    if isinstance(value, dict):
        return {k: _parse_expected(v) for k, v in value.items()}
    return value


def load_expected(kind: str, variant: str) -> list[dict[str, Any]]:
    """kind in {"intervals", "returns", "contributions"}; variant in default|decay|fixed."""
    rows = json.loads((EXPECTED_DIR / f"{kind}_{variant}.json").read_text())
    return [_parse_expected(row) for row in rows]


def expected_frame(kind: str, variant: str) -> tuple[pl.DataFrame, FrameContract]:
    contract = FRAME_CONTRACTS[kind]
    rows = load_expected(kind, variant)
    schema = {col.name: PL_DTYPES[col.dtype] for col in contract.columns}
    return pl.DataFrame(rows, schema=schema), contract


def check_contract_frame(frame: pl.DataFrame, contract: FrameContract) -> None:
    """Assert a frame matches its contract: columns, dtypes, keys, sort order."""
    assert frame.columns == list(contract.column_names), (
        f"{contract.name}: column mismatch {frame.columns}"
    )
    for col in contract.columns:
        assert frame[col.name].dtype == PL_DTYPES[col.dtype], (
            f"{contract.name}.{col.name}: dtype {frame[col.name].dtype}"
        )
        if not col.nullable:
            assert frame[col.name].null_count() == 0, f"{contract.name}.{col.name} has nulls"
    assert frame.select(contract.primary_key).is_duplicated().sum() == 0, (
        f"{contract.name}: duplicate primary key"
    )
    assert frame.equals(frame.sort(list(contract.sort_by))), (
        f"{contract.name}: not sorted by {contract.sort_by}"
    )
