"""Tests for schema bootstrap, versioning, constraints and referential integrity."""

from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from callscore_store.db import create_engine_for
from callscore_store.models import (
    Analyst,
    CalcRun,
    Delisting,
    Firm,
    MethodologyVersion,
    RatingEvent,
    RatingInterval,
    Security,
    SecurityDaily,
)
from callscore_store.schema import (
    SCHEMA_VERSION,
    SchemaTooNewError,
    UnmanagedSchemaError,
    bootstrap,
    current_version,
    table_names,
)

_TS = datetime(2024, 1, 10, 14, 30, tzinfo=UTC)
_AS_OF = date(2024, 1, 31)


def _engine(tmp_path: Path):
    return create_engine_for(tmp_path / "test.db")


def _make_v1_db(engine) -> None:
    """Hand-build a version-1 database (baseline + one surviving firm row)."""
    with engine.begin() as conn:
        conn.execute(
            text(
                "CREATE TABLE schema_version ("
                "  id INTEGER PRIMARY KEY CHECK (id = 1),"
                "  version INTEGER NOT NULL CHECK (version >= 1))"
            )
        )
        conn.execute(text("INSERT INTO schema_version (id, version) VALUES (1, 1)"))
        conn.execute(text("CREATE TABLE firm (firm_id TEXT PRIMARY KEY, name TEXT NOT NULL)"))
        conn.execute(text("CREATE TABLE analyst (analyst_id TEXT PRIMARY KEY, name TEXT NOT NULL)"))
        conn.execute(text("INSERT INTO firm (firm_id, name) VALUES ('F1', 'Firm One')"))
        conn.execute(text("INSERT INTO analyst (analyst_id, name) VALUES ('A1', 'Analyst One')"))


def _seed_reference(session: Session) -> None:
    "Insert the minimal reference rows needed by rating_event."
    session.add(Firm(firm_id="F1", name="Firm One"))
    session.add(Analyst(analyst_id="A1", name="Analyst One"))
    session.add(Security(security_id="S1", name="Security One"))
    session.flush()


def _event_kwargs(**overrides: object) -> dict[str, object]:
    row: dict[str, object] = {
        "event_id": "E1",
        "analyst_id": "A1",
        "firm_id": "F1",
        "security_id": "S1",
        "announced_ts": _TS,
        "effective_ts": _TS,
        "knowledge_ts": _TS,
        "action_type": "initiation",
        "native_rating": "Buy",
        "canonical_rating": "buy",
        "prior_canonical_rating": None,
        "scale_id": "F1:5tier",
        "price_target": Decimal("123.456789"),
        "pt_currency": "USD",
        "pt_horizon_months": 12,
        "expiry_date": None,
        "supersedes_event_id": None,
        "is_correction": False,
        "source": "test",
    }
    row.update(overrides)
    return row


def _add_event(session: Session, **overrides: object) -> None:
    session.add(RatingEvent(**_event_kwargs(**overrides)))  # type: ignore[arg-type]
    session.flush()


def _expect_integrity_error(session: Session, fn) -> None:
    """Run ``fn`` inside a savepoint; assert it raises IntegrityError."""
    with pytest.raises(IntegrityError), session.begin_nested():
        fn()
        session.flush()


def _seed_run(session: Session) -> CalcRun:
    mv = MethodologyVersion(
        params_json="{}",
        fingerprint="0" * 64,
        is_baseline=True,
        created_at=_TS,
    )
    session.add(mv)
    session.flush()
    run = CalcRun(
        run_id="R1",
        methodology_version_id=mv.id,
        as_of_date=_AS_OF,
        knowledge_cutoff=_TS,
        status="succeeded",
        algorithm_version="v1",
    )
    session.add(run)
    session.flush()
    return run


