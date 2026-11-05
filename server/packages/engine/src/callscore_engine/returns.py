"""Return legs at execution points for every interval and horizon (decision 0003).

``compute_legs`` is the shared leg pricer: the returns stage feeds it horizon legs and
the contributions stage feeds it exposure-segment legs, so a single segment prices
identically to the ``to_exit`` leg by construction.
"""

from bisect import bisect_left
from datetime import date

import polars as pl

from callscore_core.contracts import RunContext
from callscore_core.enums import ReturnBasis
from callscore_core.frames import HORIZON_TO_EXIT, POINT_CLOSE, POINT_OPEN, horizon_label
from callscore_core.methodology import Methodology
from callscore_engine.pricing import (
    MissingBenchmarkData,
    benchmark_assignment,
    benchmark_levels,
    execution_levels,
    point_level,
)

_SCHEMA: dict[str, pl.DataType] = {
    "interval_id": pl.String(),
    "horizon": pl.String(),
    "start_session": pl.Date(),
    "start_point": pl.String(),
    "end_session": pl.Date(),
    "end_point": pl.String(),
    "entry_price": pl.Float64(),
    "exit_price": pl.Float64(),
    "stock_return": pl.Float64(),
    "benchmark_id": pl.String(),
    "benchmark_return": pl.Float64(),
    "beta": pl.Float64(),
    "excess_return": pl.Float64(),
    "terminated_by_delisting": pl.Boolean(),
    "unavailable_reason": pl.String(),
}

_POINT_RANK = {POINT_OPEN: 0, POINT_CLOSE: 1}

_LEG_INPUT_COLUMNS = (
    "leg_id",
    "interval_id",
    "security_id",
    "entry_session",
    "start_session",
    "start_point",
    "end_session",
    "end_point",
)

# Explicit schema for the leg-row dicts: ``unavailable_reason`` (and the
# nullable end columns) are ``None`` on most rows, so inference would type
# them ``Null`` and fail when the first string value appears.
_LEG_ROW_SCHEMA: dict[str, pl.DataType] = {
    "leg_id": pl.String(),
    "interval_id": pl.String(),
    "horizon": pl.String(),
    "security_id": pl.String(),
    "entry_session": pl.Date(),
    "start_session": pl.Date(),
    "start_point": pl.String(),
    "end_session": pl.Date(),
    "end_point": pl.String(),
    "unavailable_reason": pl.String(),
}


def _point_after(session_a: date, point_a: str, session_b: date, point_b: str) -> bool:
    if session_a != session_b:
        return session_a > session_b
    return _POINT_RANK[point_a] > _POINT_RANK[point_b]


def _session_calendars(
    classifications: pl.DataFrame, sessions: pl.DataFrame, as_of_date: date
) -> tuple[dict[str, list[date]], list[date]]:
    """Per-exchange session dates (``<= as_of_date``) plus the union fallback."""
    usable = sessions.filter(pl.col("session_date") <= as_of_date)
    by_exchange = {
        str(row["exchange_id"]): list(row["dates"])
        for row in usable.group_by("exchange_id", maintain_order=True)
        .agg(pl.col("session_date").sort().alias("dates"))
        .to_dicts()
    }
    all_dates = sorted(usable.select("session_date").unique().to_series().to_list())
    return by_exchange, all_dates


def _exchange_at_entry(
    intervals: pl.DataFrame, classifications: pl.DataFrame
) -> dict[str, str | None]:
    """``interval_id -> exchange_id`` from the classification spell covering entry."""
    matched = (
        intervals.select("interval_id", "security_id", "entry_session")
        .join(
            classifications.select("security_id", "from_date", "to_date", "exchange_id"),
            on="security_id",
            how="left",
        )
        .filter(
            (pl.col("from_date") <= pl.col("entry_session"))
            & (pl.col("to_date").is_null() | (pl.col("to_date") >= pl.col("entry_session")))
        )
        .unique(subset="interval_id", keep="first", maintain_order=True)
    )
    return {
        str(row["interval_id"]): (None if row["exchange_id"] is None else str(row["exchange_id"]))
        for row in matched.to_dicts()
    }


def _horizon_end(entry_session: date, days: int, dates: list[date]) -> date | None:
    """Close session ``days`` sessions after ``entry_session`` on ``dates``, or None."""
    idx = bisect_left(dates, entry_session)
    if idx >= len(dates) or dates[idx] != entry_session:
        return None
    target = idx + days
    if target >= len(dates):
        return None
    return dates[target]


