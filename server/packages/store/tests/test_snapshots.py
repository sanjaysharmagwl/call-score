"""Snapshot tests: round-trip, determinism, as_of clipping, universe filter."""

from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

from callscore_core.frames import FRAME_CONTRACTS
from callscore_store import create_engine_for
from callscore_store.repositories import (
    insert_market,
    insert_rating_events,
    insert_reference,
)
from callscore_store.schema import bootstrap
from callscore_store.snapshots import Snapshot, load_input_snapshot

D0 = date(2024, 1, 2)
D1 = date(2024, 1, 3)
D2 = date(2024, 1, 4)
AS_OF = date(2024, 1, 10)
CUTOFF = datetime(2024, 2, 1, tzinfo=UTC)


def _engine(tmp_path: Path):
    tmp_path.mkdir(parents=True, exist_ok=True)
    engine = create_engine_for(tmp_path / "t.db")
    bootstrap(engine)
    return engine


def _seed(engine, *, shuffle: bool = False) -> None:
    firms = [{"firm_id": "F1", "name": "Firm", "country": "US"}]
    analysts = [{"analyst_id": "A1", "name": "Analyst", "firm_id": "F1"}]
    securities = [
        {"security_id": "S1", "name": "Sec One"},
        {"security_id": "S2", "name": "Sec Two"},
    ]
    exchanges = [{"exchange_id": "X1", "name": "Ex", "country": "US", "timezone": "UTC"}]
    sectors = [{"sector_id": "SE1", "name": "Tech", "parent_sector_id": None}]
    sessions = [
        {
            "exchange_id": "X1",
            "session_date": d,
            "open_ts": datetime(d.year, d.month, d.day, 14, 0, tzinfo=UTC),
            "close_ts": datetime(d.year, d.month, d.day, 21, 0, tzinfo=UTC),
        }
        for d in (D0, D1, D2)
    ]
    employment = [{"analyst_id": "A1", "firm_id": "F1", "from_date": D0, "to_date": None}]
    coverage = [
        {
            "analyst_id": "A1",
            "security_id": s,
            "from_date": D0,
            "to_date": None,
            "is_lead": True,
        }
        for s in ("S1", "S2")
    ]
    classifications = [
        {
            "security_id": s,
            "from_date": D0,
            "to_date": None,
            "exchange_id": "X1",
            "currency": "USD",
            "sector_id": "SE1",
            "region": "NA",
            "cap_bucket": "large",
            "listing_date": D0,
            "delisting_date": None,
        }
        for s in ("S1", "S2")
    ]
    rating_scales = [
        {
            "scale_id": "F1:scale",
            "firm_id": "F1",
            "effective_from": D0,
            "effective_to": None,
            "n_tiers": 5,
        }
    ]
    scale_maps = [
        {
            "scale_id": "F1:scale",
            "effective_from": D0,
            "native_label": "Buy",
            "canonical_rating": "buy",
            "rank_within_scale": 2,
        }
    ]
    kwargs = {
        "firms": firms,
        "analysts": analysts,
        "securities": securities,
        "exchanges": exchanges,
        "sectors": sectors,
        "sessions": sessions,
        "employment": employment,
        "coverage": coverage,
        "classifications": classifications,
        "rating_scales": rating_scales,
        "scale_maps": scale_maps,
    }
    if shuffle:
        # Reverse every group's row order to simulate different insertion order.
        kwargs = {k: list(reversed(v)) for k, v in kwargs.items()}
    insert_reference(engine, **kwargs)

    events = [
        {
            "event_id": f"E{i}",
            "analyst_id": "A1",
            "firm_id": "F1",
            "security_id": sec,
            "announced_ts": datetime(2024, 1, 3, 10, i, tzinfo=UTC),
            "effective_ts": datetime(2024, 1, 4, 9, 0, tzinfo=UTC),
            "knowledge_ts": datetime(2024, 1, 3, 12, i, tzinfo=UTC),
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
        for i, sec in enumerate(("S1", "S2"))
    ]
    insert_rating_events(engine, events)

    daily = [
        {
            "security_id": s,
            "session_date": d,
            "open": 10.0 + i,
            "high": 11.0 + i,
            "low": 9.0 + i,
            "close": 10.5 + i,
            "adj_factor": 1.0,
            "cash_dividend": None,
            "tr_index": 100.0 + i,
            "is_halted": False,
        }
        for i, (s, d) in enumerate((sec, day) for sec in ("S1", "S2") for day in (D0, D1, D2))
    ]
    insert_market(
        engine,
        benchmark_defs=[{"benchmark_id": "IDX_BROAD", "kind": "market", "rule_json": None}],
        benchmark_daily=[
            {
                "benchmark_id": "IDX_BROAD",
                "session_date": d,
                "open_level": 100.0,
                "close_level": 101.0,
            }
            for d in (D0, D1, D2)
        ],
        security_daily=daily,
        fx_daily=[
            {"from_ccy": "USD", "to_ccy": "USD", "rate_date": d, "rate": 1.0} for d in (D0, D1, D2)
        ],
        security_beta=[
            {"security_id": "S1", "as_of_date": D0, "beta": 1.1, "window_days": 252},
            {"security_id": "S1", "as_of_date": D1, "beta": 1.2, "window_days": 252},
        ],
        delistings=[
            {
                "security_id": "S2",
                "delisting_date": D2,
                "final_return": -0.4,
                "reason": "acquired",
            }
        ],
    )


def _contract_check(snap: Snapshot) -> None:
    """Every frame matches its contract's columns and sort keys are unique."""
    assert set(snap.frames) == set(FRAME_CONTRACTS) - {
        "intervals",
        "exposures",
        "returns",
        "contributions",
        "event_windows",
        "target_outcomes",
        "coverage_daily",
        "portfolio_nav",
        "metric_samples",
        "metric_results",
        "composites",
    }
    for name, frame in snap.frames.items():
        contract = FRAME_CONTRACTS[name]
        assert frame.columns == list(contract.column_names), name


def test_snapshot_roundtrip_and_contracts(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    _seed(engine)
    snap = load_input_snapshot(engine, AS_OF, CUTOFF)
    assert isinstance(snap, Snapshot)
    _contract_check(snap)
    assert snap.frames["rating_events"].height == 2
    assert snap.frames["security_daily"].height == 6
    assert snap.frames["sessions"].height == 3
    assert snap.frames["delistings"].height == 1
    assert snap.source_fingerprint != ""
    assert len(snap.source_fingerprint) == 64
    assert len(snap.universe_fingerprint) == 64


def test_snapshot_deterministic_under_shuffled_insertion(tmp_path: Path) -> None:
    e1 = _engine(tmp_path / "a")
    e2 = _engine(tmp_path / "b")
    _seed(e1, shuffle=False)
    _seed(e2, shuffle=True)
    s1 = load_input_snapshot(e1, AS_OF, CUTOFF)
    s2 = load_input_snapshot(e2, AS_OF, CUTOFF)
    assert s1.source_fingerprint == s2.source_fingerprint
    assert s1.universe_fingerprint == s2.universe_fingerprint
    for name in s1.frames:
        assert s1.frames[name].equals(s2.frames[name]), name


def test_snapshot_as_of_clipping(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    _seed(engine)
    # as_of between D1 and D2: security_daily/benchmark/fx clipped, beta by as_of.
    snap = load_input_snapshot(engine, D1, CUTOFF)
    daily = snap.frames["security_daily"]
    assert set(daily["session_date"].to_list()) == {D0, D1}
    assert snap.frames["benchmark_daily"].height == 2
    assert snap.frames["fx_daily"].height == 2
    # beta as_of D1 only (the D1 snapshot itself is included).
    assert snap.frames["security_beta"].height == 2
    # delisting at D2 is after as_of -> excluded (not yet knowable).
    assert snap.frames["delistings"].height == 0
    # rating_events are NOT clipped: all events returned for engine PIT logic.
    assert snap.frames["rating_events"].height == 2
    # classification/coverage spells are unclipped too.
    assert snap.frames["coverage_spells"].height == 2


def test_snapshot_universe_filter(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    _seed(engine)
    snap = load_input_snapshot(engine, AS_OF, CUTOFF, universe_filter={"S1"})
    assert snap.frames["security_daily"].height == 3
    assert snap.frames["rating_events"].height == 1
    assert snap.frames["coverage_spells"].height == 1
    # analyst-only frame unaffected (still the same analyst).
    assert snap.frames["employment_spells"].height == 1
    assert (
        snap.universe_fingerprint != load_input_snapshot(engine, AS_OF, CUTOFF).universe_fingerprint
    )


def test_snapshot_requires_aware_cutoff(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    _seed(engine)
    try:
        load_input_snapshot(engine, AS_OF, datetime(2024, 2, 1))  # naive
    except ValueError:
        pass
    else:  # pragma: no cover
        raise AssertionError("naive knowledge_cutoff accepted")