class TestBootstrap:
    def test_fresh_bootstrap(self, tmp_path: Path) -> None:
        engine = _engine(tmp_path)
        assert current_version(engine) == 0
        assert bootstrap(engine) == SCHEMA_VERSION
        assert current_version(engine) == SCHEMA_VERSION
        names = table_names(engine)
        for expected in (
            "firm",
            "analyst",
            "rating_event",
            "security_daily",
            "calc_run",
            "rating_interval",
            "analyst_metric",
            "analyst_composite",
            "portfolio_nav",
            "backtest",
            "writer_lease",
        ):
            assert expected in names

    def test_bootstrap_idempotent(self, tmp_path: Path) -> None:
        engine = _engine(tmp_path)
        assert bootstrap(engine) == SCHEMA_VERSION
        assert bootstrap(engine) == SCHEMA_VERSION
        assert current_version(engine) == SCHEMA_VERSION

    def test_upgrade_from_v1_preserves_data(self, tmp_path: Path) -> None:
        engine = _engine(tmp_path)
        _make_v1_db(engine)
        assert current_version(engine) == 1
        assert bootstrap(engine) == SCHEMA_VERSION
        with engine.connect() as conn:
            firm = conn.execute(text("SELECT firm_id, name, country FROM firm")).one()
            analyst_cols = {r[1] for r in conn.execute(text("PRAGMA table_info(analyst)"))}
        assert firm == ("F1", "Firm One", None)
        assert "firm_id" in analyst_cols
        assert "rating_event" in table_names(engine)

    def test_newer_version_refused(self, tmp_path: Path) -> None:
        engine = _engine(tmp_path)
        bootstrap(engine)
        with engine.begin() as conn:
            conn.execute(
                text("UPDATE schema_version SET version = :v"),
                {"v": SCHEMA_VERSION + 1},
            )
        with pytest.raises(SchemaTooNewError, match="newer"):
            bootstrap(engine)

    def test_unmanaged_schema_refused(self, tmp_path: Path) -> None:
        engine = _engine(tmp_path)
        with engine.begin() as conn:
            conn.execute(text("CREATE TABLE stray (id INTEGER PRIMARY KEY)"))
        with pytest.raises(UnmanagedSchemaError):
            bootstrap(engine)
        with pytest.raises(UnmanagedSchemaError):
            current_version(engine)


