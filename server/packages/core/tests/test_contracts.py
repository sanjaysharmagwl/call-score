from datetime import UTC, date, datetime

import pytest
from pydantic import ValidationError

from callscore_core.contracts import (
    STAGE_METHODOLOGY_FIELDS,
    STAGE_ORDER,
    MetricValue,
    RunContext,
    stage_fingerprint,
)
from callscore_core.enums import Stage
from callscore_core.frames import (
    CONTRIBUTION_FRAME,
    INTERVAL_FRAME,
    RETURN_FRAME,
    ColumnContract,
    FrameContract,
)
from callscore_core.methodology import Methodology
from callscore_core.metrics import METRIC_DEFINITIONS, metric_definition


def test_frame_contracts_separate_boundaries_returns_and_weights() -> None:
    assert "position_weight" not in {column.name for column in INTERVAL_FRAME.columns}
    assert "contribution" not in {column.name for column in RETURN_FRAME.columns}
    assert "contribution" in {column.name for column in CONTRIBUTION_FRAME.columns}
    for frame in (INTERVAL_FRAME, RETURN_FRAME, CONTRIBUTION_FRAME):
        assert frame == FrameContract.model_validate_json(frame.canonical_json())


_ID = ColumnContract(name="id", dtype="String")
_TS = ColumnContract(name="ts", dtype="Date")


@pytest.mark.parametrize(
    ("columns", "key", "sort"),
    [
        ((ColumnContract(name="id", dtype="String", nullable=True),), ("id",), ("id",)),
        ((_ID,), ("missing",), ("missing",)),
        ((_ID, _ID), ("id",), ("id",)),
        ((_ID,), ("id", "id"), ("id",)),
        ((_ID,), ("id",), ("id", "id")),
        ((_ID,), ("id",), ()),
        ((_ID, _TS), ("id",), ("ts",)),
    ],
)
def test_invalid_frame_keys_are_rejected(
    columns: tuple[ColumnContract, ...], key: tuple[str, ...], sort: tuple[str, ...]
) -> None:
    with pytest.raises(ValidationError):
        FrameContract(name="bad", columns=columns, primary_key=key, sort_by=sort)


@pytest.mark.parametrize("name", ["bad id", "bad\nid", " id", "id ", ""])
def test_identifiers_reject_whitespace(name: str) -> None:
    with pytest.raises(ValidationError):
        ColumnContract(name=name, dtype="String")


def test_upstream_fingerprints_are_validated() -> None:
    context = RunContext(
        as_of_date=date(2025, 12, 31),
        knowledge_cutoff=datetime(2026, 1, 1, tzinfo=UTC),
        source_revision="r1",
        source_fingerprint="a" * 64,
        universe_fingerprint="b" * 64,
        algorithm_version="1",
    )
    with pytest.raises(ValidationError):
        stage_fingerprint(Stage.RETURNS, Methodology(), context, ("not-a-hash",))


def test_catalog_codes_are_unique_and_lookup_works() -> None:
    assert len({metric.code for metric in METRIC_DEFINITIONS}) == len(METRIC_DEFINITIONS)
    assert metric_definition("target_bias").direction == "descriptive"
    with pytest.raises(KeyError):
        metric_definition("invented")


@pytest.mark.parametrize(
    "changes",
    [
        {"value": float("nan")},
        {"n_obs": -1},
        {"ci_low": 0.1},
        {"ci_low": 0.2, "ci_high": 0.1},
        {"peer_percentile": 101},
        {"value": None},
        {"value": None, "unavailable_reason": "no sample", "is_significant": False},
        {"value": None, "unavailable_reason": "no sample", "ci_low": 0.0, "ci_high": 1.0},
        {"value": None, "unavailable_reason": "no sample", "std_err": 0.1},
        {"value": None, "unavailable_reason": "   "},
        {"unavailable_reason": "stale reason on available value"},
    ],
)
def test_invalid_metric_objects_are_rejected(changes: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        MetricValue.model_validate({"value": 0.1, "n_obs": 3, **changes})


def test_missing_significance_is_not_reported_as_insignificant() -> None:
    value = MetricValue(value=None, n_obs=0, unavailable_reason="no sample")
    assert value.is_significant is None


def test_cache_stage_dependencies() -> None:
    context = RunContext(
        as_of_date=date(2025, 12, 31),
        knowledge_cutoff=datetime(2026, 1, 1, tzinfo=UTC),
        source_revision="r1",
        source_fingerprint="a" * 64,
        universe_fingerprint="b" * 64,
        algorithm_version="1",
    )
    base = Methodology()
    for overrides, first_changed in [
        ({"expiry_policy": "carry_forward"}, Stage.INTERVALS),
        ({"benchmark_mode": "market"}, Stage.RETURNS),
        ({"return_basis": "price"}, Stage.RETURNS),
        ({"scoring_mode": "relative_to_consensus"}, Stage.EXPOSURES),
        ({"transaction_cost_bps": 10}, Stage.METRICS),
        ({"bootstrap_seed": 1}, Stage.STATISTICS),
    ]:
        changed = base.with_overrides(overrides)
        assert stage_fingerprint(first_changed, base, context, ()) != stage_fingerprint(
            first_changed, changed, context, ()
        )
        for earlier in STAGE_ORDER[: STAGE_ORDER.index(first_changed)]:
            assert stage_fingerprint(earlier, base, context, ()) == stage_fingerprint(
                earlier, changed, context, ()
            )


def test_every_methodology_field_affects_some_stage() -> None:
    covered = {name for fields in STAGE_METHODOLOGY_FIELDS.values() for name in fields}
    assert covered == set(Methodology.model_fields) - {"contract_version"}
    assert set(STAGE_ORDER) == set(Stage) == set(STAGE_METHODOLOGY_FIELDS)
