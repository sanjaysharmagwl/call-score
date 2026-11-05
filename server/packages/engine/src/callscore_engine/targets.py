"""Price-target outcomes (decision 0003): did the adjusted price reach the target?

One row per PIT-filtered rating event carrying a positive ``price_target`` and
a ``pt_horizon_months``. ``target_price`` is stated in raw units at
announcement and is converted to the split-continuous basis with the
``adj_factor`` on the entry session, so it compares directly against the
``*_px`` levels from ``pricing.execution_levels``.

Entry resolution, in order:

1. the interval the event opened (``intervals.event_id``), using its stored
   ``entry_session``/``entry_point``;
2. for same-rating events, the interval already open at ``available_ts``
   (``entry_ts <= available_ts < exit_ts``) for the same analyst and security,
   using its ``entry_session``/``entry_point``;
3. otherwise the first session open strictly after ``available_ts`` on the
   security's exchange (looked up on ``SESSION_FRAME``; the exchange is the
   one whose sessions best match the security's ``security_daily`` dates).

Events with no reachable entry session or missing price levels are omitted:
the contract's entry/target columns are non-nullable.
"""

import calendar
from datetime import date

import polars as pl

from callscore_core.contracts import RunContext
from callscore_core.frames import TARGET_OUTCOME_FRAME
from callscore_engine.pricing import execution_levels


def _add_months(day: date, months: int) -> date:
    """``day`` shifted by ``months`` calendar months, clamping the day-of-month."""
    total = day.year * 12 + (day.month - 1) + months
    year, month = divmod(total, 12)
    month += 1
    return date(year, month, min(day.day, calendar.monthrange(year, month)[1]))


def _security_exchanges(security_daily: pl.DataFrame, sessions: pl.DataFrame) -> pl.DataFrame:
    """``security_id, exchange_id``: the exchange covering most of the security's dates."""
    dates = security_daily.select("security_id", "session_date").unique()
    overlap = dates.join(sessions.select("exchange_id", "session_date"), on="session_date")
    return (
        overlap.group_by("security_id", "exchange_id")
        .agg(pl.len().alias("_n"))
        .sort("security_id", "_n", "exchange_id", descending=[False, True, False])
        .unique("security_id", keep="first", maintain_order=True)
        .select("security_id", "exchange_id")
    )


def _target_events(events: pl.DataFrame) -> pl.DataFrame:
    """Events eligible for a target outcome, with ``_avail`` = available_ts."""
    return (
        events.with_columns(pl.max_horizontal("effective_ts", "knowledge_ts").alias("_avail"))
        .filter(
            pl.col("price_target").is_not_null()
            & (pl.col("price_target") > 0)
            & pl.col("pt_horizon_months").is_not_null()
        )
        .select(
            "event_id",
            "analyst_id",
            "security_id",
            "price_target",
            "pt_horizon_months",
            "_avail",
        )
    )


def _entry_points(
    ev: pl.DataFrame,
    intervals: pl.DataFrame,
    sessions: pl.DataFrame,
) -> pl.DataFrame:
    """Resolve ``entry_session``/``entry_point`` per event (see module docstring)."""
    # 1) The interval this event opened.
    opened = (
        ev.select("event_id")
        .join(
            intervals.select("event_id", "entry_session", "entry_point"),
            on="event_id",
            how="inner",
        )
        .unique("event_id", keep="first", maintain_order=True)
        .rename({"entry_session": "_es", "entry_point": "_ep"})
    )
    # 2) The interval already open at available_ts (same-rating events).
    open_at = (
        ev.select("event_id", "analyst_id", "security_id", "_avail")
        .join(
            intervals.select(
                "analyst_id",
                "security_id",
                "entry_ts",
                "exit_ts",
                "entry_session",
                "entry_point",
                "interval_id",
            ),
            on=["analyst_id", "security_id"],
        )
        .filter((pl.col("entry_ts") <= pl.col("_avail")) & (pl.col("exit_ts") > pl.col("_avail")))
        .sort("event_id", "entry_ts", "interval_id", descending=[False, True, True])
        .unique("event_id", keep="first", maintain_order=True)
        .select(
            "event_id",
            pl.col("entry_session").alias("_es_open"),
            pl.col("entry_point").alias("_ep_open"),
        )
    )
    # 3) First session open strictly after available_ts on the event's exchange.
    nxt = (
        ev.select("event_id", "exchange_id", "_avail")
        .join(sessions.select("exchange_id", "session_date", "open_ts"), on="exchange_id")
        .filter(pl.col("open_ts") > pl.col("_avail"))
        .sort("event_id", "open_ts")
        .unique("event_id", keep="first", maintain_order=True)
        .select("event_id", pl.col("session_date").alias("_es_next"))
        .with_columns(pl.lit("open").alias("_ep_next"))
    )
    return (
        ev.join(opened, on="event_id", how="left")
        .join(open_at, on="event_id", how="left")
        .join(nxt, on="event_id", how="left")
        .with_columns(
            pl.coalesce("_es", "_es_open", "_es_next").alias("entry_session"),
            pl.coalesce("_ep", "_ep_open", "_ep_next").alias("entry_point"),
        )
        .drop("_es", "_ep", "_es_open", "_ep_open", "_es_next", "_ep_next")
    )


