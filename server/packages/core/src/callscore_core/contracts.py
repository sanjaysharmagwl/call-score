import hashlib
from datetime import UTC, date, datetime
from typing import Annotated, Literal, Self

from pydantic import AwareDatetime, Field, TypeAdapter, field_validator, model_validator

from callscore_core.enums import Stage
from callscore_core.methodology import FrozenModel, Identifier, Methodology, canonical_dumps
from callscore_core.metrics import METRIC_CATALOG_FINGERPRINT

Fingerprint = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
_FINGERPRINTS: TypeAdapter[tuple[str, ...]] = TypeAdapter(tuple[Fingerprint, ...])

STAGE_ORDER: tuple[Stage, ...] = (
    Stage.INTERVALS,
    Stage.RETURNS,
    Stage.EXPOSURES,
    Stage.METRICS,
    Stage.STATISTICS,
)


class RunContext(FrozenModel):
    as_of_date: date
    knowledge_cutoff: AwareDatetime
    source_revision: Identifier
    source_fingerprint: Fingerprint
    universe_fingerprint: Fingerprint
    algorithm_version: Identifier

    @field_validator("knowledge_cutoff")
    @classmethod
    def utc_cutoff(cls, value: datetime) -> datetime:
        return value.astimezone(UTC)


class RunProvenance(FrozenModel):
    run_id: Identifier
    methodology_version_id: Identifier
    methodology_fingerprint: Fingerprint


class StageArtifact(FrozenModel):
    stage: Stage
    schema_version: Literal[1] = 1
    content_fingerprint: Fingerprint
    cache_fingerprint: Fingerprint
    upstream_fingerprints: tuple[Fingerprint, ...] = ()
    row_count: Annotated[int, Field(ge=0, strict=True)]


STAGE_METHODOLOGY_FIELDS: dict[Stage, tuple[str, ...]] = {
    Stage.INTERVALS: ("entry_convention", "expiry_policy", "decay_days"),
    Stage.RETURNS: (
        "benchmark_mode",
        "benchmark_id",
        "return_basis",
        "fixed_horizons_days",
        "include_announcement_window",
        "reporting_currency",
        "missing_fx_policy",
    ),
    Stage.EXPOSURES: (
        "position_weights",
        "scoring_mode",
        "expiry_policy",
        "decay_days",
        "min_consensus_ratings",
    ),
    Stage.METRICS: ("transaction_cost_bps", "primary_fixed_horizon_days"),
    Stage.STATISTICS: (
        "min_ratings_for_rank",
        "shrinkage_method",
        "peer_group_dims",
        "min_peer_size",
        "peer_fallback",
        "family_weights",
        "bootstrap_draws",
        "bootstrap_seed",
        "confidence_level",
    ),
}


_CATALOG_STAGES = frozenset({Stage.METRICS, Stage.STATISTICS})


def stage_fingerprint(
    stage: Stage,
    methodology: Methodology,
    context: RunContext,
    upstream_fingerprints: tuple[str, ...],
    metric_catalog_fingerprint: str = METRIC_CATALOG_FINGERPRINT,
) -> str:
    params = methodology.model_dump(mode="json")
    payload = {
        "contract_version": methodology.contract_version,
        "stage_schema_version": 1,
        "stage": stage.value,
        "context": context.model_dump(mode="json"),
        "methodology": {key: params[key] for key in STAGE_METHODOLOGY_FIELDS[stage]},
        "upstream": _FINGERPRINTS.validate_python(upstream_fingerprints),
    }
    if stage in _CATALOG_STAGES:
        payload["metric_catalog"] = _FINGERPRINTS.validate_python((metric_catalog_fingerprint,))
    return hashlib.sha256(canonical_dumps(payload).encode()).hexdigest()


_STATISTICAL_FIELDS = (
    "std_err",
    "t_stat",
    "ci_low",
    "ci_high",
    "shrunk_value",
    "peer_percentile",
    "is_significant",
)


class MetricValue(FrozenModel):
    value: float | None
    n_obs: Annotated[int, Field(ge=0, strict=True)]
    std_err: Annotated[float, Field(ge=0)] | None = None
    t_stat: float | None = None
    ci_low: float | None = None
    ci_high: float | None = None
    shrunk_value: float | None = None
    peer_percentile: Annotated[float, Field(ge=0, le=100)] | None = None
    is_significant: bool | None = None
    unavailable_reason: Annotated[str, Field(min_length=1, pattern=r"\S")] | None = None

    @model_validator(mode="after")
    def coherent_uncertainty(self) -> Self:
        if self.value is None:
            if self.unavailable_reason is None:
                raise ValueError("Unavailable metrics require a reason")
            if any(getattr(self, name) is not None for name in _STATISTICAL_FIELDS):
                raise ValueError("Unavailable metrics cannot carry statistical outputs")
            return self
        if self.unavailable_reason is not None:
            raise ValueError("Available metrics cannot have an unavailable_reason")
        if (self.ci_low is None) != (self.ci_high is None):
            raise ValueError("Confidence interval endpoints must both be present or absent")
        if self.ci_low is not None and self.ci_high is not None and self.ci_low > self.ci_high:
            raise ValueError("Confidence interval endpoints must be ordered")
        return self
