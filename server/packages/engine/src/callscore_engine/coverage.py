"""Daily coverage flags: which (analyst, security, session) rows are stale.

One row per covered session inside each coverage spell (``to_date`` inclusive,
null open-ended clipped to ``as_of_date``), on the security's exchange per the
classification spell active on each date, never past ``as_of_date``.

``is_stale`` is policy-independent: the latest rating event visible for that
analyst and security strictly before the session close (``available_ts =
max(effective_ts, knowledge_ts) < close_ts``, ``knowledge_ts`` within the run's
knowledge cutoff) has an ``expiry_date`` earlier than the session date.
Sessions before the analyst's first visible rating are not stale.
"""

from datetime import date, datetime

import polars as pl

from callscore_core.contracts import RunContext
from callscore_core.frames import COVERAGE_DAILY_FRAME


def _covered_sessions(
    coverage_spells: pl.DataFrame,
    classifications: pl.DataFrame,
    sessions: pl.DataFrame,
    as_of: date,
) -> pl.DataFrame:
    """``analyst_id, security_id, session_date, close_ts`` inside coverage spells."""
    spells = (
        coverage_spells.filter(pl.col("from_date") <= as_of)
        .with_columns(
            pl.min_horizontal(pl.col("to_date").fill_null(as_of), pl.lit(as_of)).alias("_end")
        )
        .filter(pl.col("_end") >= pl.col("from_date"))
        .select("analyst_id", "security_id", "from_date", "_end")
    )
    sp = (
        spells.join(
            classifications.select("security_id", "from_date", "to_date", "exchange_id"),
            on="security_id",
            suffix="_cls",
        )
        .with_columns(
            pl.max_horizontal("from_date", "from_date_cls").alias("_start"),
            pl.min_horizontal("_end", pl.col("to_date").fill_null(as_of)).alias("_stop"),
        )
        .filter(pl.col("_stop") >= pl.col("_start"))
    )
    return (
        sp.join(
            sessions.select("exchange_id", "session_date", "close_ts"),
            on="exchange_id",
            how="inner",
        )
        .filter(
            (pl.col("session_date") >= pl.col("_start"))
            & (pl.col("session_date") <= pl.col("_stop"))
        )
        .select("analyst_id", "security_id", "session_date", "close_ts")
        .unique(
            subset=["analyst_id", "security_id", "session_date"],
            keep="first",
            maintain_order=True,
        )
    )


def _latest_expiries(
    rows: pl.DataFrame, events: pl.DataFrame, knowledge_cutoff: datetime
) -> pl.DataFrame:
    """``expiry_date`` of each row's latest visible rating before session close."""
    ev = (
        events.with_columns(pl.max_horizontal("effective_ts", "knowledge_ts").alias("_avail"))
        .filter(pl.col("knowledge_ts") <= knowledge_cutoff)
        .select("analyst_id", "security_id", "_avail", "expiry_date", "event_id")
    )
    before_close = rows.join(ev, on=["analyst_id", "security_id"], how="inner").filter(
        pl.col("_avail") < pl.col("close_ts")
    )
    return (
        before_close.sort("_avail", "event_id")
        .unique(
            subset=["analyst_id", "security_id", "session_date"],
            keep="last",
            maintain_order=True,
        )
        .select("analyst_id", "security_id", "session_date", "expiry_date")
    )


def build_coverage_daily(
    events: pl.DataFrame,
    intervals: pl.DataFrame,
    coverage_spells: pl.DataFrame,
    sessions: pl.DataFrame,
    classifications: pl.DataFrame,
    context: RunContext,
) -> pl.DataFrame:
    """Build ``COVERAGE_DAILY_FRAME`` sorted by ``(analyst_id, security_id, session_date)``.

    ``intervals`` is accepted for caller symmetry with the other stage inputs;
    staleness is defined purely by rating events and coverage spells.
    """
    rows = _covered_sessions(coverage_spells, classifications, sessions, context.as_of_date)
    latest = _latest_expiries(rows, events, context.knowledge_cutoff)
    return (
        rows.drop("close_ts")
        .join(latest, on=["analyst_id", "security_id", "session_date"], how="left")
        .with_columns(
            (
                pl.col("expiry_date").is_not_null()
                & (pl.col("expiry_date") < pl.col("session_date"))
            ).alias("is_stale")
        )
        .select("analyst_id", "security_id", "session_date", "is_stale")
        .sort("analyst_id", "security_id", "session_date")
    )


def assert_coverage_daily_contract(frame: pl.DataFrame) -> None:
    """Private conformance check used by tests: columns, dtypes, sort, key."""
    contract = COVERAGE_DAILY_FRAME
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
    assert frame.select(contract.primary_key).n_unique() == frame.height
    assert frame.select(contract.sort_by).equals(
        frame.select(contract.sort_by).sort(contract.sort_by)
    )
