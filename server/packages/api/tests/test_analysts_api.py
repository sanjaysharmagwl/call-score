"""Tests for ``/api/analysts*`` and ``/api/leaderboard``.

A tmp SQLite DB is seeded by hand (4 analysts, 2 securities, 12 sessions) and
the real engine pipeline runs synchronously via ``services.calculation`` —
no mocks. A second run at an earlier ``as_of_date`` proves run isolation.
"""

import csv
import io
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import Engine

from callscore_api.routers import analysts as analysts_router
from callscore_api.routers import leaderboard as leaderboard_router
from callscore_api.services import calculation
from callscore_core.methodology import Methodology
from callscore_store import create_engine_for
from callscore_store.repositories import events, market, reference
from callscore_store.schema import bootstrap

AS_OF = date(2025, 1, 21)  # last of 12 seeded weekdays
CUTOFF = calculation.default_knowledge_cutoff(AS_OF)
AS_OF_EARLY = date(2025, 1, 10)  # second run: shorter horizon


def _weekdays(start: date, n: int) -> list[date]:
    days: list[date] = []
    day = start
    while len(days) < n:
        if day.weekday() < 5:
            days.append(day)
        day += timedelta(days=1)
    return days


SESSION_DAYS = _weekdays(date(2025, 1, 6), 12)

ANALYSTS = [
    ("A1", "Analyst One", "F1"),
    ("A2", "Analyst Two", "F1"),
    ("A3", "Analyst Three", "F2"),
    ("A4", "Analyst Four", "F2"),
]
SECURITIES = ["S1", "S2"]


def _ts(day: date, hour: int = 12) -> datetime:
    return datetime.combine(day, datetime.min.time(), UTC).replace(hour=hour)


def _seed(engine: Engine) -> None:
    sessions = [
        {
            "exchange_id": "XNYS",
            "session_date": d,
            "open_ts": _ts(d, 14).replace(minute=30),
            "close_ts": _ts(d, 21),
        }
        for d in SESSION_DAYS
    ]
    reference.insert_reference(
        engine,
        firms=[
            {"firm_id": "F1", "name": "Fund One", "country": "US"},
            {"firm_id": "F2", "name": "Fund Two", "country": "US"},
        ],
        analysts=[{"analyst_id": aid, "name": name, "firm_id": fid} for aid, name, fid in ANALYSTS],
        exchanges=[{"exchange_id": "XNYS", "name": "NYSE", "country": "US"}],
        sectors=[{"sector_id": "SEC1", "name": "Sector One"}],
        securities=[{"security_id": sid, "name": f"Security {sid[-1]}"} for sid in SECURITIES],
        sessions=sessions,
        employment=[
            {"analyst_id": aid, "firm_id": fid, "from_date": date(2020, 1, 1), "to_date": None}
            for aid, _, fid in ANALYSTS
        ],
        coverage=[
            {
                "analyst_id": aid,
                "security_id": sid,
                "from_date": date(2020, 1, 1),
                "to_date": None,
                "is_lead": sid == "S1",
            }
            for aid, _, _ in ANALYSTS
            for sid in SECURITIES
        ],
        classifications=[
            {
                "security_id": sid,
                "from_date": date(2020, 1, 1),
                "to_date": None,
                "exchange_id": "XNYS",
                "currency": "USD",
                "sector_id": "SEC1",
                "region": "US",
                "cap_bucket": "large",
                "listing_date": date(2010, 1, 1),
                "delisting_date": None,
            }
            for sid in SECURITIES
        ],
        rating_scales=[
            {
                "scale_id": "F1:main",
                "firm_id": "F1",
                "effective_from": date(2020, 1, 1),
                "n_tiers": 5,
            },
            {
                "scale_id": "F2:main",
                "firm_id": "F2",
                "effective_from": date(2020, 1, 1),
                "n_tiers": 5,
            },
        ],
    )
    market.insert_market(
        engine,
        benchmark_defs=[{"benchmark_id": "SECTOR_SEC1", "kind": "sector"}],
        security_daily=[
            {
                "security_id": sid,
                "session_date": d,
                "open": 100.0,
                "high": 100.5,
                "low": 99.5,
                "close": 100.0,
                "adj_factor": 1.0,
                "cash_dividend": None,
                "tr_index": 100.0,
                "is_halted": False,
            }
            for sid in SECURITIES
            for d in SESSION_DAYS
        ],
        benchmark_daily=[
            {
                "benchmark_id": "SECTOR_SEC1",
                "session_date": d,
                "open_level": 100.0,
                "close_level": 100.0,
            }
            for d in SESSION_DAYS
        ],
    )

    rating_rows = []
    seq = 0
    for aid, _, fid in ANALYSTS:
        scale = f"{fid}:main"
        # initiation buy on S1 at session 0
        rating_rows.append(
            {
                "event_id": f"e{seq}",
                "analyst_id": aid,
                "firm_id": fid,
                "security_id": "S1",
                "announced_ts": _ts(SESSION_DAYS[0]),
                "effective_ts": _ts(SESSION_DAYS[0]),
                "knowledge_ts": _ts(SESSION_DAYS[0]),
                "action_type": "initiation",
                "native_rating": "Buy",
                "canonical_rating": "buy",
                "prior_canonical_rating": None,
                "scale_id": scale,
                "price_target": None,
                "pt_currency": None,
                "pt_horizon_months": None,
                "expiry_date": None,
                "supersedes_event_id": None,
                "is_correction": False,
            }
        )
        seq += 1
        # upgrade hold->buy on S2 at session 2
        rating_rows.append(
            {
                "event_id": f"e{seq}",
                "analyst_id": aid,
                "firm_id": fid,
                "security_id": "S2",
                "announced_ts": _ts(SESSION_DAYS[2]),
                "effective_ts": _ts(SESSION_DAYS[2]),
                "knowledge_ts": _ts(SESSION_DAYS[2]),
                "action_type": "upgrade",
                "native_rating": "Buy",
                "canonical_rating": "buy",
                "prior_canonical_rating": "hold",
                "scale_id": scale,
                "price_target": None,
                "pt_currency": None,
                "pt_horizon_months": None,
                "expiry_date": None,
                "supersedes_event_id": None,
                "is_correction": False,
            }
        )
        seq += 1
    # A1 also sets a price target on S1.
    rating_rows.append(
        {
            "event_id": f"e{seq}",
            "analyst_id": "A1",
            "firm_id": "F1",
            "security_id": "S1",
            "announced_ts": _ts(SESSION_DAYS[3]),
            "effective_ts": _ts(SESSION_DAYS[3]),
            "knowledge_ts": _ts(SESSION_DAYS[3]),
            "action_type": "pt_change",
            "native_rating": "Buy",
            "canonical_rating": "buy",
            "prior_canonical_rating": "buy",
            "scale_id": "F1:main",
            "price_target": 120.0,
            "pt_currency": "USD",
            "pt_horizon_months": 1,
            "expiry_date": None,
            "supersedes_event_id": None,
            "is_correction": False,
        }
    )
    events.insert_rating_events(engine, rating_rows)


