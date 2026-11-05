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
from callscore_store.models import IntervalReturn, RatingInterval
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
# Security list / search
# --------------------------------------------------------------------------- #


def test_list_includes_delisted(client: TestClient) -> None:
    body = client.get("/api/securities").json()
    assert body["pagination"]["total"] == 2
    by_id = {i["security_id"]: i for i in body["data"]}
    assert by_id["S2"]["is_delisted"] is True
    assert by_id["S1"]["is_delisted"] is False
    # Latest classification: S1 -> SEC2, S2 -> SEC1.
    assert by_id["S1"]["sector_id"] == "SEC2"
    assert by_id["S2"]["sector_id"] == "SEC1"
    assert by_id["S1"]["ticker"] == "TICK1"


def test_search_by_name_ticker_and_identifier(client: TestClient) -> None:
    assert (
        client.get("/api/securities", params={"q": "Security Two"}).json()["pagination"]["total"]
        == 1
    )
    tick = client.get("/api/securities", params={"q": "TICK2"}).json()
    assert [i["security_id"] for i in tick["data"]] == ["S2"]
    isin = client.get("/api/securities", params={"q": "US0000000001"}).json()
    assert [i["security_id"] for i in isin["data"]] == ["S1"]


def test_sector_and_cap_bucket_filters(client: TestClient) -> None:
    sec1 = client.get("/api/securities", params={"sector": "SEC1"}).json()
    assert [i["security_id"] for i in sec1["data"]] == ["S2"]
    sec2 = client.get("/api/securities", params={"sector": "SEC2"}).json()
    assert [i["security_id"] for i in sec2["data"]] == ["S1"]
    small = client.get("/api/securities", params={"cap_bucket": "small"}).json()
    assert [i["security_id"] for i in small["data"]] == ["S2"]


def test_pit_classification_at_past_as_of(client: TestClient) -> None:
    body = client.get("/api/securities", params={"as_of": "2024-06-30"}).json()
    s1 = next(i for i in body["data"] if i["security_id"] == "S1")
    assert s1["sector_id"] == "SEC1"  # spell effective then, not the later SEC2
    body2 = client.get("/api/securities", params={"as_of": "2025-02-01"}).json()
    s1b = next(i for i in body2["data"] if i["security_id"] == "S1")
    assert s1b["sector_id"] == "SEC2"


def test_pagination_bounds(client: TestClient) -> None:
    body = client.get("/api/securities", params={"limit": 1, "offset": 1}).json()
    assert body["pagination"] == {"limit": 1, "offset": 1, "count": 1, "total": 2}
    assert len(body["data"]) == 1
    assert client.get("/api/securities", params={"limit": 501}).status_code == 422
    assert client.get("/api/securities", params={"offset": -1}).status_code == 422


def test_securities_csv(client: TestClient) -> None:
    resp = client.get("/api/securities", params={"format": "csv"})
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/csv")
    lines = resp.text.strip().splitlines()
    assert lines[0].startswith("security_id,name")
    assert len(lines) == 3  # header + 2 securities


# --------------------------------------------------------------------------- #
# Security detail
# --------------------------------------------------------------------------- #


def test_security_detail(client: TestClient) -> None:
    body = client.get("/api/securities/S1").json()
    assert body["security_id"] == "S1"
    assert body["latest_classification"]["sector_id"] == "SEC2"
    assert len(body["classification_spells"]) == 2
    id_types = {i["id_type"] for i in body["identifiers"]}
    assert id_types == {"ticker", "isin"}
    assert body["coverage_analysts"] == 2
    assert body["price_coverage"]["n_rows"] == len(SESSION_DAYS)
    assert body["rating_event_count"] == 4
    assert body["is_delisted"] is False


def test_security_detail_delisted(client: TestClient) -> None:
    body = client.get("/api/securities/S2").json()
    assert body["is_delisted"] is True
    assert body["delisting"]["reason"] == "bankrupt"
    assert body["delisting"]["final_return"] == -0.5


def test_security_detail_404(client: TestClient) -> None:
    assert client.get("/api/securities/NOPE").status_code == 404


# --------------------------------------------------------------------------- #
# Prices
# --------------------------------------------------------------------------- #


