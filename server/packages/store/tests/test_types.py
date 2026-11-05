"""Tests for Money and UTCDateTime column types."""

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlalchemy import Column, MetaData, Table, select, text
from sqlalchemy.exc import StatementError

from callscore_store.db import create_engine_for
from callscore_store.types import Money, MoneyError, UTCDateTime, to_micros


def _roundtrip(value: object, col_type: object) -> object:
    """Bind ``value`` through a single-column table and read it back."""
    engine = create_engine_for(":memory:")
    metadata = MetaData()
    table = Table(
        "t",
        metadata,
        Column("v", col_type, primary_key=True),  # type: ignore[arg-type]
    )
    metadata.create_all(engine)
    with engine.begin() as conn:
        conn.execute(table.insert().values(v=value))
        return conn.execute(select(table.c.v)).scalar_one()


class TestMoney:
    def test_decimal_roundtrip_exact(self) -> None:
        assert _roundtrip(Decimal("123.456789"), Money()) == Decimal("123.456789")

    def test_int_and_float(self) -> None:
        assert _roundtrip(5, Money()) == Decimal("5.000000")
        # Floats convert via repr and round half-to-even to 6 places.
        assert _roundtrip(1.5, Money()) == Decimal("1.500000")

    def test_rejects_nan_and_infinity(self) -> None:
        for bad in (Decimal("NaN"), Decimal("Infinity"), float("nan"), float("inf")):
            with pytest.raises(MoneyError):
                to_micros(bad)  # type: ignore[arg-type]

    def test_rejects_too_many_decimals(self) -> None:
        with pytest.raises(MoneyError, match="decimal places"):
            to_micros(Decimal("1.1234567"))
        with pytest.raises(MoneyError):
            to_micros(Decimal("0.0000001"))

    def test_rejects_bool_and_out_of_range(self) -> None:
        with pytest.raises(MoneyError):
            to_micros(True)  # type: ignore[arg-type]
        with pytest.raises(MoneyError):
            to_micros(Decimal("1e15"))

    def test_stored_as_scaled_integer(self) -> None:
        engine = create_engine_for(":memory:")
        metadata = MetaData()
        table = Table("t", metadata, Column("v", Money, primary_key=True))
        metadata.create_all(engine)
        with engine.begin() as conn:
            conn.execute(table.insert().values(v=Decimal("2.5")))
            raw = conn.execute(text("SELECT v FROM t")).scalar_one()
        assert raw == 2_500_000
        assert isinstance(raw, int)


class TestUTCDateTime:
    def test_aware_roundtrip(self) -> None:
        ts = datetime(2024, 3, 1, 14, 30, 15, 123456, tzinfo=UTC)
        assert _roundtrip(ts, UTCDateTime()) == ts

    def test_non_utc_normalized(self) -> None:
        from datetime import timedelta, timezone

        ts = datetime(2024, 3, 1, 9, 30, tzinfo=timezone(timedelta(hours=-5)))
        assert _roundtrip(ts, UTCDateTime()) == datetime(2024, 3, 1, 14, 30, tzinfo=UTC)

    def test_naive_rejected_on_write(self) -> None:
        engine = create_engine_for(":memory:")
        metadata = MetaData()
        table = Table("t", metadata, Column("v", UTCDateTime, primary_key=True))
        metadata.create_all(engine)
        with (
            pytest.raises((ValueError, StatementError)),
            engine.begin() as conn,
        ):
            conn.execute(table.insert().values(v=datetime(2024, 1, 1, 12, 0)))