def _attach_benchmarks_and_beta(
    legs: pl.DataFrame,
    classifications: pl.DataFrame,
    beta: pl.DataFrame,
    methodology: Methodology,
) -> pl.DataFrame:
    """Add ``benchmark_id``, ``beta_adjusted`` and per-leg ``beta`` (beta legs only)."""
    unique = legs.select("interval_id", "security_id", "entry_session").unique(subset="interval_id")
    assignment = benchmark_assignment(unique, classifications, methodology).select(
        "interval_id", "benchmark_id", "beta_adjusted"
    )
    out = legs.join(assignment, on="interval_id", how="left")

    beta_values = (
        legs.select("leg_id", "security_id", "entry_session")
        .join(
            beta.select("security_id", "as_of_date", "beta"),
            on="security_id",
            how="inner",
        )
        .filter(pl.col("as_of_date") < pl.col("entry_session"))
        .sort("as_of_date")
        .group_by("leg_id", maintain_order=True)
        .last()
        .select("leg_id", pl.col("beta").alias("beta_value"))
    )
    out = out.join(beta_values, on="leg_id", how="left")
    return out.with_columns(
        pl.when(pl.col("beta_adjusted")).then(pl.col("beta_value")).otherwise(None).alias("beta")
    ).drop("beta_value")


def _terminate_at_delisting(legs: pl.DataFrame, delistings: pl.DataFrame) -> pl.DataFrame:
    """Truncate legs ending at/after a delisting to that session's terminal close."""
    if delistings.is_empty():
        return legs.with_columns(
            pl.lit(None, dtype=pl.Date).alias("del_date"),
            pl.lit(None, dtype=pl.Float64).alias("final_return"),
            pl.lit(False).alias("terminated_by_delisting"),
        )
    delisted = delistings.select(
        "security_id", pl.col("delisting_date").alias("del_date"), "final_return"
    )
    out = legs.join(delisted, on="security_id", how="left")
    terminated = (
        pl.col("del_date").is_not_null()
        & pl.col("end_session").is_not_null()
        & (pl.col("del_date") >= pl.col("start_session"))
        & (
            (pl.col("end_session") > pl.col("del_date"))
            | ((pl.col("end_session") == pl.col("del_date")) & (pl.col("end_point") == POINT_CLOSE))
        )
    ).fill_null(False)
    return out.with_columns(
        pl.when(terminated)
        .then(pl.col("del_date"))
        .otherwise(pl.col("end_session"))
        .alias("end_session"),
        pl.when(terminated)
        .then(pl.lit(POINT_CLOSE))
        .otherwise(pl.col("end_point"))
        .alias("end_point"),
        terminated.alias("terminated_by_delisting"),
    )


