"""Property tests for build_intervals: no overlaps, order invariance, PIT."""

from datetime import UTC, date, datetime, timedelta
from typing import Any

import hypothesis.strategies as st
import polars as pl
from hypothesis import given, settings

from callscore_core.contracts import RunContext
from callscore_core.methodology import Methodology
from callscore_engine.intervals import build_intervals

XNYS = "XNYS"
AS_OF = date(2024, 1, 23)
FAR_CUTOFF = datetime(2024, 12, 31, tzinfo=UTC)


def _ts(day: date, hour: int, minute: int = 0) -> datetime:
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=UTC)


def _weekdays(start: date, n: int) -> list[date]:
    out: list[date] = []
    day = start
    while len(out) < n:
        if day.weekday() < 5:
            out.append(day)
        day += timedelta(days=1)
    return out


DATES = _weekdays(date(2024, 1, 2), 15)

_EVENT_SCHEMA: dict[str, pl.DataType] = {
    "event_id": pl.String,
    "analyst_id": pl.String,
    "firm_id": pl.String,
    "security_id": pl.String,
    "announced_ts": pl.Datetime("us", "UTC"),
    "effective_ts": pl.Datetime("us", "UTC"),
    "knowledge_ts": pl.Datetime("us", "UTC"),
    "action_type": pl.String,
    "native_rating": pl.String,
    "canonical_rating": pl.String,
    "prior_canonical_rating": pl.String,
    "scale_id": pl.String,
    "price_target": pl.Float64,
    "pt_currency": pl.String,
    "pt_horizon_months": pl.Int64,
    "expiry_date": pl.Date,
    "supersedes_event_id": pl.String,
    "is_correction": pl.Boolean,
}


def _sessions() -> pl.DataFrame:
    return pl.DataFrame(
        {
            "exchange_id": [XNYS] * len(DATES),
            "session_date": DATES,
            "open_ts": [_ts(d, 14, 30) for d in DATES],
            "close_ts": [_ts(d, 21, 0) for d in DATES],
        }
    )


def _context(cutoff: datetime = FAR_CUTOFF) -> RunContext:
    return RunContext(
        as_of_date=AS_OF,
        knowledge_cutoff=cutoff,
        source_revision="rev1",
        source_fingerprint="0" * 64,
        universe_fingerprint="1" * 64,
        algorithm_version="v1",
    )


_ACTIONS = [
    "initiation",
    "upgrade",
    "downgrade",
    "reiteration",
    "pt_change",
    "suspend",
    "restrict",
    "resume",
    "drop",
]
_CANONICALS = [
    "strong_buy",
    "buy",
    "hold",
    "sell",
    "strong_sell",
    "not_rated",
    "under_review",
    "restricted",
]
_ANALYSTS = ["A1", "A2"]
_SECURITIES = ["S1", "S2"]
_HOURS = [0, 8, 12, 15, 22]


@st.composite
def _event_rows(draw: st.DrawFn) -> list[dict[str, Any]]:
    n = draw(st.integers(min_value=0, max_value=8))
    rows: list[dict[str, Any]] = []
    for i in range(n):
        day = draw(st.sampled_from(DATES))
        hour = draw(st.sampled_from(_HOURS))
        minute = draw(st.integers(min_value=0, max_value=59))
        base = _ts(day, hour, minute)
        knowledge = base + timedelta(minutes=draw(st.integers(min_value=-120, max_value=2880)))
        expiry = draw(st.one_of(st.none(), st.sampled_from(DATES)))
        rows.append(
            {
                "event_id": f"E{i}",
                "analyst_id": draw(st.sampled_from(_ANALYSTS)),
                "firm_id": "F1",
                "security_id": draw(st.sampled_from(_SECURITIES)),
                "announced_ts": base,
                "effective_ts": base,
                "knowledge_ts": knowledge,
                "action_type": draw(st.sampled_from(_ACTIONS)),
                "native_rating": draw(st.sampled_from(["Buy", "Hold", "Sell"])),
                "canonical_rating": draw(st.sampled_from(_CANONICALS)),
                "prior_canonical_rating": None,
                "scale_id": "scale1",
                "price_target": None,
                "pt_currency": None,
                "pt_horizon_months": None,
                "expiry_date": expiry,
                "supersedes_event_id": None,
                "is_correction": False,
            }
        )
    return rows


