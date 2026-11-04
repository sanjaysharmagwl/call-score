import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from callscore_calc import CalcContext, CalcEngine
from callscore_calc.integrations.fastapi import create_router


@pytest.fixture
def client(engine: CalcEngine) -> TestClient:
    def resolve(request: Request) -> CalcContext:
        user = request.headers.get("X-User")
        rows = "desk == 'rates'" if user == "rates-trader" else None
        return CalcContext(principal=user, row_filter=rows)

    app = FastAPI()
    app.include_router(
        create_router(engine, prefix="/calc", context_resolver=resolve, max_body_bytes=20_000)
    )
    return TestClient(app)


def test_scenario_lifecycle(client: TestClient) -> None:
    created = client.post(
        "/calc/scenarios", json={"dataset": "pos", "name": "s"}, headers={"X-User": "ana"}
    )
    assert created.status_code == 201
    scenario = created.json()
    assert scenario["owner"] == "ana"
    url = f"/calc/scenarios/{scenario['id']}"
    step = {"kind": "shock", "column": "qty", "op": "add", "value": 1}
    appended = client.post(
        f"{url}/steps",
        json={"steps": [step], "expected_version": 0},
        headers={"Idempotency-Key": "k1"},
    )
    assert appended.json()["version"] == 1
    retried = client.post(
        f"{url}/steps",
        json={"steps": [step], "expected_version": 0},
        headers={"Idempotency-Key": "k1"},
    )
    assert retried.json()["version"] == 1
    stale = client.post(f"{url}/steps", json={"steps": [step], "expected_version": 0})
    assert stale.status_code == 409 and stale.json()["detail"]["code"] == "version_conflict"
    assert len(client.get(f"{url}/log").json()) == 1
    assert client.get(f"{url}/verify").json()["intact"] is True
    fork = client.post(f"{url}/fork", json={"name": "copy"})
    assert fork.status_code == 201 and fork.json()["version"] == 1
    run = client.post(
        "/calc/query",
        json={"dataset": "pos", "scenario": scenario["id"], "query": {"filter": "id == 1"}},
    )
    assert run.json()["rows"][0]["qty"] == 11
    assert client.delete(url).status_code == 204
    assert client.get(url).status_code == 404
    assert [s["name"] for s in client.get("/calc/scenarios", params={"dataset": "pos"}).json()] == [
        "copy"
    ]


def test_aggrid_rows_and_edit(client: TestClient) -> None:
    scenario = client.post("/calc/scenarios", json={"dataset": "pos", "name": "grid"}).json()
    edit = client.post(
        "/calc/aggrid/edit",
        json={
            "scenario": scenario["id"],
            "expected_version": 0,
            "edit": {"colId": "qty", "newValue": "99", "data": {"id": 1}},
        },
    )
    assert edit.status_code == 200 and edit.json()["version"] == 1
    response = client.post(
        "/calc/aggrid/rows",
        json={
            "dataset": "pos",
            "scenario": scenario["id"],
            "request": {
                "startRow": 0,
                "endRow": 50,
                "rowGroupCols": [{"id": "desk", "field": "desk"}],
                "valueCols": [{"id": "qty", "field": "qty", "aggFunc": "sum"}],
                "groupKeys": [],
            },
        },
    )
    data = response.json()
    assert data["rowCount"] == 4
    assert next(r for r in data["rowData"] if r["desk"] == "rates")["qty"] == 119


def test_compare_and_distinct_with_what_if(client: TestClient) -> None:
    shock = {"kind": "shock", "column": "qty", "op": "mul", "value": 2}
    compared = client.post(
        "/calc/compare",
        json={
            "dataset": "pos",
            "extensions": {"whatif": {"steps": [shock]}},
            "query": {"measures": [{"name": "q", "fn": "sum", "of": "qty"}]},
        },
    ).json()
    assert compared["rows"] == [{"q": 420, "q__base": 210, "q__delta": 210, "q__pct": 100.0}]
    legacy = {"dataset": "pos", "column": "qty", "what_if": [shock], "filter": "id < 3"}
    current = {
        "dataset": "pos",
        "column": "qty",
        "extensions": {"whatif": {"steps": [shock]}},
        "filter": "id < 3",
    }
    for body in (legacy, current):
        assert client.post("/calc/distinct", json=body).json() == {"values": [20, 40]}
