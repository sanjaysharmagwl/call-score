"""Event windows around each interval's entry session (decision 0003).

One row per interval: the weight change versus the analyst's previous interval
on the same security, the deviation from other analysts' visible consensus,
the announcement-window CAR and the compounded 20-session pre/post excess
returns. Pure frame functions; a missing benchmark level where a window is
otherwise complete raises ``MissingBenchmarkData`` rather than assuming zero.

The security's own ``security_daily`` session dates are the trading calendar
for every window (CAR, pre and post), matching "on the security's calendar".
"""

from datetime import date

import polars as pl

from callscore_core.contracts import RunContext
from callscore_core.frames import EVENT_WINDOW_FRAME
from callscore_core.methodology import Methodology
from callscore_engine.pricing import (
    MissingBenchmarkData,
    benchmark_assignment,
    benchmark_levels,
    execution_levels,
)

_PRE_LAG = 21  # closes at entry-21 and entry-1 compound the 20 pre-event sessions
_POST_AHEAD = 20
_POINT_OFFSETS = (-_PRE_LAG, -1, 0, 1, _POST_AHEAD)
_CAR_OFFSETS = (-1, 0, 1)


def _first_segment_weights(exposures: pl.DataFrame) -> pl.DataFrame:
    """``interval_id, first_weight`` from the ``segment_no = 1`` exposure row."""
    return (
        exposures.filter(pl.col("segment_no") == 1)
        .select("interval_id", pl.col("position_weight").alias("first_weight"))
        .unique("interval_id", keep="first", maintain_order=True)
    )


def _with_weight_change(intervals: pl.DataFrame, exposures: pl.DataFrame) -> pl.DataFrame:
    """Attach ``first_weight``, the previous interval's weight and ``weight_change``.

    Intervals are ordered per (analyst, security) by entry timestamp; the
    previous interval is the preceding row. No previous interval means the
    previous weight is 0.0; a null weight on either side yields null.
    """
    iv = (
        intervals.join(_first_segment_weights(exposures), on="interval_id", how="left")
        .sort(
            "analyst_id",
            "security_id",
            "entry_ts",
            "entry_session",
            "entry_point",
            "interval_id",
        )
        .with_columns(
            pl.int_range(pl.len()).over("analyst_id", "security_id").alias("_ord"),
            pl.col("first_weight").shift(1).over("analyst_id", "security_id").alias("_prev_weight"),
        )
    )
    previous = pl.when(pl.col("_ord") == 0).then(pl.lit(0.0)).otherwise(pl.col("_prev_weight"))
    return iv.with_columns(
        pl.when(pl.col("first_weight").is_null())
        .then(pl.lit(None, dtype=pl.Float64))
        .otherwise(pl.col("first_weight") - previous)
        .alias("weight_change")
    )


def _with_consensus(iv: pl.DataFrame, methodology: Methodology) -> pl.DataFrame:
    """``consensus_deviation = w_own - mean(w_others)`` over investable peers.

    Peers are other analysts' investable intervals on the same security active
    at this interval's ``available_ts`` (``entry_ts <= available_ts <
    exit_ts``). Null when fewer than ``min_consensus_ratings`` peers or when
    the interval's own weight is null.
    """
    peers = iv.select(
        "interval_id",
        "analyst_id",
        "security_id",
        "entry_ts",
        "exit_ts",
        "available_ts",
        "is_investable",
        "first_weight",
    )
    own = iv.select("interval_id", "analyst_id", "security_id", "available_ts", "first_weight")
    active = own.join(peers, on="security_id", suffix="_peer").filter(
        (pl.col("analyst_id") != pl.col("analyst_id_peer"))
        & pl.col("is_investable")
        & (pl.col("entry_ts") <= pl.col("available_ts"))
        & (pl.col("exit_ts") > pl.col("available_ts"))
        & pl.col("first_weight_peer").is_not_null()
    )
    stats = active.group_by("interval_id").agg(
        pl.len().alias("_n_peers"),
        pl.col("first_weight_peer").mean().alias("_peer_mean"),
    )
    return iv.join(stats, on="interval_id", how="left").with_columns(
        pl.when(
            pl.col("first_weight").is_not_null()
            & (pl.col("_n_peers").fill_null(0) >= methodology.min_consensus_ratings)
        )
        .then(pl.col("first_weight") - pl.col("_peer_mean"))
        .otherwise(pl.lit(None, dtype=pl.Float64))
        .alias("consensus_deviation")
    )


def _security_levels(security_daily: pl.DataFrame, as_of: date) -> pl.DataFrame:
    """TR/px levels per security session with a 0-based ``_idx`` calendar position."""
    levels = execution_levels(security_daily.filter(pl.col("session_date") <= as_of))
    levels = levels.sort("security_id", "session_date")
    return levels.with_columns(pl.int_range(pl.len()).over("security_id").alias("_idx"))