class TestConstraints:
    def test_foreign_key_rejection(self, tmp_path: Path) -> None:
        engine = _engine(tmp_path)
        bootstrap(engine)
        with Session(engine) as session:
            _seed_reference(session)
            _expect_integrity_error(session, lambda: _add_event(session, analyst_id="NOPE"))

    def test_event_dedup_and_correction_sharing(self, tmp_path: Path) -> None:
        engine = _engine(tmp_path)
        bootstrap(engine)
        with Session(engine) as session:
            _seed_reference(session)
            _add_event(session)
            # Same (analyst, security, announced_ts) non-correction: rejected.
            _expect_integrity_error(session, lambda: _add_event(session, event_id="E2"))
            # A correction may share the timestamp.
            _add_event(session, event_id="E3", is_correction=True)
            # A different timestamp is fine.
            later = datetime(2024, 1, 11, 14, 30, tzinfo=UTC)
            _add_event(
                session,
                event_id="E4",
                announced_ts=later,
                effective_ts=later,
                knowledge_ts=later,
            )

    def test_check_rejects_bad_enum(self, tmp_path: Path) -> None:
        engine = _engine(tmp_path)
        bootstrap(engine)
        with Session(engine) as session:
            _seed_reference(session)
            _expect_integrity_error(
                session, lambda: _add_event(session, action_type="not_an_action")
            )
            _expect_integrity_error(session, lambda: _add_event(session, canonical_rating="moon"))

    def test_check_rejects_bad_run_status(self, tmp_path: Path) -> None:
        engine = _engine(tmp_path)
        bootstrap(engine)
        with Session(engine) as session:
            mv = MethodologyVersion(
                params_json="{}",
                fingerprint="0" * 64,
                is_baseline=False,
                created_at=_TS,
            )
            session.add(mv)
            session.flush()

            def bad_run() -> None:
                session.add(
                    CalcRun(
                        run_id="R1",
                        methodology_version_id=mv.id,
                        as_of_date=_AS_OF,
                        knowledge_cutoff=_TS,
                        status="bogus",
                        algorithm_version="v1",
                    )
                )

            _expect_integrity_error(session, bad_run)

    def test_check_rejects_bad_delisting_reason(self, tmp_path: Path) -> None:
        engine = _engine(tmp_path)
        bootstrap(engine)
        with Session(engine) as session:
            _seed_reference(session)

            def bad_delisting() -> None:
                session.add(
                    Delisting(
                        security_id="S1",
                        delisting_date=date(2024, 1, 31),
                        final_return=-1.0,
                        reason="raid",
                    )
                )

            _expect_integrity_error(session, bad_delisting)

    def test_date_ordering_check(self, tmp_path: Path) -> None:
        engine = _engine(tmp_path)
        bootstrap(engine)
        with Session(engine) as session:
            _seed_reference(session)

            def bad_spell() -> None:
                session.execute(
                    text(
                        "INSERT INTO analyst_coverage "
                        "(analyst_id, security_id, from_date, to_date, is_lead) "
                        "VALUES ('A1', 'S1', '2024-02-01', '2024-01-01', 0)"
                    )
                )

            _expect_integrity_error(session, bad_spell)

    def test_price_and_dividend_money_roundtrip(self, tmp_path: Path) -> None:
        engine = _engine(tmp_path)
        bootstrap(engine)
        with Session(engine) as session:
            _seed_reference(session)
            session.add(
                SecurityDaily(
                    security_id="S1",
                    session_date=date(2024, 1, 10),
                    open=Decimal("100.125"),
                    high=Decimal("101.5"),
                    low=Decimal("99.0"),
                    close=Decimal("100.5"),
                    adj_factor=1.0,
                    cash_dividend=Decimal("0.25"),
                    tr_index=100.5,
                    is_halted=False,
                )
            )
            session.flush()
            row = session.get(SecurityDaily, ("S1", date(2024, 1, 10)))
            assert row is not None
            # ORM reads go through Money -> exact Decimal.
            assert row.open == Decimal("100.125000")
            assert row.cash_dividend == Decimal("0.250000")
            # Raw stored form is scaled integer micro-units.
            raw = session.execute(text("SELECT open FROM security_daily")).scalar_one()
        assert raw == 100_125_000

    def test_event_price_target_money_roundtrip(self, tmp_path: Path) -> None:
        engine = _engine(tmp_path)
        bootstrap(engine)
        with Session(engine) as session:
            _seed_reference(session)
            _add_event(session)
            event = session.get(RatingEvent, "E1")
            assert event is not None
            assert event.price_target == Decimal("123.456789")
            raw = session.execute(
                text("SELECT price_target FROM rating_event WHERE event_id = 'E1'")
            ).scalar_one()
            assert raw == 123_456_789

    def test_derived_run_restrict(self, tmp_path: Path) -> None:
        engine = _engine(tmp_path)
        bootstrap(engine)
        with Session(engine) as session:
            _seed_run(session)
            _seed_reference(session)
            _add_event(session)
            session.add(
                RatingInterval(
                    run_id="R1",
                    interval_id="I1",
                    event_id="E1",
                    analyst_id="A1",
                    firm_id="F1",
                    security_id="S1",
                    native_rating="Buy",
                    canonical_rating="buy",
                    action_type="initiation",
                    announced_ts=_TS,
                    effective_ts=_TS,
                    knowledge_ts=_TS,
                    available_ts=_TS,
                    entry_session=date(2024, 1, 10),
                    entry_point="open",
                    entry_ts=_TS,
                    exit_session=date(2024, 2, 10),
                    exit_point="close",
                    exit_ts=_TS,
                    exit_reason="superseded",
                    is_investable=True,
                )
            )
            session.flush()
            # Deleting the run must be restricted while derived rows exist.
            with pytest.raises(IntegrityError), session.begin_nested():
                session.delete(session.get(CalcRun, "R1"))
                session.flush()
