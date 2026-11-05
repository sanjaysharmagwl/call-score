"""Tests for the securities API on a tmp SQLite DB.

The DB is seeded by hand: two securities (S1 current, S2 delisted), PIT
classification spells, identifiers, prices, consensus and four rating
events, then one real ``calc_run`` via ``services.calculation`` so derived
(interval/return/contribution/target) rows exist for the run-scoped
tests.
"""

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, func, select

from callscore_api.app import create_app
from callscore_api.routers import changes as changes_router
from callscore_api.routers import overview as overview_router
from callscore_api.routers import sectors as sectors_router
from callscore_api.routers import securities as securities_router
from callscore_api.services import calculation
from callscore_api.services import market_queries as mq
from callscore_core.methodology import Methodology
from callscore_store import create_engine_for
from callscore_store.repositories import events, market, reference
from callscore_store.schema import bootstrap

AS_OF = date(2025, 1, 31)
RUN_ID = "run-1"


def _weekdays(start: date, n: int) -> list[date]:
    days: list[date] = []
    day = start
    while len(days) < n:
        if day.weekday() < 5:
            days.append(day)
        day += timedelta(days=1)
    return days


SESSION_DAYS = _weekdays(date(2025, 1, 6), 16)


def _ts(d: date, hour: int = 12) -> datetime:
    return datetime.combine(d, datetime.min.time(), UTC).replace(hour=hour)