def _calendar_points(iv: pl.DataFrame, levels: pl.DataFrame) -> pl.DataFrame:
    """``_d<off>``, ``_c<off>``, ``_o<off>`` columns at ``_idx + off`` per interval."""
    idx = levels.select("security_id", "session_date", "_idx").rename(
        {"session_date": "entry_session"}
    )
    out = iv.join(idx, on=["security_id", "entry_session"], how="left")
    parts = []
    for off in _POINT_OFFSETS:
        part = (
            out.select("interval_id", "security_id", "_idx")
            .with_columns((pl.col("_idx") + off).alias("_nidx"))
            .filter(pl.col("_nidx") >= 0)
            .join(
                levels.select(
                    "security_id",
                    pl.col("_idx").alias("_nidx"),
                    pl.col("session_date").alias(f"_d{off}"),
                    pl.col("close_tr").alias(f"_c{off}"),
                    pl.col("open_tr").alias(f"_o{off}"),
                ),
                on=["security_id", "_nidx"],
                how="left",
            )
            .select("interval_id", f"_d{off}", f"_c{off}", f"_o{off}")
        )
        parts.append(part)
    wide = parts[0]
    for part in parts[1:]:
        wide = wide.join(part, on="interval_id", how="left")
    return out.join(wide, on="interval_id", how="left")


def _benchmark_frame(benchmark_daily: pl.DataFrame, as_of: date) -> pl.DataFrame:
    """Benchmark levels plus the close-to-close daily return per benchmark."""
    levels = benchmark_levels(benchmark_daily.filter(pl.col("session_date") <= as_of)).sort(
        "benchmark_id", "session_date"
    )
    return levels.with_columns(
        (pl.col("close_tr") / pl.col("close_tr").shift(1).over("benchmark_id") - 1).alias(
            "bench_ret"
        )
    )


def _join_security_returns(iv: pl.DataFrame, levels: pl.DataFrame) -> pl.DataFrame:
    """Attach ``_sr<off>``: the security's close-to-close TR return at ``_d<off>``."""
    stock = levels.with_columns(
        (pl.col("close_tr") / pl.col("close_tr").shift(1).over("security_id") - 1).alias(
            "_stock_ret"
        )
    ).select("security_id", "session_date", "_stock_ret")
    for off in _CAR_OFFSETS:
        iv = iv.join(
            stock.select(
                "security_id",
                pl.col("session_date").alias(f"_k{off}"),
                "_stock_ret",
            ),
            left_on=["security_id", f"_d{off}"],
            right_on=["security_id", f"_k{off}"],
            how="left",
        ).rename({"_stock_ret": f"_sr{off}"})
    return iv


def _join_benchmark_values(
    iv: pl.DataFrame, bench: pl.DataFrame, offsets: tuple[int, ...]
) -> pl.DataFrame:
    """Attach benchmark ``bench_ret`` (CAR offsets) and levels at ``_d<off>``."""
    for off in offsets:
        value_cols = [pl.col("bench_ret").alias(f"_br{off}")] if off in _CAR_OFFSETS else []
        value_cols += [
            pl.col("close_tr").alias(f"_bc{off}"),
            pl.col("open_tr").alias(f"_bo{off}"),
        ]
        iv = iv.join(
            bench.select(["benchmark_id", pl.col("session_date").alias(f"_bk{off}")] + value_cols),
            left_on=["benchmark_id", f"_d{off}"],
            right_on=["benchmark_id", f"_bk{off}"],
            how="left",
        )
    return iv


def _attach_windows(
    iv: pl.DataFrame,
    levels: pl.DataFrame,
    bench: pl.DataFrame,
    methodology: Methodology,
) -> pl.DataFrame:
    """Compute ``car_pm1``, ``pre_event_excess_20d`` and ``post_event_excess_20d``."""
    iv = _join_security_returns(iv, levels)
    iv = _join_benchmark_values(iv, bench, _POINT_OFFSETS)

    # CAR: sum of daily abnormal returns over the sessions around the entry.
    car_stock = (
        pl.col("_sr-1")
        - pl.col("_br-1")
        + pl.col("_sr0")
        - pl.col("_br0")
        + pl.col("_sr1")
        - pl.col("_br1")
    )
    car_stock_complete = pl.all_horizontal(pl.col(f"_sr{o}").is_not_null() for o in _CAR_OFFSETS)
    car_bench_missing = pl.any_horizontal(pl.col(f"_br{o}").is_null() for o in _CAR_OFFSETS)

    # Pre-event: compounded TR over the 20 sessions before the entry session.
    pre_stock_ok = pl.col("_c-21").is_not_null() & pl.col("_c-1").is_not_null()
    pre_stock = pl.col("_c-1") / pl.col("_c-21") - 1
    pre_bench_ok = pl.col("_bc-21").is_not_null() & pl.col("_bc-1").is_not_null()
    pre_bench = pl.col("_bc-1") / pl.col("_bc-21") - 1

    # Post-event: from the interval's entry point to the close 20 sessions on.
    entry_level = (
        pl.when(pl.col("entry_point") == "open")
        .then(pl.col("_o0"))
        .when(pl.col("entry_point") == "close")
        .then(pl.col("_c0"))
        .otherwise(None)
    )
    post_stock_ok = entry_level.is_not_null() & pl.col("_c20").is_not_null()
    post_stock = pl.col("_c20") / entry_level - 1
    bench_entry = (
        pl.when(pl.col("entry_point") == "open")
        .then(pl.col("_bo0"))
        .when(pl.col("entry_point") == "close")
        .then(pl.col("_bc0"))
        .otherwise(None)
    )
    post_bench_ok = bench_entry.is_not_null() & pl.col("_bc20").is_not_null()
    post_bench = pl.col("_bc20") / bench_entry - 1

    missing = iv.filter(
        (car_stock_complete & car_bench_missing)
        | (pre_stock_ok & ~pre_bench_ok)
        | (post_stock_ok & ~post_bench_ok)
    )
    if missing.height:
        ids = ", ".join(missing["interval_id"].head(5).to_list())
        raise MissingBenchmarkData(
            f"Benchmark levels missing for event windows of intervals: {ids}"
        )

    car = (
        pl.when(~pl.lit(methodology.include_announcement_window))
        .then(pl.lit(None, dtype=pl.Float64))
        .when(
            car_stock_complete
            & pl.all_horizontal(pl.col(f"_br{o}").is_not_null() for o in _CAR_OFFSETS)
        )
        .then(car_stock)
        .otherwise(pl.lit(None, dtype=pl.Float64))
        .alias("car_pm1")
    )
    pre = (
        pl.when(pre_stock_ok & pre_bench_ok)
        .then(pre_stock - pre_bench)
        .otherwise(pl.lit(None, dtype=pl.Float64))
        .alias("pre_event_excess_20d")
    )
    post = (
        pl.when(post_stock_ok & post_bench_ok)
        .then(post_stock - post_bench)
        .otherwise(pl.lit(None, dtype=pl.Float64))
        .alias("post_event_excess_20d")
    )
    return iv.with_columns(car, pre, post)


