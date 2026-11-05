"""Validation unit tests: pure validators plus atomic batch rejection."""

from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from callscore_store import create_engine_for
from callscore_store.repositories import (
    DuplicateRowError,
    insert_market,
    insert_rating_events,
    insert_reference,
)
from callscore_store.schema import bootstrap
from callscore_store.validation import (
    ValidationError,
    validate_rating_events,
    validate_security_classifications,
    validate_security_daily,
)

D0 = date(2024, 1, 2)
TS = datetime(2024, 1, 3, 10, 0, tzinfo=UTC)


def _engine(tmp_path: Path):
    engine = create_engine_for(tmp_path / "t.db")
    bootstrap(engine)
    return engine


def _seed_reference(engine) -> None:
    insert_reference(
        engine,
        firms=[{"firm_id": "F1", "name": "Firm", "country": "US"}],
        analysts=[{"analyst_id": "A1", "name": "Analyst", "firm_id": "F1"}],
        securities=[{"security_id": "S1", "name": "Sec"}],
        exchanges=[{"exchange_id": "X1", "name": "Ex", "country": "US", "timezone": "UTC"}],
        sectors=[{"sector_id": "SE1", "name": "Tech", "parent_sector_id": None}],
        rating_scales=[
            {
                "scale_id": "F1:scale",
                "firm_id": "F1",
                "effective_from": D0,
                "effective_to": None,
                "n_tiers": 5,
            }
        ],
    )


def _event(**over):
    row = {
        "event_id": "E1",
        "analyst_id": "A1",
        "firm_id": "F1",
        "security_id": "S1",
        "announced_ts": TS,
        "effective_ts": datetime(2024, 1, 4, 9, 0, tzinfo=UTC),
        "knowledge_ts": datetime(2024, 1, 3, 12, 0, tzinfo=UTC),
        "action_type": "initiation",
        "native_rating": "Buy",
        "canonical_rating": "buy",
        "prior_canonical_rating": None,
        "scale_id": "F1:scale",
        "price_target": Decimal("150.5"),
        "pt_currency": "USD",
        "pt_horizon_months": 12,
        "expiry_date": date(2025, 1, 4),
        "supersedes_event_id": None,
        "is_correction": False,
    }
    row.update(over)
    return row


# --- pure validators ------------------------------------------------------- #


def test_rating_event_bad_enum_rejected() -> None:
    issues = validate_rating_events([_event(canonical_rating="moon")])
    assert any(i.column == "canonical_rating" and i.is_error for i in issues)


def test_rating_event_unknown_scale_flagged_when_scales_given() -> None:
    issues = validate_rating_events([_event(scale_id="nope")], known_scale_ids={"F1:scale"})
    assert any(i.column == "scale_id" and i.is_error for i in issues)
    # Without the DB context the same row has no *errors* (warnings allowed).
    assert not [i for i in validate_rating_events([_event(scale_id="nope")]) if i.is_error]


def test_rating_event_expiry_before_effective_rejected() -> None:
    issues = validate_rating_events([_event(expiry_date=date(2024, 1, 3))])
    assert any(i.column == "expiry_date" and i.is_error for i in issues)


def test_rating_event_knowledge_before_announced_warns_not_errors() -> None:
    issues = validate_rating_events([_event(knowledge_ts=datetime(2024, 1, 1, tzinfo=UTC))])
    warnings = [i for i in issues if not i.is_error]
    assert warnings and warnings[0].column == "knowledge_ts"
    assert not [i for i in issues if i.is_error]


def test_rating_event_supersedes_cycle_detected() -> None:
    rows = [
        _event(event_id="E1", supersedes_event_id="E2"),
        _event(event_id="E2", supersedes_event_id="E1", is_correction=True),
    ]
    issues = validate_rating_events(rows, existing_event_ids=set())
    assert any("cyclic" in i.message for i in issues)


def test_rating_event_supersedes_missing_target_rejected() -> None:
    issues = validate_rating_events([_event(supersedes_event_id="GHOST")])
    assert any(i.column == "supersedes_event_id" and i.is_error for i in issues)


def test_rating_event_money_precision() -> None:
    issues = validate_rating_events([_event(price_target=Decimal("1.1234567"))])
    assert any(i.column == "price_target" and i.is_error for i in issues)


