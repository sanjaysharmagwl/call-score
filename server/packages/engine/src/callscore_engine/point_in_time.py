"""Point-in-time visibility of rating events (decision 0003).

A rating is actionable at ``available_ts = max(effective_ts, knowledge_ts)`` —
``knowledge_ts`` governs, so a correction is never usable before the system
learned it. ``visible_events`` applies the run's ``knowledge_cutoff`` and
resolves correction chains: an event superseded by a *visible* correction is
dropped, transitively (a 2-deep chain drops both the original and the
intermediate correction once the final correction is visible; a correction
whose own ``knowledge_ts`` is after the cutoff does not count).
"""

from datetime import UTC, datetime

import polars as pl


def visible_events(events: pl.DataFrame, knowledge_cutoff: datetime) -> pl.DataFrame:
    """Events knowable at ``knowledge_cutoff``, with superseded events removed.

    Keeps exactly the ``RATING_EVENT_FRAME`` columns. An event is superseded
    when a visible event (``knowledge_ts <= cutoff``) has ``is_correction`` and
    ``supersedes_event_id`` pointing at it; resolution is one hop over the
    visible set, which handles chains transitively because intermediate
    corrections remain in the visible set even when they are themselves
    superseded.
    """
    cutoff = knowledge_cutoff.astimezone(UTC)
    visible = events.filter(pl.col("knowledge_ts") <= cutoff)
    if visible.height == 0 or "is_correction" not in events.columns:
        return visible
    superseded_ids = (
        visible.filter(pl.col("is_correction") & pl.col("supersedes_event_id").is_not_null())
        .select(pl.col("supersedes_event_id").cast(pl.String).alias("event_id"))
        .unique()
    )
    return visible.join(superseded_ids, on="event_id", how="anti")


def with_available_ts(events: pl.DataFrame) -> pl.DataFrame:
    """Add ``available_ts = max(effective_ts, knowledge_ts)`` (decision 0003).

    The column is internal to the engine pipeline; ``RATING_EVENT_FRAME`` itself
    is unchanged.
    """
    return events.with_columns(
        pl.max_horizontal("effective_ts", "knowledge_ts").alias("available_ts")
    )
