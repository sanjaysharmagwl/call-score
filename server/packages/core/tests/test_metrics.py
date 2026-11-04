from datetime import UTC, date, datetime

import pytest
from pydantic import ValidationError

from callscore_core.contracts import RunContext, stage_fingerprint
from callscore_core.enums import MetricFamily, Stage
from callscore_core.frames import (
    FRAME_CONTRACTS,
    HORIZON_TO_EXIT,
    INTERVAL_FRAME,
    RETURN_FRAME,
    horizon_label,
)
from callscore_core.methodology import FamilyWeights, Methodology
from callscore_core.metrics import (
    COMPOSITE_SPEC,
    METRIC_CATALOG_FINGERPRINT,
    METRIC_DEFINITIONS,
    CompositeSpec,
    MetricDefinition,
    catalog_fingerprint,
    composite_metrics,
)

REQUIRED_CODES = {
    "annualized_alpha",
    "information_ratio",
    "target_hit_rate",
    "revision_drift_20d",
    "independence",
    "buy_fraction",
    "optimism_vs_peers",
    "ratings_per_year",
    "positive_quarter_fraction",
    "quarterly_contribution_std",
    "sector_dispersion",
    "regime_spread",
}


def test_catalog_covers_every_required_measure() -> None:
    assert {metric.code for metric in METRIC_DEFINITIONS} >= REQUIRED_CODES


def test_every_family_with_weight_has_a_composite_metric() -> None:
    for family in MetricFamily:
        assert composite_metrics(family), family


def test_style_measures_are_shown_but_never_scored() -> None:
    for code in (
        "independence",
        "buy_fraction",
        "optimism_vs_peers",
        "ratings_per_year",
        "signed_car",
        "momentum_chasing",
        "target_bias",
    ):
        metric = next(m for m in METRIC_DEFINITIONS if m.code == code)
        assert metric.direction == "descriptive"
        assert not metric.in_composite


def test_alpha_is_not_double_counted_with_information_ratio() -> None:
    by_code = {m.code: m for m in METRIC_DEFINITIONS}
    assert by_code["information_ratio"].in_composite
    assert not by_code["annualized_alpha"].in_composite
    assert by_code["annualized_alpha"].inference == "quarter_block_bootstrap"


def test_required_inputs_reference_declared_frames() -> None:
    for metric in METRIC_DEFINITIONS:
        assert set(metric.required_inputs) <= FRAME_CONTRACTS.keys(), metric.code


def _metric(**changes: object) -> MetricDefinition:
    base = METRIC_DEFINITIONS[0].model_dump()
    return MetricDefinition.model_validate({**base, **changes})


@pytest.mark.parametrize(
    "changes",
    [
        {"required_inputs": ("not_a_frame",)},
        {"required_inputs": ()},
        {"direction": "descriptive", "in_composite": True},
        {"inference": "none", "shrink": True},
        {"formula": "  "},
    ],
)
def test_incoherent_metric_definitions_are_rejected(changes: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        _metric(**changes)


def test_orientation() -> None:
    assert _metric(direction="higher").orientation == 1
    assert _metric(direction="lower").orientation == -1
    assert _metric(direction="descriptive", in_composite=False).orientation == 0


def test_catalog_fingerprint_tracks_definitions_and_composite_rules() -> None:
    assert catalog_fingerprint() == METRIC_CATALOG_FINGERPRINT
    assert catalog_fingerprint(tuple(reversed(METRIC_DEFINITIONS))) == METRIC_CATALOG_FINGERPRINT
    edited = (_metric(formula="median(contribution)"), *METRIC_DEFINITIONS[1:])
    assert catalog_fingerprint(edited) != METRIC_CATALOG_FINGERPRINT
    assert catalog_fingerprint(spec=CompositeSpec(z_clip=2.0)) != METRIC_CATALOG_FINGERPRINT
    assert CompositeSpec() == COMPOSITE_SPEC


def _context() -> RunContext:
    return RunContext(
        as_of_date=date(2025, 12, 31),
        knowledge_cutoff=datetime(2026, 1, 1, tzinfo=UTC),
        source_revision="r1",
        source_fingerprint="a" * 64,
        universe_fingerprint="b" * 64,
        algorithm_version="1",
    )


def test_metric_catalog_change_invalidates_metrics_and_statistics_only() -> None:
    methodology, context = Methodology(), _context()
    other = "f" * 64
    for stage in (Stage.INTERVALS, Stage.RETURNS, Stage.EXPOSURES):
        assert stage_fingerprint(stage, methodology, context, ()) == stage_fingerprint(
            stage, methodology, context, (), metric_catalog_fingerprint=other
        )
    for stage in (Stage.METRICS, Stage.STATISTICS):
        assert stage_fingerprint(stage, methodology, context, ()) != stage_fingerprint(
            stage, methodology, context, (), metric_catalog_fingerprint=other
        )


def test_primary_horizon_must_be_reported_and_only_affects_metrics() -> None:
    with pytest.raises(ValidationError):
        Methodology(primary_fixed_horizon_days=100)
    base = Methodology()
    changed = base.with_overrides({"primary_fixed_horizon_days": 252})
    context = _context()
    for stage in (Stage.INTERVALS, Stage.RETURNS, Stage.EXPOSURES):
        assert stage_fingerprint(stage, base, context, ()) == stage_fingerprint(
            stage, changed, context, ()
        )
    assert stage_fingerprint(Stage.METRICS, base, context, ()) != stage_fingerprint(
        Stage.METRICS, changed, context, ()
    )


def test_default_family_weights_lead_with_skill() -> None:
    weights = FamilyWeights()
    assert weights.return_skill == max(weights.model_dump().values())
    assert weights.behavior < weights.consistency


def test_horizon_labels() -> None:
    assert horizon_label(None) == HORIZON_TO_EXIT
    assert horizon_label(126) == "d126"
    with pytest.raises(ValueError):
        horizon_label(0)


def test_stage_frames_carry_audit_and_execution_columns() -> None:
    interval_columns = set(INTERVAL_FRAME.column_names)
    assert {
        "native_rating",
        "knowledge_ts",
        "available_ts",
        "entry_point",
        "exit_point",
        "decay_start_session",
        "non_investable_reason",
    } <= interval_columns
    assert "position_weight" not in interval_columns
    assert {"entry_price", "exit_price", "terminated_by_delisting"} <= set(
        RETURN_FRAME.column_names
    )
