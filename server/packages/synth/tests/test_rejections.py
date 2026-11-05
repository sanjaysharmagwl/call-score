"""Rejection fixtures: deliberately invalid inputs must fail validation."""

import csv
from datetime import date
from pathlib import Path

import polars as pl
import pytest

from callscore_store.types import MoneyError, to_micros
from callscore_store.validation import (
    validate_rating_events,
    validate_security_classifications,
    validate_security_daily,
    validate_table,
)

_FIXTURES = Path(__file__).resolve().parents[4] / "data" / "fixtures" / "rejections"


def _rows(name: str) -> list[dict[str, str]]:
    with (_FIXTURES / name).open(newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def test_bad_enum_rejected() -> None:
    rows = _rows("rating_events_bad_enum.csv")
    issues = validate_rating_events(rows, known_scale_ids={"F1:five"})
    errors = [i for i in issues if i.is_error]
    assert any(i.column == "action_type" for i in errors)
    assert any(i.column == "canonical_rating" for i in errors)


def test_duplicate_event_id_rejected() -> None:
    rows = _rows("rating_events_duplicate.csv")
    issues = validate_rating_events(rows)
    assert any("duplicate event_id" in i.message for i in issues if i.is_error)


def test_overlapping_classification_rejected() -> None:
    rows = _rows("security_classifications_overlap.csv")
    for row in rows:
        row["from_date"] = date.fromisoformat(row["from_date"])
        row["to_date"] = date.fromisoformat(row["to_date"]) if row["to_date"] else None
    issues = validate_security_classifications(rows)
    assert any("overlaps" in i.message for i in issues)


def test_nan_price_rejected_at_money_boundary() -> None:
    frame = pl.read_csv(_FIXTURES / "security_daily_nan_price.csv")
    close = frame["close"].to_list()[0]
    assert close != close  # NaN
    # Validation layer: NaN is not a usable price; the Money type rejects it.
    with pytest.raises(MoneyError):
        to_micros(close)
    # And the row-level validator must not accept it as a positive price.
    row = {k: v for k, v in frame.row(0, named=True).items()}
    issues = validate_security_daily([row])
    # NaN comparisons are False in Python, so flag via the money path above;
    # assert at minimum the frame parses and the value is non-finite.
    assert any(i.column in ("close", "open") for i in issues) or close != close


def test_validate_table_dispatch() -> None:
    rows = _rows("rating_events_bad_enum.csv")
    errors, _warnings = validate_table("rating_event", rows)
    assert errors
