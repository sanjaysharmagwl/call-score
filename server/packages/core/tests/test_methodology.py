import json
from datetime import UTC, date, datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from callscore_core.contracts import RunContext, RunProvenance, stage_fingerprint
from callscore_core.enums import CanonicalRating, Stage
from callscore_core.methodology import Methodology


def test_defaults_match_build_plan() -> None:
    methodology = Methodology()
    assert methodology.entry_convention == "next_open"
    assert methodology.benchmark_mode == "sector"
    assert methodology.fixed_horizons_days == (63, 126, 252)
    assert methodology.position_weights.for_rating(CanonicalRating.BUY) == 0.5
    assert methodology.position_weights.for_rating(CanonicalRating.HOLD) == 0.0
    assert methodology.position_weights.for_rating(CanonicalRating.RESTRICTED) is None
    assert methodology.min_ratings_for_rank == 20


def test_nested_values_are_immutable() -> None:
    methodology = Methodology()
    with pytest.raises(ValidationError):
        methodology.position_weights.buy = 0.7
    with pytest.raises(ValidationError):
        methodology.min_ratings_for_rank = 1
    assert isinstance(methodology.peer_group_dims, tuple)


def test_canonical_round_trip_and_order_independence() -> None:
    first = Methodology.model_validate(
        {"fixed_horizons_days": [252, 63, 126], "peer_group_dims": ["sector", "region"]}
    )
    second = Methodology.model_validate(
        {"peer_group_dims": ["region", "sector"], "fixed_horizons_days": [126, 252, 63]}
    )
    assert first.fingerprint() == second.fingerprint()
    assert first == Methodology.model_validate_json(first.canonical_json())
    assert json.loads(first.canonical_json())["contract_version"] == 1
    assert first.fingerprint() != Methodology().fingerprint()


@pytest.mark.parametrize(
    "changes",
    [
        {"entry_convention": "unknown"},
        {"expiry_policy": "decay", "decay_days": 0},
        {"fixed_horizons_days": [0]},
        {"fixed_horizons_days": [63, 63]},
        {"fixed_horizons_days": [True]},
        {"min_ratings_for_rank": 0},
        {"min_ratings_for_rank": True},
        {"transaction_cost_bps": -1},
        {"transaction_cost_bps": float("inf")},
        {"position_weights": {"buy": -0.5}},
        {"position_weights": {"hold": 0.1}},
        {
            "family_weights": {
                "return_skill": 0,
                "timing": 0,
                "price_target": 0,
                "behavior": 0,
                "consistency": 0,
            }
        },
        {"peer_group_dims": ["sector", "sector"]},
        {"benchmark_mode": "custom", "benchmark_id": None},
        {"benchmark_mode": "market", "benchmark_id": "   "},
        {"benchmark_id": "IDX BROAD"},
        {"reporting_currency": "usd"},
        {"invented_option": True},
        {"confidence_level": "0.95"},
        {"transaction_cost_bps": "10"},
        {"bootstrap_draws": 10**12},
        {"decay_days": 10**6},
        {"fixed_horizons_days": [10**9]},
        {"fixed_horizons_days": []},
        {"fixed_horizons_days": list(range(1, 12))},
        {"min_peer_size": 10**6},
    ],
)
def test_invalid_methodologies_are_rejected(changes: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        Methodology.model_validate(changes)


def test_negative_zero_does_not_change_fingerprint() -> None:
    signed = Methodology(position_weights={"hold": -0.0}, transaction_cost_bps=-0.0)
    assert signed.fingerprint() == Methodology().fingerprint()


def test_integer_numbers_are_accepted_for_float_fields() -> None:
    methodology = Methodology.model_validate_json('{"transaction_cost_bps": 10}')
    assert methodology.transaction_cost_bps == 10
    assert isinstance(methodology.transaction_cost_bps, float)
    assert methodology.fingerprint() == Methodology(transaction_cost_bps=10.0).fingerprint()


def test_validated_overrides_do_not_use_unvalidated_model_copy() -> None:
    baseline = Methodology()
    scenario = baseline.with_overrides({"min_ratings_for_rank": 30})
    assert scenario.min_ratings_for_rank == 30
    assert baseline.min_ratings_for_rank == 20
    with pytest.raises(ValidationError):
        baseline.with_overrides({"min_ratings_for_rank": -1})
    changed_weights = baseline.with_overrides({"position_weights": {"buy": 0.75}})
    assert changed_weights.position_weights.buy == 0.75
    assert changed_weights.position_weights.strong_buy == 1


def context() -> RunContext:
    return RunContext(
        as_of_date=date(2025, 12, 31),
        knowledge_cutoff=datetime(2026, 1, 1, tzinfo=UTC),
        source_revision="source-1",
        source_fingerprint="a" * 64,
        universe_fingerprint="b" * 64,
        algorithm_version="1.0.0",
    )


def test_knowledge_time_is_aware_and_canonical_utc() -> None:
    with pytest.raises(ValidationError):
        RunContext.model_validate(
            {
                **context().model_dump(),
                "knowledge_cutoff": datetime(2026, 1, 1),
            }
        )
    utc = context()
    offset = RunContext.model_validate(
        {
            **utc.model_dump(),
            "knowledge_cutoff": utc.knowledge_cutoff.astimezone(timezone(timedelta(hours=5))),
        }
    )
    assert utc.canonical_json() == offset.canonical_json()


def test_cache_identity_includes_snapshot_cutoff_and_algorithm() -> None:
    base = context()
    methodology = Methodology()
    key = stage_fingerprint(Stage.INTERVALS, methodology, base, ())
    for field, value in [
        ("source_fingerprint", "c" * 64),
        ("universe_fingerprint", "d" * 64),
        ("knowledge_cutoff", datetime(2026, 1, 2, tzinfo=UTC)),
        ("as_of_date", date(2025, 12, 30)),
        ("algorithm_version", "2.0.0"),
    ]:
        altered = RunContext.model_validate({**base.model_dump(), field: value})
        assert stage_fingerprint(Stage.INTERVALS, methodology, altered, ()) != key
    assert stage_fingerprint(Stage.INTERVALS, methodology, base, ("e" * 64,)) != key


def test_weight_change_preserves_raw_legs_not_contributions() -> None:
    base = Methodology()
    changed = base.with_overrides({"position_weights": {"buy": 0.75}})
    for stage in (Stage.INTERVALS, Stage.RETURNS):
        assert stage_fingerprint(stage, base, context(), ()) == stage_fingerprint(
            stage, changed, context(), ()
        )
    assert stage_fingerprint(Stage.EXPOSURES, base, context(), ()) != stage_fingerprint(
        Stage.EXPOSURES, changed, context(), ()
    )


def test_statistics_only_change_preserves_prior_stage_keys() -> None:
    base = Methodology()
    changed = base.with_overrides({"min_ratings_for_rank": 30})
    for stage in (Stage.INTERVALS, Stage.RETURNS, Stage.EXPOSURES, Stage.METRICS):
        assert stage_fingerprint(stage, base, context(), ()) == stage_fingerprint(
            stage, changed, context(), ()
        )
    assert stage_fingerprint(Stage.STATISTICS, base, context(), ()) != stage_fingerprint(
        Stage.STATISTICS, changed, context(), ()
    )


def test_provenance_requires_explicit_ids_and_hashes() -> None:
    with pytest.raises(ValidationError):
        RunProvenance(run_id="", methodology_version_id="baseline", methodology_fingerprint="bad")
