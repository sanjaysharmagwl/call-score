"""Execution-point price levels and benchmark selection (decision 0003).

Shared by the return, event-window, target and portfolio stages so every stage values a
position identically. Pure functions over frames that follow ``callscore_core.frames``.
"""

import polars as pl

from callscore_core.enums import BenchmarkMode
from callscore_core.frames import POINT_CLOSE, POINT_OPEN, sector_benchmark_id
from callscore_core.methodology import Methodology


class MissingBenchmarkData(ValueError):
    """A required benchmark level is absent; the run must fail rather than assume zero."""


LEVEL_COLUMNS = ("open_tr", "close_tr", "open_px", "close_px", "high_px", "low_px")


def execution_levels(security_daily: pl.DataFrame) -> pl.DataFrame:
    """Per security and session: total-return and split-continuous price levels.

    ``close_tr`` is ``tr_index``; ``open_tr = tr_index * open / close`` follows from the
    convention that ``tr_index(t) = open_tr(t) * close(t) / open(t)``. Price levels are
    ``raw * adj_factor``.
    """
    if security_daily.filter(pl.col("close") <= 0).height:
        raise ValueError("security_daily closes must be positive")
    return security_daily.sort("security_id", "session_date").select(
        "security_id",
        "session_date",
        (pl.col("tr_index") * pl.col("open") / pl.col("close")).alias("open_tr"),
        pl.col("tr_index").alias("close_tr"),
        (pl.col("open") * pl.col("adj_factor")).alias("open_px"),
        (pl.col("close") * pl.col("adj_factor")).alias("close_px"),
        (pl.col("high") * pl.col("adj_factor")).alias("high_px"),
        (pl.col("low") * pl.col("adj_factor")).alias("low_px"),
        "is_halted",
    )


def point_level(
    frame: pl.DataFrame,
    levels: pl.DataFrame,
    *,
    id_col: str,
    session_col: str,
    point_col: str,
    out: str,
    basis: str = "tr",
) -> pl.DataFrame:
    """Attach the level at ``(id_col, session_col, point_col)`` as column ``out``.

    ``levels`` has ``id_col``, ``session_date`` and ``open_<basis>``/``close_<basis>``
    columns. Missing levels come back null; callers decide whether that is fatal.
    """
    open_col, close_col = f"open_{basis}", f"close_{basis}"
    lookup = levels.select(
        pl.col(id_col),
        pl.col("session_date").alias(session_col),
        pl.col(open_col).alias("__open"),
        pl.col(close_col).alias("__close"),
    )
    return (
        frame.join(lookup, on=[id_col, session_col], how="left")
        .with_columns(
            pl.when(pl.col(point_col) == POINT_OPEN)
            .then(pl.col("__open"))
            .when(pl.col(point_col) == POINT_CLOSE)
            .then(pl.col("__close"))
            .otherwise(None)
            .alias(out)
        )
        .drop("__open", "__close")
    )


def benchmark_levels(benchmark_daily: pl.DataFrame) -> pl.DataFrame:
    """Benchmark levels renamed to the ``open_tr``/``close_tr`` shape ``point_level`` uses."""
    return benchmark_daily.select(
        "benchmark_id",
        "session_date",
        pl.col("open_level").alias("open_tr"),
        pl.col("close_level").alias("close_tr"),
    )


def benchmark_assignment(
    intervals: pl.DataFrame,
    classifications: pl.DataFrame,
    methodology: Methodology,
) -> pl.DataFrame:
    """``interval_id, benchmark_id, beta_adjusted`` for each interval.

    ``sector`` mode uses the security's sector on its entry session (spell ``to_date`` is
    inclusive); ``market``/``custom`` use ``methodology.benchmark_id``; ``beta_adjusted``
    uses ``methodology.benchmark_id`` scaled by beta. Raises ``MissingBenchmarkData`` when a
    sector cannot be resolved for an interval.
    """
    mode = methodology.benchmark_mode
    base = intervals.select("interval_id", "security_id", "entry_session")
    if mode != BenchmarkMode.SECTOR:
        assert methodology.benchmark_id is not None
        return base.select(
            "interval_id",
            pl.lit(methodology.benchmark_id).alias("benchmark_id"),
            pl.lit(mode == BenchmarkMode.BETA_ADJUSTED).alias("beta_adjusted"),
        )
    matched = (
        base.join(
            classifications.select("security_id", "from_date", "to_date", "sector_id"),
            on="security_id",
            how="left",
        )
        .filter(
            (pl.col("from_date") <= pl.col("entry_session"))
            & (pl.col("to_date").is_null() | (pl.col("to_date") >= pl.col("entry_session")))
        )
        .unique(subset="interval_id", keep="first", maintain_order=True)
    )
    missing = base.join(matched, on="interval_id", how="anti")
    if missing.height:
        ids = ", ".join(missing["interval_id"].head(5).to_list())
        raise MissingBenchmarkData(f"No sector classification at entry for intervals: {ids}")
    return (
        base.select("interval_id")
        .join(
            matched.select(
                "interval_id",
                pl.col("sector_id")
                .map_elements(sector_benchmark_id, return_dtype=pl.String)
                .alias("benchmark_id"),
            ),
            on="interval_id",
            how="left",
        )
        .with_columns(pl.lit(False).alias("beta_adjusted"))
    )
