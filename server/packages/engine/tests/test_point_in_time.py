"""Point-in-time visibility: knowledge cutoff + correction chains."""

from datetime import UTC, datetime

import polars as pl

from callscore_engine.point_in_time import visible_events, with_available_ts

T0 = datetime(2024, 1, 2, 12, 0, tzinfo=UTC)
CUTOFF = datetime(2024, 6, 30, tzinfo=UTC)


def _event(
    event_id: str,
    *,
    knowledge: datetime = T0,
    effective: datetime = T0,
    supersedes: str | None = None,
    is_correction: bool = False,
    canonical: str = "buy",
) -> dict[str, object]:
    return {
        "event_id": event_id,
        "analyst_id": "A1",
        "firm_id": "F1",
        "security_id": "S1",
        "announced_ts": effective,
        "effective_ts": effective,
        "knowledge_ts": knowledge,
        "action_type": "initiation",
        "native_rating": "Buy",
        "canonical_rating": canonical,
        "prior_canonical_rating": None,
        "scale_id": "scale1",
        "price_target": None,
        "pt_currency": None,
        "pt_horizon_months": None,
        "expiry_date": None,
        "supersedes_event_id": supersedes,
        "is_correction": is_correction,
    }


def _frame(rows: list[dict[str, object]]) -> pl.DataFrame:
    return pl.DataFrame(rows)


def test_events_after_cutoff_are_dropped() -> None:
    events = _frame(
        [
            _event("E1", knowledge=T0),
            _event("E2", knowledge=datetime(2024, 7, 1, tzinfo=UTC)),
            _event("E3", knowledge=datetime(2024, 12, 31, tzinfo=UTC)),
        ]
    )
    out = visible_events(events, CUTOFF)
    assert out["event_id"].to_list() == ["E1"]


def test_correction_at_cutoff_boundary_is_visible() -> None:
    # knowledge_ts == cutoff counts as visible (<=).
    events = _frame(
        [
            _event("E1"),
            _event(
                "C1",
                knowledge=CUTOFF,
                supersedes="E1",
                is_correction=True,
                canonical="hold",
            ),
        ]
    )
    out = visible_events(events, CUTOFF)
    assert out["event_id"].to_list() == ["C1"]


def test_one_deep_correction_supersedes_original() -> None:
    events = _frame(
        [
            _event("E1"),
            _event("C1", knowledge=T0, supersedes="E1", is_correction=True),
        ]
    )
    out = visible_events(events, CUTOFF)
    assert out["event_id"].to_list() == ["C1"]
    # All RATING_EVENT_FRAME columns preserved.
    assert out.columns == events.columns


def test_two_deep_chain_keeps_only_final_correction() -> None:
    events = _frame(
        [
            _event("E1"),
            _event("C1", supersedes="E1", is_correction=True),
            _event("C2", supersedes="C1", is_correction=True, canonical="sell"),
        ]
    )
    out = visible_events(events, CUTOFF)
    assert out["event_id"].to_list() == ["C2"]


def test_not_yet_visible_correction_does_not_supersede() -> None:
    late = datetime(2024, 7, 1, tzinfo=UTC)
    events = _frame(
        [
            _event("E1"),
            _event("C1", knowledge=late, supersedes="E1", is_correction=True),
        ]
    )
    out = visible_events(events, CUTOFF)
    assert out["event_id"].to_list() == ["E1"]


def test_two_deep_chain_with_invisible_tip_keeps_middle() -> None:
    # E1 <- C1 <- C2; C2 not yet visible. C1 is the best known correction:
    # E1 is dropped (superseded by visible C1), C1 survives (C2 invisible).
    late = datetime(2024, 7, 1, tzinfo=UTC)
    events = _frame(
        [
            _event("E1"),
            _event("C1", supersedes="E1", is_correction=True, canonical="hold"),
            _event("C2", knowledge=late, supersedes="C1", is_correction=True),
        ]
    )
    out = visible_events(events, CUTOFF)
    assert out["event_id"].to_list() == ["C1"]


def test_invisible_middle_correction_leaves_original() -> None:
    late = datetime(2024, 7, 1, tzinfo=UTC)
    events = _frame(
        [
            _event("E1"),
            _event("C1", knowledge=late, supersedes="E1", is_correction=True),
            _event("C2", knowledge=late, supersedes="C1", is_correction=True),
        ]
    )
    out = visible_events(events, CUTOFF)
    assert out["event_id"].to_list() == ["E1"]


def test_supersedes_without_correction_flag_does_not_supersede() -> None:
    # Only events flagged is_correction act as corrections.
    events = _frame(
        [
            _event("E1"),
            _event("X1", supersedes="E1", is_correction=False),
        ]
    )
    out = visible_events(events, CUTOFF)
    assert out["event_id"].sort().to_list() == ["E1", "X1"]


def test_available_ts_is_max_of_effective_and_knowledge() -> None:
    early = datetime(2024, 1, 2, 10, 0, tzinfo=UTC)
    late = datetime(2024, 1, 2, 15, 0, tzinfo=UTC)
    events = _frame(
        [
            _event("E1", effective=early, knowledge=late),
            _event("E2", effective=late, knowledge=early),
        ]
    )
    out = with_available_ts(events).sort("event_id")
    assert out["available_ts"].to_list() == [late, late]


def test_empty_events() -> None:
    events = pl.DataFrame(
        schema={
            "event_id": pl.String,
            "knowledge_ts": pl.Datetime("us", "UTC"),
            "effective_ts": pl.Datetime("us", "UTC"),
        }
    )
    assert visible_events(events, CUTOFF).height == 0
    assert with_available_ts(events).height == 0