def compute_legs(
    legs: pl.DataFrame,
    *,
    security_daily: pl.DataFrame,
    benchmark_daily: pl.DataFrame,
    beta: pl.DataFrame,
    delistings: pl.DataFrame,
    classifications: pl.DataFrame,
    methodology: Methodology,
) -> pl.DataFrame:
    """Price every leg and attach benchmark/excess returns.

    ``legs`` must carry ``_LEG_INPUT_COLUMNS`` plus any pass-through columns; rows with a
    null ``end_session`` keep null prices and returns. Raises ``MissingBenchmarkData``
    when a priced leg lacks a benchmark level or a required beta.
    """
    enriched = _attach_benchmarks_and_beta(legs, classifications, beta, methodology)
    enriched = _terminate_at_delisting(enriched, delistings)

    priced = enriched.filter(pl.col("end_session").is_not_null())
    priced_out: pl.DataFrame | None = None
    if priced.height:
        sec_levels = execution_levels(security_daily)
        bench_levels = benchmark_levels(benchmark_daily)
        endpoints = pl.concat(
            [
                priced.select(
                    "leg_id",
                    "security_id",
                    "benchmark_id",
                    pl.col("start_session").alias("session_date"),
                    pl.col("start_point").alias("point"),
                ).with_columns(pl.lit("start").alias("which")),
                priced.select(
                    "leg_id",
                    "security_id",
                    "benchmark_id",
                    pl.col("end_session").alias("session_date"),
                    pl.col("end_point").alias("point"),
                ).with_columns(pl.lit("end").alias("which")),
            ]
        )
        endpoints = point_level(
            endpoints,
            sec_levels,
            id_col="security_id",
            session_col="session_date",
            point_col="point",
            out="lvl_tr",
            basis="tr",
        )
        endpoints = point_level(
            endpoints,
            sec_levels,
            id_col="security_id",
            session_col="session_date",
            point_col="point",
            out="lvl_px",
            basis="px",
        )
        endpoints = point_level(
            endpoints,
            bench_levels,
            id_col="benchmark_id",
            session_col="session_date",
            point_col="point",
            out="bench_lvl",
        )

        missing_bench = (
            priced.join(
                endpoints.filter(pl.col("which") == "start").select(
                    "leg_id", pl.col("bench_lvl").alias("bench_start")
                ),
                on="leg_id",
            )
            .join(
                endpoints.filter(pl.col("which") == "end").select(
                    "leg_id", pl.col("bench_lvl").alias("bench_end")
                ),
                on="leg_id",
            )
            .filter(pl.col("bench_start").is_null() | pl.col("bench_end").is_null())
        )
        if missing_bench.height:
            ids = ", ".join(missing_bench["leg_id"].head(5).to_list())
            raise MissingBenchmarkData(f"Missing benchmark levels for legs: {ids}")
        missing_beta = priced.filter(pl.col("beta_adjusted") & pl.col("beta").is_null())
        if missing_beta.height:
            ids = ", ".join(missing_beta["leg_id"].head(5).to_list())
            raise MissingBenchmarkData(f"Missing beta for beta_adjusted legs: {ids}")

        terminal_mult = 1.0 + pl.col("final_return").fill_null(0.0)
        ends = (
            endpoints.filter(pl.col("which") == "end")
            .select(
                "leg_id",
                pl.col("lvl_tr").alias("end_lvl_raw"),
                pl.col("lvl_px").alias("end_px_raw"),
                pl.col("bench_lvl").alias("bench_end"),
            )
            .join(
                priced.select(
                    "leg_id",
                    "terminated_by_delisting",
                    "final_return",
                ),
                on="leg_id",
            )
            .with_columns(
                pl.when(pl.col("terminated_by_delisting"))
                .then(pl.col("end_lvl_raw") * terminal_mult)
                .otherwise(pl.col("end_lvl_raw"))
                .alias("end_lvl"),
                pl.when(pl.col("terminated_by_delisting"))
                .then(pl.col("end_px_raw") * terminal_mult)
                .otherwise(pl.col("end_px_raw"))
                .alias("end_px"),
            )
            .select("leg_id", "end_lvl", "end_px", "bench_end")
        )
        starts = endpoints.filter(pl.col("which") == "start").select(
            "leg_id",
            pl.col("lvl_tr").alias("start_lvl"),
            pl.col("lvl_px").alias("entry_price"),
            pl.col("bench_lvl").alias("bench_start"),
        )
        total_basis = methodology.return_basis == ReturnBasis.TOTAL
        priced_out = (
            priced.join(starts, on="leg_id")
            .join(ends, on="leg_id")
            .with_columns(
                pl.col("end_px").alias("exit_price"),
                pl.when(pl.col("beta_adjusted"))
                .then(pl.col("beta") * (pl.col("bench_end") / pl.col("bench_start") - 1.0))
                .otherwise(pl.col("bench_end") / pl.col("bench_start") - 1.0)
                .alias("benchmark_return"),
            )
            .with_columns(
                pl.when(total_basis)
                .then(
                    pl.when(pl.col("start_lvl").is_not_null() & pl.col("end_lvl").is_not_null())
                    .then(pl.col("end_lvl") / pl.col("start_lvl") - 1.0)
                    .otherwise(None)
                )
                .otherwise(
                    pl.when(
                        pl.col("entry_price").is_not_null() & pl.col("exit_price").is_not_null()
                    )
                    .then(pl.col("exit_price") / pl.col("entry_price") - 1.0)
                    .otherwise(None)
                )
                .alias("stock_return"),
            )
            .with_columns(
                pl.when(
                    pl.col("stock_return").is_not_null() & pl.col("benchmark_return").is_not_null()
                )
                .then(pl.col("stock_return") - pl.col("benchmark_return"))
                .otherwise(None)
                .alias("excess_return")
            )
            .select(
                "leg_id",
                "entry_price",
                "exit_price",
                "stock_return",
                "benchmark_return",
                "excess_return",
            )
        )

    unpriced = enriched.filter(pl.col("end_session").is_null()).select(
        "leg_id",
        pl.lit(None, dtype=pl.Float64).alias("entry_price"),
        pl.lit(None, dtype=pl.Float64).alias("exit_price"),
        pl.lit(None, dtype=pl.Float64).alias("stock_return"),
        pl.lit(None, dtype=pl.Float64).alias("benchmark_return"),
        pl.lit(None, dtype=pl.Float64).alias("excess_return"),
    )
    computed = unpriced if priced_out is None else pl.concat([priced_out, unpriced], how="vertical")
    return (
        enriched.drop(
            [
                column
                for column in ("beta_adjusted", "beta_value", "del_date", "final_return")
                if column in enriched.columns
            ]
        )
        .join(computed, on="leg_id", how="left")
        .with_columns(pl.col("terminated_by_delisting").fill_null(False))
    )