@pytest.fixture()
def engine(tmp_path: Path) -> Engine:
    path = tmp_path / "callscore.db"
    eng = create_engine_for(path)
    bootstrap(eng)
    _seed(eng)
    methodology = Methodology(
        min_ratings_for_rank=1,
        min_peer_size=2,
        peer_group_dims=(),
        bootstrap_draws=50,
    )
    calculation.prepare_run(
        eng, methodology, run_id="run-1", as_of_date=AS_OF, knowledge_cutoff=CUTOFF
    )
    calculation.execute_run(eng, "run-1")
    calculation.prepare_run(
        eng,
        methodology,
        run_id="run-early",
        as_of_date=AS_OF_EARLY,
        knowledge_cutoff=calculation.default_knowledge_cutoff(AS_OF_EARLY),
    )
    calculation.execute_run(eng, "run-early")
    return eng


@pytest.fixture()
def client(engine: Engine) -> TestClient:
    app = FastAPI()
    app.include_router(analysts_router.router)
    app.include_router(leaderboard_router.router)
    app.state.engine = engine
    return TestClient(app)


# --------------------------------------------------------------------------- #
# Analysts list
# --------------------------------------------------------------------------- #


def test_list_analysts(client: TestClient) -> None:
    resp = client.get("/analysts", params={"run_id": "run-1"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 4
    assert body["limit"] == 50
    assert body["offset"] == 0
    assert body["provenance"]["run_id"] == "run-1"
    assert body["provenance"]["methodology_fingerprint"]
    ids = [i["analyst_id"] for i in body["items"]]
    assert ids == ["A1", "A2", "A3", "A4"]  # stable default order (rank null -> id)
    first = body["items"][0]
    assert first["name"] == "Analyst One"
    assert first["firm_name"] == "Fund One"
    assert first["peer_group"] == "all"
    assert first["n_obs"] >= 1
    assert "mean_contribution" in first["key_metrics"]
    assert "hit_rate" in first["key_metrics"]
    assert resp.headers["etag"].startswith('"')


def test_list_analysts_pagination_stable(client: TestClient) -> None:
    page1 = client.get("/analysts", params={"run_id": "run-1", "limit": 2, "offset": 0}).json()
    page2 = client.get("/analysts", params={"run_id": "run-1", "limit": 2, "offset": 2}).json()
    assert page1["total"] == 4 and page2["total"] == 4
    assert [i["analyst_id"] for i in page1["items"]] == ["A1", "A2"]
    assert [i["analyst_id"] for i in page2["items"]] == ["A3", "A4"]
    # Same ordering repeated -> identical pages (stable sort).
    again = client.get("/analysts", params={"run_id": "run-1", "limit": 2, "offset": 0}).json()
    assert again["items"] == page1["items"]


def test_list_analysts_filters(client: TestClient) -> None:
    by_name = client.get("/analysts", params={"run_id": "run-1", "q": "three"}).json()
    assert [i["analyst_id"] for i in by_name["items"]] == ["A3"]
    by_id = client.get("/analysts", params={"run_id": "run-1", "q": "A4"}).json()
    assert [i["analyst_id"] for i in by_id["items"]] == ["A4"]
    pg = client.get("/analysts", params={"run_id": "run-1", "peer_group": "all"}).json()
    assert pg["total"] == 4
    pg_none = client.get(
        "/analysts", params={"run_id": "run-1", "peer_group": "nonexistent"}
    ).json()
    assert pg_none["total"] == 0
    unranked = client.get("/analysts", params={"run_id": "run-1", "eligible": "unranked"}).json()
    all_items = client.get("/analysts", params={"run_id": "run-1"}).json()["items"]
    expected = sum(1 for i in all_items if not i["is_ranked"])
    assert unranked["total"] == expected
    ranked = client.get("/analysts", params={"run_id": "run-1", "eligible": "ranked"}).json()
    assert ranked["total"] == 4 - expected
    for item in ranked["items"]:
        assert item["is_ranked"] is True


def test_list_analysts_bad_params(client: TestClient) -> None:
    assert client.get("/analysts", params={"run_id": "nope"}).status_code == 404
    assert client.get("/analysts", params={"run_id": "run-1", "sort": "bogus"}).status_code == 422
    assert (
        client.get("/analysts", params={"run_id": "run-1", "order": "sideways"}).status_code == 422
    )


def test_etag_304(client: TestClient) -> None:
    first = client.get("/analysts", params={"run_id": "run-1"})
    etag = first.headers["etag"]
    second = client.get("/analysts", params={"run_id": "run-1"}, headers={"If-None-Match": etag})
    assert second.status_code == 304
    assert second.headers["etag"] == etag
    assert second.content == b""
    # Different run -> different ETag -> 200.
    other = client.get("/analysts", params={"run_id": "run-early"})
    assert other.status_code == 200
    assert other.headers["etag"] != etag


# --------------------------------------------------------------------------- #
# Profile + metrics
# --------------------------------------------------------------------------- #


def test_analyst_profile(client: TestClient) -> None:
    resp = client.get("/analysts/A1", params={"run_id": "run-1"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["analyst_id"] == "A1"
    assert body["name"] == "Analyst One"
    assert body["firm_name"] == "Fund One"
    assert len(body["employment_spells"]) == 1
    assert body["employment_spells"][0]["firm_id"] == "F1"
    assert body["employment_spells"][0]["to_date"] is None
    assert len(body["coverage_spells"]) == 2
    comp = body["composite"]
    assert comp is not None
    assert comp["peer_group"] == "all"
    assert comp["n_obs"] >= 1
    assert set(comp["family_scores"]) == {
        "return_skill",
        "timing",
        "price_target",
        "behavior",
        "consistency",
    }
    assert comp["shrinkage"]["method"] == "empirical_bayes"
    assert body["provenance"]["run_id"] == "run-1"
    # All catalog metrics are represented, including unavailable ones.
    codes = {m["metric_code"] for m in body["metrics"]}
    assert "mean_contribution" in codes
    assert "information_ratio" in codes
    unavailable = [m for m in body["metrics"] if m["unavailable_reason"]]
    assert unavailable, "expected at least one unavailable metric (63-session IR)"
    for m in body["metrics"]:
        assert "family" in m and "n_obs" in m


def test_analyst_profile_404s(client: TestClient) -> None:
    assert client.get("/analysts/NOPE", params={"run_id": "run-1"}).status_code == 404
    assert client.get("/analysts/A1", params={"run_id": "nope"}).status_code == 404


def test_metrics_drilldown_json_and_family(client: TestClient) -> None:
    resp = client.get("/analysts/A1/metrics", params={"run_id": "run-1"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["analyst_id"] == "A1"
    assert len(body["items"]) > 10
    fam = client.get(
        "/analysts/A1/metrics", params={"run_id": "run-1", "family": "return_skill"}
    ).json()
    assert fam["items"]
    assert all(i["family"] == "return_skill" for i in fam["items"])
    assert (
        client.get(
            "/analysts/A1/metrics", params={"run_id": "run-1", "family": "bogus"}
        ).status_code
        == 422
    )
    assert client.get("/analysts/NOPE/metrics", params={"run_id": "run-1"}).status_code == 404


def test_metrics_csv(client: TestClient) -> None:
    resp = client.get("/analysts/A1/metrics", params={"run_id": "run-1", "format": "csv"})
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/csv")
    assert "metrics-A1.csv" in resp.headers["content-disposition"]
    rows = list(csv.DictReader(io.StringIO(resp.text)))
    json_rows = client.get("/analysts/A1/metrics", params={"run_id": "run-1"}).json()["items"]
    assert len(rows) == len(json_rows)
    assert rows[0]["metric_code"]
    # Spot-check a value round-trips.
    by_code = {r["metric_code"]: r for r in rows}
    assert "mean_contribution" in by_code


# --------------------------------------------------------------------------- #
# Ratings list + detail
# --------------------------------------------------------------------------- #


def _first_interval_id(client: TestClient) -> str:
    body = client.get("/analysts/A1/ratings", params={"run_id": "run-1"}).json()
    assert body["total"] >= 1
    return str(body["items"][0]["interval_id"])


def test_ratings_list(client: TestClient) -> None:
    resp = client.get("/analysts/A1/ratings", params={"run_id": "run-1"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] >= 2  # initiation on S1, upgrade on S2, pt_change on S1
    item = body["items"][0]
    assert item["interval_id"]
    assert item["security_id"] in SECURITIES
    assert item["is_investable"] is True
    assert "excess_return" in item["return_summary"]
    assert item["exposures"], "expected at least one exposure leg"


def test_ratings_filters_and_pagination(client: TestClient) -> None:
    initiation = client.get(
        "/analysts/A1/ratings",
        params={"run_id": "run-1", "action": "initiation"},
    ).json()
    assert initiation["total"] == 1
    assert initiation["items"][0]["action_type"] == "initiation"
    on_s2 = client.get("/analysts/A1/ratings", params={"run_id": "run-1", "security": "S2"}).json()
    assert on_s2["total"] == 1
    assert on_s2["items"][0]["security_id"] == "S2"
    early = client.get(
        "/analysts/A1/ratings",
        params={"run_id": "run-1", "from": "2025-01-01", "to": "2025-01-10"},
    ).json()
    assert early["total"] >= 1
    page = client.get(
        "/analysts/A1/ratings", params={"run_id": "run-1", "limit": 1, "offset": 1}
    ).json()
    assert page["total"] == body_total(client)
    assert len(page["items"]) == 1
    assert (
        client.get(
            "/analysts/A1/ratings", params={"run_id": "run-1", "action": "bogus"}
        ).status_code
        == 422
    )


def body_total(client: TestClient) -> int:
    return int(client.get("/analysts/A1/ratings", params={"run_id": "run-1"}).json()["total"])


def test_rating_detail(client: TestClient) -> None:
    interval_id = _first_interval_id(client)
    resp = client.get(f"/analysts/A1/ratings/{interval_id}", params={"run_id": "run-1"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["interval_id"] == interval_id
    assert body["analyst_id"] == "A1"
    assert body["event"]["event_id"] == body["event_id"]
    assert body["exposures"]
    assert body["returns"]
    assert body["contributions"]
    assert body["event_window"] is not None
    explanation = body["explanation"]
    assert "weight" in explanation
    assert ("excess" in explanation) or ("unavailable" in explanation)
    # Wrong analyst / wrong run -> 404.
    assert (
        client.get(f"/analysts/A2/ratings/{interval_id}", params={"run_id": "run-1"}).status_code
        == 404
    )
    assert (
        client.get(
            f"/analysts/A1/ratings/{interval_id}", params={"run_id": "run-early"}
        ).status_code
        == 200  # same deterministic interval_id exists in the early run too
    )


# --------------------------------------------------------------------------- #
# Portfolio
# --------------------------------------------------------------------------- #


def test_portfolio(client: TestClient) -> None:
    resp = client.get("/analysts/A1/portfolio", params={"run_id": "run-1"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["total_points"] >= 5
    assert body["downsampled"] is False
    dates = [p["session_date"] for p in body["points"]]
    assert dates == sorted(dates)
    assert all("nav" in p and "benchmark_nav" in p and "drawdown" in p for p in body["points"])
    positions = body["positions"]
    assert positions["session_date"] == dates[-1]
    # ``portfolio_positions`` is a published stage output, so the snapshot is
    # populated by the real run (no manual inserts).
    assert positions["rows"], "expected portfolio_position rows for the latest session"
    for row in positions["rows"]:
        assert row["session_date"] == dates[-1]
        assert row["analyst_id"] == "A1"
        assert row["security_id"] in SECURITIES
        assert -1.0 <= row["weight"] <= 1.0
        # ``notional`` is a Decimal column -> serialized as a numeric string.
        assert row["notional"] is None or float(row["notional"]) is not None
    # Date filter narrows the series.
    narrow = client.get(
        "/analysts/A1/portfolio",
        params={"run_id": "run-1", "from": "2025-01-15", "to": "2025-01-17"},
    ).json()
    assert 0 < narrow["total_points"] < body["total_points"]
    assert client.get("/analysts/NOPE/portfolio", params={"run_id": "run-1"}).status_code == 404


# --------------------------------------------------------------------------- #
# Leaderboard
# --------------------------------------------------------------------------- #


def test_leaderboard(client: TestClient) -> None:
    resp = client.get("/leaderboard", params={"run_id": "run-1"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 4
    assert body["provenance"]["run_id"] == "run-1"
    for item in body["items"]:
        assert item["analyst_id"]
        assert item["peer_group"] == "all"
        assert item["n_investable"] >= 1
        assert set(item["family_scores"]) == {
            "return_skill",
            "timing",
            "price_target",
            "behavior",
            "consistency",
        }
        assert "mean_contribution" in item["key_metrics"]
        assert "information_ratio" in item["key_metrics"]
        assert "target_hit_rate" in item["key_metrics"]


def test_leaderboard_sort_and_filters(client: TestClient) -> None:
    by_metric = client.get(
        "/leaderboard", params={"run_id": "run-1", "sort": "hit_rate", "order": "desc"}
    ).json()
    assert by_metric["total"] == 4
    values = [i["key_metrics"].get("hit_rate", {}).get("value") for i in by_metric["items"]]
    present = [v for v in values if v is not None]
    assert present == sorted(present, reverse=True)

    filtered = client.get(
        "/leaderboard", params={"run_id": "run-1", "metric": "mean_contribution"}
    ).json()
    assert filtered["total"] >= 1
    assert all("mean_contribution" in i["key_metrics"] for i in filtered["items"])

    min_obs = client.get("/leaderboard", params={"run_id": "run-1", "min_obs": 2}).json()
    assert all(i["n_investable"] >= 2 for i in min_obs["items"])
    min_obs_high = client.get("/leaderboard", params={"run_id": "run-1", "min_obs": 999}).json()
    assert min_obs_high["total"] == 0

    pg = client.get("/leaderboard", params={"run_id": "run-1", "peer_group": "bogus"}).json()
    assert pg["total"] == 0

    page = client.get("/leaderboard", params={"run_id": "run-1", "limit": 3, "offset": 3}).json()
    assert page["total"] == 4
    assert len(page["items"]) == 1

    assert (
        client.get("/leaderboard", params={"run_id": "run-1", "metric": "bogus"}).status_code == 422
    )
    assert (
        client.get("/leaderboard", params={"run_id": "run-1", "sort": "bogus"}).status_code == 422
    )
    assert client.get("/leaderboard", params={"run_id": "nope"}).status_code == 404


def test_run_isolation(client: TestClient) -> None:
    """Each run sees only its own derived rows (PIT scoping)."""
    early = client.get("/analysts/A1/ratings", params={"run_id": "run-early"}).json()
    late = client.get("/analysts/A1/ratings", params={"run_id": "run-1"}).json()
    assert early["provenance"]["run_id"] == "run-early"
    # The early run's intervals exit at the earlier as_of date.
    early_exit = early["items"][0]["exit_session"]
    late_exit = late["items"][0]["exit_session"]
    assert early_exit <= AS_OF_EARLY.isoformat()
    assert late_exit == AS_OF.isoformat()
    # Leaderboard rows carry the requesting run's provenance only.
    board = client.get("/leaderboard", params={"run_id": "run-early"}).json()
    assert board["provenance"]["run_id"] == "run-early"
