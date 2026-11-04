from typing import Any

import pytest
from pydantic import ValidationError

from callscore_calc import CalcRequest, CompareRequest, Query, SpecError, fingerprint
from callscore_calc.spec.canonical import canonical_json, upgrade


def request(**query: Any) -> dict[str, Any]:
    return {"dataset": "pos", "query": query}


def test_fingerprint_ignores_key_order_and_formula_text() -> None:
    a = CalcRequest.model_validate(request(filter="qty > 1", group_by=["desk"]))
    b = CalcRequest.model_validate(
        {
            "query": {
                "group_by": ["desk"],
                "filter": {
                    "kind": "cmp",
                    "op": "gt",
                    "left": {"kind": "col", "name": "qty"},
                    "right": {"kind": "lit", "type": "int", "value": 1},
                },
            },
            "dataset": {"id": "pos"},
        }
    )
    assert fingerprint(a) == fingerprint(b)


def test_decimal_formatting_is_canonical() -> None:
    assert canonical_json(Query.model_validate({"filter": "p > 1.50"})) == canonical_json(
        Query.model_validate({"filter": "p > 1.5"})
    )
    assert fingerprint(CalcRequest.model_validate(request(filter="p > 1.50"))) == fingerprint(
        CalcRequest.model_validate(request(filter="p > 1.5"))
    )


def test_different_requests_differ() -> None:
    a = CalcRequest.model_validate(request(filter="qty > 1"))
    b = CalcRequest.model_validate(request(filter="qty > 2"))
    assert fingerprint(a) != fingerprint(b)


def test_canonical_dump_round_trips_with_kinds() -> None:
    req = CalcRequest.model_validate(
        {
            "dataset": "pos",
            "extensions": {"whatif": {"steps": [{"kind": "shock", "column": "p", "value": 1}]}},
            "query": {"derive": [{"name": "n", "expr": "p * q"}]},
        }
    )
    dumped = req.model_dump(mode="json", exclude_defaults=True)
    assert dumped["query"]["derive"][0]["expr"]["kind"] == "binary"
    assert CalcRequest.model_validate(dumped) == req


@pytest.mark.parametrize(
    "query",
    [
        {"post": [{"name": "x", "expr": "1"}]},
        {"rollup": True, "measures": [{"name": "n", "fn": "count_rows"}]},
        {"group_by": ["a"], "select": ["a"]},
        {"measures": [{"name": "n", "fn": "sum"}]},
        {"measures": [{"name": "n", "fn": "count_rows", "of": "a"}]},
        {"measures": [{"name": "n", "fn": "wavg", "of": "a"}]},
        {"derive": [{"name": "x", "expr": "a", "otherwise": "b"}]},
    ],
)
def test_invalid_query_shapes(query: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        Query.model_validate(query)


def test_upgrade_rejects_unknown_versions() -> None:
    assert upgrade({"spec_version": 1})["spec_version"] == 2
    assert upgrade({"spec_version": 2}) == {"spec_version": 2}
    with pytest.raises(SpecError, match="unsupported spec_version"):
        upgrade({"spec_version": 99})


SHOCK = {"kind": "shock", "column": "p", "op": "pct", "value": 5}


def test_v1_what_if_moves_into_the_whatif_extension() -> None:
    v1 = {
        "dataset": "pos",
        "scenario": "s1",
        "what_if": [SHOCK],
        "options": {"strict_edits": False, "audit": True},
    }
    for raw in (v1, {**v1, "spec_version": 1}):
        req = CalcRequest.model_validate(upgrade(raw))
        assert req.spec_version == 2 and req.options.audit
        assert req.extensions == {
            "whatif": {"scenario": "s1", "steps": [SHOCK], "strict_edits": False}
        }
    # Without what-if keys a request without spec_version is already current.
    assert upgrade({"dataset": "pos"}) == {"dataset": "pos"}
    plain = CalcRequest.model_validate(upgrade({"dataset": "pos", "options": {"audit": True}}))
    assert plain.extensions == {}


def test_v1_compare_sides_move_into_extensions() -> None:
    raw = {
        "dataset": "pos",
        "what_if": [SHOCK],
        "base": {"id": "s0", "version": 2},
        "base_what_if": [SHOCK],
    }
    req = CompareRequest.model_validate(upgrade(raw))
    assert req.extensions == {"whatif": {"steps": [SHOCK]}}
    assert req.base.extensions == {
        "whatif": {"scenario": {"id": "s0", "version": 2}, "steps": [SHOCK]}
    }
    unchanged = CompareRequest.model_validate(upgrade({"dataset": "pos", "scenario": "s1"}))
    assert unchanged.base.extensions == {} and unchanged.base.version is None


def test_unknown_fields_are_rejected() -> None:
    with pytest.raises(ValidationError):
        CalcRequest.model_validate({"dataset": "pos", "querry": {}})