def test_prices_series_and_adjusted(client: TestClient) -> None:
    body = client.get("/api/securities/S1/prices").json()
    assert body["total"] == len(SESSION_DAYS)
    assert len(body["rows"]) == len(SESSION_DAYS)
    row = body["rows"][0]
    assert row["session_date"] == SESSION_DAYS[0].isoformat()
    assert row["adj_close"] == pytest.approx(row["close"] * row["adj_factor"])
    assert row["tr_index"] == pytest.approx(100.0)
    # adjusted=false leaves adjusted fields null
    raw = client.get("/api/securities/S1/prices", params={"adjusted": "false"}).json()
    assert raw["rows"][0]["adj_close"] is None


def test_prices_date_bounds_and_interval(client: TestClient) -> None:
    body = client.get(
        "/api/securities/S1/prices",
        params={"from": "2025-01-06", "to": "2025-01-10"},
    ).json()
    assert body["total"] == 5
    assert body["rows"][-1]["session_date"] == "2025-01-10"
    weekly = client.get("/api/securities/S1/prices", params={"interval": "1w"}).json()
    assert weekly["interval"] == "1w"
    assert weekly["total"] < body["total"] + len(SESSION_DAYS)
    assert weekly["rows"][0]["n_sessions"] >= 1
    # resampled close must be a real close (last session of bucket)
    assert weekly["rows"][-1]["close"] == pytest.approx(115.0)


def test_prices_csv_and_404(client: TestClient) -> None:
    resp = client.get("/api/securities/S1/prices", params={"format": "csv"})
    assert resp.headers["content-type"].startswith("text/csv")
    assert "session_date,open" in resp.text.splitlines()[0]
    assert client.get("/api/securities/NOPE/prices").status_code == 404
    assert client.get("/api/securities/S1/prices", params={"interval": "1h"}).status_code == 422


def test_prices_downsample_preserves_extrema(engine: Engine) -> None:
    rows = [
        {
            "security_id": "S1",
            "session_date": SESSION_DAYS[0] + timedelta(days=i),
            "open": 1.0,
            "high": 1.0,
            "low": 1.0,
            "close": float(i + 1),  # 1..16 strictly increasing
            "adj_factor": 1.0,
            "cash_dividend": None,
            "tr_index": 1.0,
            "is_halted": False,
        }
        for i in range(16)
    ]
    # Direct unit check of the bucket-extrema contract.
    ds = mq._bucket_extrema([dict(r) for r in rows], 4)
    closes = [r["close"] for r in ds]
    assert min(closes) == 1.0 and max(closes) == 16.0
    assert len(ds) <= 8  # 4 buckets x (min+max) worst case, dedup may shrink

    # Endpoint path: shrink the threshold so the 16-row series downsamples.
    orig = mq.PRICE_DOWNSAMPLE_TARGET
    mq.PRICE_DOWNSAMPLE_TARGET = 6
    try:
        result = mq.security_prices(engine, "S1", interval="1d")
    finally:
        mq.PRICE_DOWNSAMPLE_TARGET = orig
    assert result["downsampled"] is True
    assert result["source_rows"] == len(SESSION_DAYS)
    closes = [r["close"] for r in result["rows"]]
    assert min(closes) == pytest.approx(min(r["close"] for r in result["rows"]))
    # global min/max close of the source series preserved
    from callscore_store.models import SecurityDaily

    all_rows = mq._all(
        engine,
        select(SecurityDaily)
        .where(SecurityDaily.security_id == "S1")
        .order_by(SecurityDaily.session_date),
    )
    all_closes = [float(r["close"]) for r in all_rows]
    assert min(closes) == pytest.approx(min(all_closes))
    assert max(closes) == pytest.approx(max(all_closes))
    # first and last rows always survive
    assert result["rows"][0]["session_date"] == SESSION_DAYS[0]
    assert result["rows"][-1]["session_date"] == SESSION_DAYS[-1]


# --------------------------------------------------------------------------- #
# Ratings / consensus / performance
# --------------------------------------------------------------------------- #


