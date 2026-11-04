import datetime as dt
from decimal import Decimal

import polars as pl
import pytest
from hypothesis import HealthCheck, settings

from callscore_calc import CalcEngine, Catalog

settings.register_profile(
    "default", deadline=None, suppress_health_check=[HealthCheck.too_slow], max_examples=150
)
settings.register_profile("ci", deadline=None, suppress_health_check=[HealthCheck.too_slow])
settings.load_profile("default")


def positions_frame() -> pl.DataFrame:
    """A small book of positions with every kind of column, nulls included."""
    return pl.DataFrame(
        {
            "id": [1, 2, 3, 4, 5, 6],
            "desk": ["rates", "rates", "credit", "credit", "equity", None],
            "sector": ["Tech", "Fin", "Tech", "Energy", "Tech", "Fin"],
            "price": [
                Decimal("101.25"),
                Decimal("99.50"),
                Decimal("50.00"),
                Decimal("75.10"),
                None,
                Decimal("10.00"),
            ],
            "qty": [10, 20, 30, 40, 50, 60],
            "yield": [0.05, 0.04, None, 0.03, 0.02, 0.01],
            "trade_date": [
                dt.date(2026, 1, 5),
                dt.date(2026, 2, 5),
                dt.date(2026, 3, 5),
                None,
                dt.date(2026, 5, 5),
                dt.date(2026, 6, 5),
            ],
        },
        schema_overrides={"price": pl.Decimal(18, 2)},
    )


@pytest.fixture
def frame() -> pl.DataFrame:
    return positions_frame()


@pytest.fixture
def catalog(frame: pl.DataFrame) -> Catalog:
    cat = Catalog()
    cat.register_frame("pos", frame, key_columns=["id"], version="v1")
    return cat


@pytest.fixture
def engine(catalog: Catalog) -> CalcEngine:
    return CalcEngine(catalog)