def _frames_for(rows: list[dict[str, Any]]) -> tuple[pl.DataFrame, pl.DataFrame, pl.DataFrame]:
    """Open-ended coverage/employment spells and classifications for used ids."""
    pairs = sorted({(r["analyst_id"], r["security_id"]) for r in rows}) or [("A1", "S1")]
    analysts = sorted({a for a, _ in pairs})
    securities = sorted({s for _, s in pairs})
    coverage = pl.DataFrame(
        {
            "analyst_id": [a for a, _ in pairs],
            "security_id": [s for _, s in pairs],
            "from_date": [date(2024, 1, 1)] * len(pairs),
            "to_date": [None] * len(pairs),
            "is_lead": [True] * len(pairs),
        }
    )
    employment = pl.DataFrame(
        {
            "analyst_id": analysts,
            "firm_id": ["F1"] * len(analysts),
            "from_date": [date(2024, 1, 1)] * len(analysts),
            "to_date": [None] * len(analysts),
        }
    )
    classifications = pl.DataFrame(
        {
            "security_id": securities,
            "from_date": [date(2024, 1, 1)] * len(securities),
            "to_date": [None] * len(securities),
            "exchange_id": [XNYS] * len(securities),
            "currency": ["USD"] * len(securities),
            "sector_id": ["TECH"] * len(securities),
            "region": ["NA"] * len(securities),
            "cap_bucket": ["large"] * len(securities),
            "listing_date": [date(2024, 1, 1)] * len(securities),
            "delisting_date": [None] * len(securities),
        }
    )
    return coverage, employment, classifications


def _run(rows: list[dict[str, Any]], *, cutoff: datetime = FAR_CUTOFF) -> pl.DataFrame:
    coverage, employment, classifications = _frames_for(rows)
    return build_intervals(
        events=pl.DataFrame(rows, schema=_EVENT_SCHEMA),
        sessions=_sessions(),
        coverage_spells=coverage,
        employment_spells=employment,
        classifications=classifications,
        delistings=pl.DataFrame(),
        methodology=Methodology(),
        context=_context(cutoff),
    )


@settings(max_examples=60, deadline=None)
@given(rows=_event_rows())
def test_no_overlapping_investable_intervals(rows: list[dict[str, Any]]) -> None:
    df = _run(rows)
    investable = df.filter(pl.col("is_investable")).sort(
        ["analyst_id", "security_id", "entry_ts", "interval_id"]
    )
    for (analyst, security), group in investable.group_by(
        ["analyst_id", "security_id"], maintain_order=True
    ):
        entries = group["entry_ts"].to_list()
        exits = group["exit_ts"].to_list()
        for prev_exit, next_entry in zip(exits, entries[1:], strict=False):
            assert prev_exit <= next_entry, (
                f"overlap for {(analyst, security)}: exit {prev_exit} > entry {next_entry}"
            )


@settings(max_examples=40, deadline=None)
@given(rows=_event_rows(), seed=st.integers(min_value=0, max_value=2**31 - 1))
def test_row_order_invariance(rows: list[dict[str, Any]], seed: int) -> None:
    if not rows:
        return
    shuffled = pl.DataFrame(rows, schema=_EVENT_SCHEMA).sample(
        fraction=1.0, shuffle=True, seed=seed
    )
    coverage, employment, classifications = _frames_for(rows)
    kwargs: dict[str, Any] = {
        "sessions": _sessions(),
        "coverage_spells": coverage,
        "employment_spells": employment,
        "classifications": classifications,
        "delistings": pl.DataFrame(),
        "methodology": Methodology(),
        "context": _context(),
    }
    ordered_events = pl.DataFrame(rows, schema=_EVENT_SCHEMA)
    a = build_intervals(events=ordered_events, **kwargs)
    b = build_intervals(events=shuffled, **kwargs)
    assert a.equals(b)


@settings(max_examples=40, deadline=None)
@given(
    rows=_event_rows(),
    cutoff_idx=st.integers(min_value=0, max_value=2000),
)
def test_no_event_after_knowledge_cutoff_is_used(
    rows: list[dict[str, Any]], cutoff_idx: int
) -> None:
    # Cutoff sweeps a range around the generated knowledge timestamps.
    base = _ts(DATES[0], 0)
    cutoff = base + timedelta(minutes=cutoff_idx)
    df = _run(rows, cutoff=cutoff)
    allowed = {r["event_id"] for r in rows if r["knowledge_ts"] <= cutoff}
    for event_id in df["event_id"].to_list():
        assert event_id in allowed
    # Every emitted row respects the cutoff on its own knowledge_ts.
    assert (df["knowledge_ts"] <= cutoff).all()


@settings(max_examples=30, deadline=None)
@given(rows=_event_rows())
def test_output_columns_and_sort(rows: list[dict[str, Any]]) -> None:
    from callscore_core.frames import INTERVAL_FRAME

    df = _run(rows)
    assert df.columns == list(INTERVAL_FRAME.column_names)
    assert df.equals(df.sort(*INTERVAL_FRAME.sort_by))
    assert df["interval_id"].n_unique() == df.height