def _seed(engine: Engine) -> None:
    sessions = [
        {
            "exchange_id": "XNYS",
            "session_date": d,
            "open_ts": datetime.combine(d, datetime.min.time(), UTC).replace(hour=14, minute=30),
            "close_ts": datetime.combine(d, datetime.min.time(), UTC).replace(hour=21),
        }
        for d in SESSION_DAYS
    ]
    reference.insert_reference(
        engine,
        firms=[{"firm_id": "F1", "name": "Fund One"}],
        analysts=[
            {"analyst_id": "A1", "name": "Analyst One", "firm_id": "F1"},
            {"analyst_id": "A2", "name": "Analyst Two", "firm_id": "F1"},
        ],
        exchanges=[{"exchange_id": "XNYS", "name": "NYSE"}],
        sectors=[
            {"sector_id": "SEC1", "name": "Sector One"},
            {"sector_id": "SEC2", "name": "Sector Two"},
        ],
        securities=[
            {"security_id": "S1", "name": "Security One"},
            {"security_id": "S2", "name": "Security Two"},
        ],
        sessions=sessions,
        employment=[
            {"analyst_id": "A1", "firm_id": "F1", "from_date": date(2020, 1, 1), "to_date": None},
            {"analyst_id": "A2", "firm_id": "F1", "from_date": date(2020, 1, 1), "to_date": None},
        ],
        coverage=[
            {
                "analyst_id": "A1",
                "security_id": "S1",
                "from_date": date(2020, 1, 1),
                "to_date": None,
                "is_lead": True,
            },
            {
                "analyst_id": "A2",
                "security_id": "S1",
                "from_date": date(2020, 1, 1),
                "to_date": None,
                "is_lead": False,
            },
        ],
        identifiers=[
            {
                "security_id": "S1",
                "id_type": "ticker",
                "value": "TICK1",
                "from_date": date(2020, 1, 1),
                "to_date": None,
            },
            {
                "security_id": "S1",
                "id_type": "isin",
                "value": "US0000000001",
                "from_date": date(2020, 1, 1),
                "to_date": None,
            },
            {
                "security_id": "S2",
                "id_type": "ticker",
                "value": "TICK2",
                "from_date": date(2020, 1, 1),
                "to_date": None,
            },
        ],
        classifications=[
            {
                "security_id": "S1",
                "from_date": date(2020, 1, 1),
                "to_date": date(2024, 12, 31),
                "exchange_id": "XNYS",
                "currency": "USD",
                "sector_id": "SEC1",
                "region": "US",
                "cap_bucket": "large",
                "listing_date": date(2010, 1, 1),
                "delisting_date": None,
            },
            {
                "security_id": "S1",
                "from_date": date(2025, 1, 1),
                "to_date": None,
                "exchange_id": "XNYS",
                "currency": "USD",
                "sector_id": "SEC2",
                "region": "US",
                "cap_bucket": "large",
                "listing_date": date(2010, 1, 1),
                "delisting_date": None,
            },
            {
                "security_id": "S2",
                "from_date": date(2020, 1, 1),
                "to_date": None,
                "exchange_id": "XNYS",
                "currency": "USD",
                "sector_id": "SEC1",
                "region": "US",
                "cap_bucket": "small",
                "listing_date": date(2015, 1, 1),
                "delisting_date": SESSION_DAYS[4],
            },
        ],
        rating_scales=[
            {
                "scale_id": "F1:main",
                "firm_id": "F1",
                "effective_from": date(2020, 1, 1),
                "n_tiers": 5,
            }
        ],
    )

    daily_s1 = []
    for i, d in enumerate(SESSION_DAYS):
        close = 100.0 + i
        daily_s1.append(
            {
                "security_id": "S1",
                "session_date": d,
                "open": close * 0.99,
                "high": close * 1.01,
                "low": close * 0.98,
                "close": close,
                "adj_factor": 1.0,
                "cash_dividend": None,
                "tr_index": 100.0 + i,
                "is_halted": False,
            }
        )
    daily_s2 = [
        {
            "security_id": "S2",
            "session_date": d,
            "open": 50.0,
            "high": 51.0,
            "low": 49.0,
            "close": 50.0,
            "adj_factor": 1.0,
            "cash_dividend": None,
            "tr_index": 100.0,
            "is_halted": False,
        }
        for d in SESSION_DAYS[:5]
    ]
    bench_defs = [
        {"benchmark_id": "IDX_BROAD", "kind": "market"},
        {"benchmark_id": "SECTOR_SEC1", "kind": "sector"},
        {"benchmark_id": "SECTOR_SEC2", "kind": "sector"},
    ]
    bench_daily = [
        {
            "benchmark_id": b["benchmark_id"],
            "session_date": d,
            "open_level": 100.0,
            "close_level": 100.0,
        }
        for b in bench_defs
        for d in SESSION_DAYS
    ]
    market.insert_market(
        engine,
        benchmark_defs=bench_defs,
        security_daily=daily_s1 + daily_s2,
        benchmark_daily=bench_daily,
        consensus_daily=[
            {
                "security_id": "S1",
                "session_date": d,
                "n_ratings": 2,
                "consensus_score": 0.5,
                "target_price": Decimal("105.5"),
                "target_currency": "USD",
            }
            for d in SESSION_DAYS[:8]
        ],
        delistings=[
            {
                "security_id": "S2",
                "delisting_date": SESSION_DAYS[4],
                "final_return": -0.5,
                "reason": "bankrupt",
            }
        ],
    )

    def _event(
        eid: str,
        analyst: str,
        day: int,
        action: str,
        native: str,
        canonical: str,
        prior: str | None,
        correction: bool = False,
    ) -> dict:
        d = SESSION_DAYS[day]
        return {
            "event_id": eid,
            "analyst_id": analyst,
            "firm_id": "F1",
            "security_id": "S1",
            "announced_ts": _ts(d),
            "effective_ts": _ts(d),
            "knowledge_ts": _ts(d),
            "action_type": action,
            "native_rating": native,
            "canonical_rating": canonical,
            "prior_canonical_rating": prior,
            "scale_id": "F1:main",
            "price_target": Decimal("110") if eid == "e1" else None,
            "pt_currency": "USD" if eid == "e1" else None,
            "pt_horizon_months": 12 if eid == "e1" else None,
            "expiry_date": None,
            "supersedes_event_id": None,
            "is_correction": correction,
        }

    events.insert_rating_events(
        engine,
        [
            _event("e1", "A1", 0, "initiation", "Buy", "buy", None),
            _event("e2", "A1", 3, "upgrade", "Strong Buy", "strong_buy", "buy"),
            _event("e3", "A1", 6, "downgrade", "Hold", "hold", "strong_buy", correction=True),
            _event("e4", "A2", 1, "initiation", "Buy", "buy", None),
        ],
    )