def _unavailable_reason(methodology: Methodology) -> pl.Expr:
    """Semicolon-joined reasons for the null window/weight fields."""
    parts = [
        pl.when(~pl.lit(methodology.include_announcement_window))
        .then(pl.lit("announcement_window_disabled"))
        .when(pl.col("car_pm1").is_null())
        .then(pl.lit("event_window_incomplete"))
        .otherwise(None),
        pl.when(pl.col("pre_event_excess_20d").is_null())
        .then(pl.lit("pre_event_window_incomplete"))
        .otherwise(None),
        pl.when(pl.col("post_event_excess_20d").is_null())
        .then(pl.lit("post_event_window_incomplete"))
        .otherwise(None),
        pl.when(pl.col("weight_change").is_null())
        .then(pl.lit("weight_change_unavailable"))
        .otherwise(None),
        pl.when(pl.col("first_weight").is_not_null() & pl.col("consensus_deviation").is_null())
        .then(pl.lit("insufficient_consensus"))
        .otherwise(None),
    ]
    reason = pl.concat_str([p.cast(pl.String) for p in parts], separator=";", ignore_nulls=True)
    return pl.when(reason == "").then(None).otherwise(reason).alias("unavailable_reason")


def build_event_windows(
    intervals: pl.DataFrame,
    exposures: pl.DataFrame,
    security_daily: pl.DataFrame,
    benchmark_daily: pl.DataFrame,
    classifications: pl.DataFrame,
    methodology: Methodology,
    context: RunContext,
) -> pl.DataFrame:
    """Build ``EVENT_WINDOW_FRAME``: one row per interval, sorted by ``event_id``."""
    as_of = context.as_of_date
    iv = _with_weight_change(intervals, exposures)
    iv = _with_consensus(iv, methodology)
    assignment = benchmark_assignment(intervals, classifications, methodology).select(
        "interval_id", "benchmark_id"
    )
    iv = iv.join(assignment, on="interval_id", how="left")

    levels = _security_levels(security_daily, as_of)
    iv = _calendar_points(iv, levels)
    bench = _benchmark_frame(benchmark_daily, as_of)
    iv = _attach_windows(iv, levels, bench, methodology)
    iv = iv.with_columns(_unavailable_reason(methodology))

    return iv.select(
        "event_id",
        "analyst_id",
        "security_id",
        pl.col("entry_session").alias("event_session"),
        "weight_change",
        "consensus_deviation",
        "car_pm1",
        "pre_event_excess_20d",
        "post_event_excess_20d",
        "unavailable_reason",
    ).sort("event_id")


def assert_event_windows_contract(frame: pl.DataFrame) -> None:
    """Private conformance check used by tests: columns, dtypes, sort, key."""
    contract = EVENT_WINDOW_FRAME
    assert frame.columns == list(contract.column_names)
    dtypes = {
        "String": pl.String(),
        "Date": pl.Date(),
        "Datetime[us, UTC]": pl.Datetime("us", "UTC"),
        "Float64": pl.Float64(),
        "Int64": pl.Int64(),
        "Boolean": pl.Boolean(),
    }
    for column in contract.columns:
        assert frame.schema[column.name] == dtypes[column.dtype], column.name
    assert frame["event_id"].n_unique() == frame.height
    assert frame.select(contract.sort_by).equals(
        frame.select(contract.sort_by).sort(contract.sort_by)
    )