def test_security_daily_ohlc_rules() -> None:
    good = {
        "security_id": "S1",
        "session_date": D0,
        "open": 10.0,
        "high": 11.0,
        "low": 9.0,
        "close": 10.5,
        "adj_factor": 1.0,
        "cash_dividend": None,
        "tr_index": 100.0,
        "is_halted": False,
    }
    assert validate_security_daily([good]) == []
    bad = {**good, "high": 10.4}  # high < close
    issues = validate_security_daily([bad])
    assert any(i.column == "high" for i in issues)
    bad2 = {**good, "low": 10.6}  # low > open
    assert any(i.column == "low" for i in validate_security_daily([bad2]))
    bad3 = {**good, "adj_factor": 0.0}
    assert any(i.column == "adj_factor" for i in validate_security_daily([bad3]))
    bad4 = {**good, "tr_index": -1.0}
    assert any(i.column == "tr_index" for i in validate_security_daily([bad4]))


def test_classification_spells_non_overlap() -> None:
    base = {
        "security_id": "S1",
        "exchange_id": "X1",
        "currency": "USD",
        "sector_id": "SE1",
        "region": "NA",
        "cap_bucket": "large",
        "listing_date": D0,
        "delisting_date": None,
    }
    ok = [
        {**base, "from_date": date(2024, 1, 1), "to_date": date(2024, 6, 30)},
        {**base, "from_date": date(2024, 7, 1), "to_date": None},
    ]
    assert validate_security_classifications(ok) == []
    overlap = [
        {**base, "from_date": date(2024, 1, 1), "to_date": date(2024, 8, 1)},
        {**base, "from_date": date(2024, 6, 1), "to_date": None},
    ]
    issues = validate_security_classifications(overlap)
    assert any(i.is_error for i in issues)
    # Open-ended first spell swallows the second.
    open_ended = [
        {**base, "from_date": date(2024, 1, 1), "to_date": None},
        {**base, "from_date": date(2025, 1, 1), "to_date": None},
    ]
    assert any(i.is_error for i in validate_security_classifications(open_ended))


# --- repository-level atomicity -------------------------------------------- #


def test_insert_rating_events_rejects_batch_atomically(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    _seed_reference(engine)
    good = _event(event_id="E1")
    bad = _event(event_id="E2", canonical_rating="not_a_rating")
    with pytest.raises(ValidationError):
        insert_rating_events(engine, [good, bad])
    # Nothing inserted — including the valid row.
    from sqlalchemy import text

    with engine.connect() as conn:
        n = conn.execute(text("SELECT COUNT(*) FROM rating_event")).scalar()
    assert n == 0


def test_insert_rating_events_dedupe_natural_key(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    _seed_reference(engine)
    insert_rating_events(engine, [_event(event_id="E1")])
    # Same analyst/security/announced_ts, different event_id -> rejected.
    with pytest.raises(DuplicateRowError):
        insert_rating_events(engine, [_event(event_id="E2")])
    # A correction at the same timestamp is allowed.
    insert_rating_events(
        engine, [_event(event_id="E3", is_correction=True, supersedes_event_id="E1")]
    )


def test_insert_rating_events_correction_chain(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    _seed_reference(engine)
    insert_rating_events(engine, [_event(event_id="E1")])
    insert_rating_events(
        engine,
        [
            _event(
                event_id="E2",
                is_correction=True,
                supersedes_event_id="E1",
                canonical_rating="strong_buy",
            )
        ],
    )
    # In-batch chain: E3 supersedes E2 which supersedes E1 — all new.
    insert_rating_events(
        engine,
        [
            _event(event_id="E4", is_correction=True, supersedes_event_id="E3"),
            _event(event_id="E3", is_correction=True, supersedes_event_id="E2"),
        ],
    )


def test_insert_market_atomic_reject(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    _seed_reference(engine)
    insert_reference(
        engine,
        securities=[{"security_id": "S2", "name": "Sec2"}],
    )
    good = {
        "security_id": "S2",
        "session_date": D0,
        "open": 5.0,
        "high": 6.0,
        "low": 4.5,
        "close": 5.5,
        "adj_factor": 1.0,
        "cash_dividend": None,
        "tr_index": 100.0,
        "is_halted": False,
    }
    bad = {**good, "security_id": "S1", "high": 1.0}  # high < open/close
    with pytest.raises(ValidationError):
        insert_market(engine, security_daily=[good, bad])
    from sqlalchemy import text

    with engine.connect() as conn:
        assert conn.execute(text("SELECT COUNT(*) FROM security_daily")).scalar() == 0