def build_target_outcomes(
    events: pl.DataFrame,
    intervals: pl.DataFrame,
    security_daily: pl.DataFrame,
    sessions: pl.DataFrame,
    context: RunContext,
) -> pl.DataFrame:
    """Build ``TARGET_OUTCOME_FRAME``: one row per targeted event, sorted by ``event_id``."""
    as_of = context.as_of_date
    ev = _target_events(events)
    if not ev.height:
        return _empty_outcomes()
    ev = ev.join(_security_exchanges(security_daily, sessions), on="security_id", how="left")
    ev = _entry_points(ev, intervals, sessions)
    ev = ev.filter(pl.col("entry_session").is_not_null())
    if not ev.height:
        return _empty_outcomes()

    levels = execution_levels(security_daily)
    adj = security_daily.select("security_id", "session_date", "adj_factor")

    ev = ev.join(
        levels.select(
            "security_id",
            pl.col("session_date").alias("_eds"),
            pl.col("open_px").alias("_open_px"),
            pl.col("close_px").alias("_entry_close_px"),
        ),
        left_on=["security_id", "entry_session"],
        right_on=["security_id", "_eds"],
        how="left",
    ).join(
        adj.rename({"session_date": "entry_session"}),
        on=["security_id", "entry_session"],
        how="left",
    )
    ev = ev.with_columns(
        pl.when(pl.col("entry_point") == "open")
        .then(pl.col("_open_px"))
        .when(pl.col("entry_point") == "close")
        .then(pl.col("_entry_close_px"))
        .otherwise(None)
        .alias("entry_price"),
        (pl.col("price_target") * pl.col("adj_factor")).alias("target_price"),
    ).with_columns((pl.col("target_price") / pl.col("entry_price") - 1).alias("implied_return"))

    # Horizon end: last exchange session on/before entry_session + N calendar months.
    ev = ev.with_columns(
        pl.struct("entry_session", "pt_horizon_months")
        .map_elements(
            lambda row: _add_months(row["entry_session"], int(row["pt_horizon_months"])),
            return_dtype=pl.Date,
        )
        .alias("_horizon_date")
    )
    horizon = (
        ev.select("event_id", "exchange_id", "_horizon_date")
        .join(sessions.select("exchange_id", "session_date"), on="exchange_id")
        .filter(pl.col("session_date") <= pl.col("_horizon_date"))
        .group_by("event_id")
        .agg(pl.col("session_date").max().alias("horizon_end_session"))
    )
    last_asof = (
        sessions.filter(pl.col("session_date") <= as_of)
        .group_by("exchange_id")
        .agg(pl.col("session_date").max().alias("_last_session"))
    )
    ev = (
        ev.join(horizon, on="event_id", how="left")
        .join(last_asof, on="exchange_id", how="left")
        .with_columns(
            (
                pl.col("horizon_end_session").is_not_null()
                & (pl.col("horizon_end_session") <= pl.col("_last_session"))
            ).alias("is_matured")
        )
    )
    ev = ev.filter(pl.col("horizon_end_session").is_not_null())
    if not ev.height:
        return _empty_outcomes()

    # Realized return at the horizon close and the intra-horizon high/low reach.
    ev = ev.join(
        levels.select(
            "security_id",
            pl.col("session_date").alias("_hds"),
            pl.col("close_px").alias("_horizon_close_px"),
        ),
        left_on=["security_id", "horizon_end_session"],
        right_on=["security_id", "_hds"],
        how="left",
    )
    reach = (
        ev.select(
            "event_id",
            "security_id",
            "entry_session",
            "horizon_end_session",
            "implied_return",
            "target_price",
            "is_matured",
        )
        .join(
            levels.select("security_id", "session_date", "high_px", "low_px"),
            on="security_id",
        )
        .filter(
            (pl.col("session_date") >= pl.col("entry_session"))
            & (pl.col("session_date") <= pl.col("horizon_end_session"))
        )
        .group_by("event_id")
        .agg(
            pl.col("high_px").max().alias("_max_high_px"),
            pl.col("low_px").min().alias("_min_low_px"),
        )
    )
    ev = ev.join(reach, on="event_id", how="left")
    return (
        ev.filter(
            pl.col("entry_price").is_not_null()
            & pl.col("target_price").is_not_null()
            & pl.col("implied_return").is_not_null()
        )
        .with_columns(
            pl.when(pl.col("is_matured"))
            .then(pl.col("_horizon_close_px") / pl.col("entry_price") - 1)
            .otherwise(None)
            .alias("realized_return"),
            pl.when(pl.col("is_matured") & (pl.col("implied_return") > 0))
            .then(pl.col("_max_high_px") >= pl.col("target_price"))
            .when(pl.col("is_matured") & (pl.col("implied_return") < 0))
            .then(pl.col("_min_low_px") <= pl.col("target_price"))
            .otherwise(None)
            .alias("hit"),
        )
        .select(
            "event_id",
            "analyst_id",
            "entry_session",
            "horizon_end_session",
            "entry_price",
            "target_price",
            "implied_return",
            "realized_return",
            "hit",
            "is_matured",
        )
        .sort("event_id")
    )


def _empty_outcomes() -> pl.DataFrame:
    return pl.DataFrame(
        schema={
            "event_id": pl.String(),
            "analyst_id": pl.String(),
            "entry_session": pl.Date(),
            "horizon_end_session": pl.Date(),
            "entry_price": pl.Float64(),
            "target_price": pl.Float64(),
            "implied_return": pl.Float64(),
            "realized_return": pl.Float64(),
            "hit": pl.Boolean(),
            "is_matured": pl.Boolean(),
        }
    )


def assert_target_outcomes_contract(frame: pl.DataFrame) -> None:
    """Private conformance check used by tests: columns, dtypes, sort, key."""
    contract = TARGET_OUTCOME_FRAME
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