def test_ratings_without_run(client: TestClient) -> None:
    body = client.get("/api/securities/S1/ratings").json()
    assert body["pagination"]["total"] == 4
    assert all(i["interval_id"] is None for i in body["data"])
    e3 = next(i for i in body["data"] if i["event_id"] == "e3")
    assert e3["is_correction"] is True
    assert e3["prior_canonical_rating"] == "strong_buy"
    e2 = next(i for i in body["data"] if i["event_id"] == "e2")
    assert e2["canonical_rating"] == "strong_buy"
    assert e2["analyst_name"] == "Analyst One"


def test_ratings_with_run_intervals(client: TestClient, engine: Engine) -> None:
    body = client.get("/api/securities/S1/ratings", params={"run_id": RUN_ID}).json()
    assert body["run_id"] == RUN_ID
    with_iv = [i for i in body["data"] if i["interval_id"] is not None]
    assert len(with_iv) >= 1
    row = with_iv[0]
    assert row["entry_session"] is not None
    assert row["excess_return"] is not None or row["stock_return"] is not None

    # date filter: only events announced on/after day 3
    d3 = SESSION_DAYS[3].isoformat()
    filtered = client.get(
        "/api/securities/S1/ratings",
        params={"from": d3, "to": d3},
    ).json()
    assert filtered["pagination"]["total"] == 1
    assert filtered["data"][0]["event_id"] == "e2"

    assert client.get("/api/securities/S1/ratings", params={"run_id": "nope"}).status_code == 404
    assert client.get("/api/securities/NOPE/ratings").status_code == 404


def test_consensus_series(client: TestClient) -> None:
    body = client.get("/api/securities/S1/consensus").json()
    assert len(body["consensus"]) == 8
    assert body["consensus"][0]["n_ratings"] == 2
    assert body["consensus"][0]["target_price"] == pytest.approx(105.5)
    assert body["target_outcomes"] == []

    with_run = client.get("/api/securities/S1/consensus", params={"run_id": RUN_ID}).json()
    # e1 carried a price target -> at least one target outcome row may exist
    # (maturity depends on the run horizon); assert shape consistency instead.
    for o in with_run["target_outcomes"]:
        assert o["event_id"] in {"e1", "e2", "e3", "e4"}
        assert "hit" in o and "is_matured" in o


def test_security_performance_reconciles(client: TestClient, engine: Engine) -> None:
    body = client.get("/api/securities/S1/performance", params={"run_id": RUN_ID}).json()
    # Direct SQL reconciliation.
    with engine.connect() as conn:
        n_intervals = conn.execute(
            select(func.count())
            .select_from(RatingInterval.__table__)
            .where(
                RatingInterval.run_id == RUN_ID,
                RatingInterval.security_id == "S1",
            )
        ).scalar_one()
        n_inv = conn.execute(
            select(func.count())
            .select_from(RatingInterval.__table__)
            .where(
                RatingInterval.run_id == RUN_ID,
                RatingInterval.security_id == "S1",
                RatingInterval.is_investable.is_(True),
            )
        ).scalar_one()
    assert body["n_ratings"] == n_intervals
    assert body["n_investable"] == n_inv
    assert body["run"]["run_id"] == RUN_ID
    to_exit = [h for h in body["by_horizon"] if h["horizon"] == "to_exit"]
    assert len(to_exit) == 1

    # mean_excess_return equals the mean of stored to_exit excess returns.
    interval_ids = [
        r["interval_id"]
        for r in mq._all(
            engine,
            select(RatingInterval.interval_id).where(
                RatingInterval.run_id == RUN_ID,
                RatingInterval.security_id == "S1",
            ),
        )
    ]
    rets = mq._all(
        engine,
        select(IntervalReturn.excess_return).where(
            IntervalReturn.run_id == RUN_ID,
            IntervalReturn.interval_id.in_(interval_ids),
            IntervalReturn.horizon == "to_exit",
            IntervalReturn.excess_return.is_not(None),
        ),
    )
    expected = sum(float(r["excess_return"]) for r in rets) / len(rets)
    assert body["mean_excess_return"] == pytest.approx(expected)

    assert (
        client.get("/api/securities/S1/performance", params={"run_id": "nope"}).status_code == 404
    )
    assert (
        client.get("/api/securities/NOPE/performance", params={"run_id": RUN_ID}).status_code == 404
    )