@pytest.fixture()
def engine(tmp_path: Path) -> Engine:
    eng = create_engine_for(tmp_path / "callscore.db")
    bootstrap(eng)
    _seed(eng)
    calculation.prepare_run(
        eng,
        Methodology(),
        run_id=RUN_ID,
        as_of_date=AS_OF,
        knowledge_cutoff=calculation.default_knowledge_cutoff(AS_OF),
    )
    calculation.execute_run(eng, RUN_ID)
    return eng


@pytest.fixture()
def client(engine: Engine, tmp_path: Path) -> TestClient:
    app = create_app(web_dist=tmp_path / "no-dist")
    for r in (
        securities_router.router,
        overview_router.router,
        sectors_router.router,
        changes_router.router,
    ):
        app.include_router(r, prefix="/api")
    app.dependency_overrides[mq.get_engine] = lambda: engine
    yield TestClient(app)
    app.dependency_overrides.clear()
    engine.dispose()


# --------------------------------------------------------------------------- #
# Overview
# --------------------------------------------------------------------------- #


def _sql_counts(engine: Engine) -> dict[str, int]:
    from callscore_store.models import (
        AnalystComposite,
        CalcRun,
        Delisting,
        RatingEvent,
        Security,
        TradingSession,
    )

    with engine.connect() as conn:
        return {
            "analysts": conn.execute(
                select(func.count())
                .select_from(AnalystComposite.__table__)
                .where(AnalystComposite.run_id == RUN_ID)
            ).scalar_one(),
            "runs": conn.execute(select(func.count()).select_from(CalcRun.__table__)).scalar_one(),
            "sessions": conn.execute(
                select(func.count()).select_from(TradingSession.__table__)
            ).scalar_one(),
            "securities": conn.execute(
                select(func.count()).select_from(Security.__table__)
            ).scalar_one(),
            "delisted": conn.execute(
                select(func.count()).select_from(Delisting.__table__)
            ).scalar_one(),
            "events": conn.execute(
                select(func.count()).select_from(RatingEvent.__table__)
            ).scalar_one(),
        }


def test_overview_reconciles_with_sql(client: TestClient, engine: Engine) -> None:
    body = client.get("/api/overview", params={"run_id": RUN_ID}).json()
    counts = _sql_counts(engine)

    assert body["run"]["run_id"] == RUN_ID
    assert body["run"]["status"] == "succeeded"
    assert body["run"]["methodology_fingerprint"] is not None

    a = body["analysts"]
    assert a["n_analysts"] == counts["analysts"]
    assert a["n_ranked"] <= a["n_analysts"]
    assert a["n_scored"] <= a["n_analysts"]
    assert sum(b["count"] for b in a["score_distribution"]) == a["n_scored"] or a["n_scored"] == 0
    assert len(a["top"]) <= 5 and len(a["bottom"]) <= 5
    if a["top"]:
        scores = [t["composite_score"] for t in a["top"] if t["composite_score"] is not None]
        assert scores == sorted(scores, reverse=True)

    rc = body["run_counts_by_status"]
    assert sum(rc.values()) == counts["runs"]
    assert rc.get("succeeded", 0) == 1

    dc = body["data_coverage"]
    assert dc["n_sessions"] == counts["sessions"]
    assert dc["n_securities"] == counts["securities"]
    assert dc["n_delisted_securities"] == counts["delisted"]
    assert dc["n_rating_events"] == counts["events"]
    assert dc["first_session"] == SESSION_DAYS[0].isoformat()
    assert dc["last_session"] == SESSION_DAYS[-1].isoformat()
    assert dc["latest_event_ts"] is not None

    prov = body["provenance"]
    assert "source_revisions" in prov and "generation_manifest" in prov


def test_overview_default_run_and_404(client: TestClient) -> None:
    default = client.get("/api/overview").json()
    explicit = client.get("/api/overview", params={"run_id": RUN_ID}).json()
    assert default["run"]["run_id"] == RUN_ID
    assert default["analysts"]["n_analysts"] == explicit["analysts"]["n_analysts"]
    assert client.get("/api/overview", params={"run_id": "nope"}).status_code == 404
