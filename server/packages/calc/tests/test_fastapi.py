import io

import polars as pl
import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from callscore_calc import CalcContext, CalcEngine
from callscore_calc.integrations.fastapi import ARROW_STREAM, create_router


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


def test_query_json(client: TestClient) -> None:
    body = {
        "dataset": "pos",
        "query": {"group_by": ["sector"], "measures": [{"name": "q", "fn": "sum", "of": "qty"}]},
    }
    response = client.post("/calc/query", json=body)
    assert response.status_code == 200
    data = response.json()
    assert data["rows"] == [
        {"sector": "Energy", "q": 40},
        {"sector": "Fin", "q": 80},
        {"sector": "Tech", "q": 90},
    ]
    assert data["meta"]["total_rows"] == 3
    assert response.headers["X-Calc-Fingerprint"] == data["meta"]["fingerprint"]


def test_query_arrow(client: TestClient) -> None:
    response = client.post(
        "/calc/query",
        json={"dataset": "pos", "query": {"select": ["id", "price"]}},
        headers={"Accept": ARROW_STREAM},
    )
    assert response.headers["content-type"] == ARROW_STREAM
    frame = pl.read_ipc_stream(io.BytesIO(response.content))
    assert frame.schema["price"] == pl.Decimal(38, 2)
    assert frame.height == 6


def test_context_applies_row_filter(client: TestClient) -> None:
    response = client.post(
        "/calc/query", json={"dataset": "pos"}, headers={"X-User": "rates-trader"}
    )
    assert [r["id"] for r in response.json()["rows"]] == [1, 2]


@pytest.mark.parametrize(
    ("body", "status", "code"),
    [
        ({"dataset": "nope"}, 404, "dataset_not_found"),
        ({"dataset": "pos", "query": {"filter": "qty >"}}, 422, "formula_syntax"),
        ({"dataset": "pos", "query": {"filter": "nope > 1"}}, 422, "unknown_column"),
        ({"dataset": "pos", "querry": {}}, 422, "invalid_request"),
        ({"dataset": "pos", "query": {"page": {"limit": 10**9}}}, 413, "limit_exceeded"),
    ],
)
def test_errors(client: TestClient, body: dict[str, object], status: int, code: str) -> None:
    response = client.post("/calc/query", json=body)
    assert response.status_code == status
    assert response.json()["detail"]["code"] == code


def test_body_limit(client: TestClient) -> None:
    body = {"dataset": "pos", "query": {"filter": " or ".join(["qty > 1"] * 3000)}}
    response = client.post("/calc/query", json=body)
    assert response.status_code == 413


def test_schema_distinct_and_explain(client: TestClient) -> None:
    schema = client.get("/calc/datasets/pos/schema").json()
    assert schema["key_columns"] == ["id"]
    assert {c["name"]: c["type"] for c in schema["columns"]}["price"] == "decimal(2)"
    assert [d["dataset"] for d in client.get("/calc/datasets").json()] == ["pos"]
    values = client.post("/calc/distinct", json={"dataset": "pos", "column": "desk"}).json()
    assert values == {"values": ["credit", "equity", "rates", None]}
    explained = client.post(
        "/calc/explain", json={"dataset": "pos", "query": {"filter": "qty > 1"}}
    ).json()
    assert "plan" in explained and len(explained["fingerprint"]) == 64
    compared = client.post(
        "/calc/compare",
        json={"dataset": "pos", "query": {"measures": [{"name": "q", "fn": "sum", "of": "qty"}]}},
    ).json()
    assert compared["rows"] == [{"q": 210, "q__base": 210, "q__delta": 0, "q__pct": 0.0}]


def test_core_router_has_no_plugin_routes(client: TestClient) -> None:
    assert client.get("/calc/scenarios").status_code == 404
    assert client.get("/calc/operations").json() == []
    unknown = client.post("/calc/operations/nope", json={})
    assert unknown.status_code == 400 and unknown.json()["detail"]["code"] == "unknown_operation"