def build_returns(
    intervals: pl.DataFrame,
    security_daily: pl.DataFrame,
    benchmark_daily: pl.DataFrame,
    beta: pl.DataFrame,
    delistings: pl.DataFrame,
    classifications: pl.DataFrame,
    sessions: pl.DataFrame,
    methodology: Methodology,
    context: RunContext,
) -> pl.DataFrame:
    """Build ``RETURN_FRAME``: ``to_exit`` plus every fixed horizon, per interval."""
    horizons = [HORIZON_TO_EXIT] + [horizon_label(days) for days in methodology.fixed_horizons_days]

    investable = intervals.filter(pl.col("is_investable"))
    leg_rows: list[dict[str, object]] = []
    non_investable_rows: list[dict[str, object]] = []

    if investable.height:
        by_exchange, all_dates = _session_calendars(classifications, sessions, context.as_of_date)
        exchange_of = _exchange_at_entry(investable, classifications)
        for row in investable.iter_rows(named=True):
            interval_id = str(row["interval_id"])
            exchange_id = exchange_of.get(interval_id)
            dates = (
                by_exchange.get(exchange_id, all_dates) if exchange_id is not None else all_dates
            )
            entry_session = row["entry_session"]
            for horizon in horizons:
                if horizon == HORIZON_TO_EXIT:
                    end_session = row["exit_session"]
                    end_point = row["exit_point"]
                    reason = None
                else:
                    end_session = _horizon_end(entry_session, int(horizon[1:]), dates)
                    end_point = POINT_CLOSE if end_session is not None else None
                    reason = None if end_session is not None else "horizon_incomplete"
                leg_rows.append(
                    {
                        "leg_id": f"{interval_id}|{horizon}",
                        "interval_id": interval_id,
                        "horizon": horizon,
                        "security_id": row["security_id"],
                        "entry_session": entry_session,
                        "start_session": entry_session,
                        "start_point": row["entry_point"],
                        "end_session": end_session,
                        "end_point": end_point,
                        "unavailable_reason": reason,
                    }
                )

    for row in intervals.filter(~pl.col("is_investable")).iter_rows(named=True):
        interval_id = str(row["interval_id"])
        for horizon in horizons:
            non_investable_rows.append(
                {
                    "interval_id": interval_id,
                    "horizon": horizon,
                    "start_session": row["entry_session"],
                    "start_point": row["entry_point"],
                    "end_session": None,
                    "end_point": None,
                    "entry_price": None,
                    "exit_price": None,
                    "stock_return": None,
                    "benchmark_id": None,
                    "benchmark_return": None,
                    "beta": None,
                    "excess_return": None,
                    "terminated_by_delisting": False,
                    "unavailable_reason": "non_investable",
                }
            )

    if leg_rows:
        legs = pl.DataFrame(leg_rows, schema=_LEG_ROW_SCHEMA)
        priced = compute_legs(
            legs,
            security_daily=security_daily,
            benchmark_daily=benchmark_daily,
            beta=beta,
            delistings=delistings,
            classifications=classifications,
            methodology=methodology,
        )
        investable_out = priced.select(
            "interval_id",
            "horizon",
            "start_session",
            "start_point",
            "end_session",
            "end_point",
            "entry_price",
            "exit_price",
            "stock_return",
            "benchmark_id",
            "benchmark_return",
            "beta",
            "excess_return",
            "terminated_by_delisting",
            "unavailable_reason",
        )
    else:
        investable_out = pl.DataFrame(schema=_SCHEMA)

    non_investable_out = pl.DataFrame(non_investable_rows, schema=_SCHEMA)
    frame = pl.concat([investable_out, non_investable_out], how="vertical")
    return frame.select(list(_SCHEMA)).sort("interval_id", "horizon")
