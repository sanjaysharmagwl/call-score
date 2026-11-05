"""Metric catalog and composite-score rules (decision 0003).

The catalog and composite spec are data: any change alters ``METRIC_CATALOG_FINGERPRINT``,
which feeds the metric and statistics stage cache keys.
"""

import hashlib
from typing import Annotated, Literal, Self

from pydantic import Field, model_validator

from callscore_core.enums import MetricFamily
from callscore_core.frames import FRAME_CONTRACTS
from callscore_core.methodology import FrozenModel, canonical_dumps

Text = Annotated[str, Field(min_length=1, pattern=r"\S")]


class MetricDefinition(FrozenModel):
    code: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    family: MetricFamily
    unit: Literal["return", "ratio", "fraction", "count", "per_year"]
    direction: Literal["higher", "lower", "descriptive"]
    horizon: Literal["to_exit", "primary_fixed", "daily", "event_window", "target", "none"]
    required_inputs: Annotated[tuple[str, ...], Field(min_length=1)]
    formula: Text
    estimand: Text
    unavailable_when: Text
    minimum_observations: int = Field(ge=1, strict=True)
    in_composite: bool
    inference: Literal["quarter_block_bootstrap", "none"]
    shrink: bool
    aggregate: Literal["mean", "annualized_mean", "annualized_sharpe", "none"]
    """How ``metric_samples`` values combine into the metric (and each bootstrap draw):
    ``mean`` -> mean; ``annualized_mean`` -> 252 * mean;
    ``annualized_sharpe`` -> sqrt(252) * mean / sample_std. Defaults from ``inference``."""
    null_value: float | None = None
    """No-skill value for the t-stat and significance; ``None`` means no natural null."""

    @model_validator(mode="before")
    @classmethod
    def default_aggregate(cls, data: object) -> object:
        if isinstance(data, dict) and "aggregate" not in data:
            return {**data, "aggregate": "none" if data.get("inference") == "none" else "mean"}
        return data

    @model_validator(mode="after")
    def coherent(self) -> Self:
        unknown = set(self.required_inputs) - FRAME_CONTRACTS.keys()
        if unknown:
            raise ValueError(f"Unknown required inputs: {sorted(unknown)}")
        if (self.inference == "none") != (self.aggregate == "none"):
            raise ValueError("Bootstrapped metrics need a sample aggregate, others none")
        if self.null_value is not None and self.inference == "none":
            raise ValueError("A null value needs an inference method")
        if self.in_composite and self.direction == "descriptive":
            raise ValueError("Composite metrics need a higher/lower direction")
        if self.shrink and self.inference == "none":
            raise ValueError("Shrinkage needs a sampling-variance estimate")
        return self

    @property
    def orientation(self) -> int:
        """+1 when higher is better, -1 when lower is better, 0 for descriptive metrics."""
        return {"higher": 1, "lower": -1, "descriptive": 0}[self.direction]


class CompositeSpec(FrozenModel):
    """How scored metrics become a 0-100 composite and a rank."""

    version: Literal[1] = 1
    standardization: Literal["peer_z_of_shrunk_else_raw"] = "peer_z_of_shrunk_else_raw"
    z_clip: Annotated[float, Field(gt=0, le=10)] = 3.0
    family_aggregation: Literal["mean_of_oriented_z"] = "mean_of_oriented_z"
    missing_family: Literal["renormalize_weights"] = "renormalize_weights"
    min_scored_families: Annotated[int, Field(ge=1, le=5, strict=True)] = 3
    score_mapping: Literal["normal_cdf_of_cohort_standardized"] = (
        "normal_cdf_of_cohort_standardized"
    )
    tie_break: Literal["composite_desc_then_analyst_id"] = "composite_desc_then_analyst_id"


def _m(**kwargs: object) -> MetricDefinition:
    return MetricDefinition.model_validate(kwargs)


_BOOT = "quarter_block_bootstrap"

METRIC_DEFINITIONS: tuple[MetricDefinition, ...] = (
    # --- return / skill -------------------------------------------------------------------
    _m(
        code="mean_contribution",
        family=MetricFamily.RETURN_SKILL,
        unit="return",
        direction="higher",
        horizon="to_exit",
        required_inputs=("contributions",),
        formula="mean(contribution) over investable intervals with non-null contribution",
        estimand="mean weighted excess return per call, holding period to exit",
        unavailable_when="no investable interval with a contribution",
        minimum_observations=1,
        in_composite=True,
        inference=_BOOT,
        shrink=True,
        null_value=0.0,
    ),
    _m(
        code="hit_rate",
        family=MetricFamily.RETURN_SKILL,
        unit="fraction",
        direction="higher",
        horizon="to_exit",
        required_inputs=("contributions",),
        formula="count(contribution > 0) / count(position_weight != 0 and contribution not null)",
        estimand="share of directional calls whose weighted excess return was positive",
        unavailable_when="no directional (non-hold) interval with a contribution",
        minimum_observations=1,
        in_composite=True,
        inference=_BOOT,
        shrink=True,
        null_value=0.5,
    ),
    _m(
        code="information_coefficient",
        family=MetricFamily.RETURN_SKILL,
        unit="ratio",
        direction="higher",
        horizon="primary_fixed",
        required_inputs=("contributions",),
        formula="spearman(position_weight, excess_return) over intervals at the primary horizon",
        estimand="rank association between call direction and subsequent excess return",
        unavailable_when="fewer than 10 pairs or either series constant",
        minimum_observations=10,
        in_composite=True,
        inference="none",
        shrink=False,
    ),
    _m(
        code="information_ratio",
        family=MetricFamily.RETURN_SKILL,
        unit="ratio",
        direction="higher",
        horizon="daily",
        required_inputs=("portfolio_nav",),
        formula="sqrt(252) * mean(active_return) / sample_std(active_return)",
        estimand="annualized risk-adjusted active return of the simulated analyst book",
        unavailable_when="fewer than 63 active-return sessions or zero active-return variance",
        minimum_observations=63,
        in_composite=True,
        inference=_BOOT,
        shrink=False,
        aggregate="annualized_sharpe",
        null_value=0.0,
    ),
    _m(
        code="annualized_alpha",
        family=MetricFamily.RETURN_SKILL,
        unit="return",
        direction="higher",
        horizon="daily",
        required_inputs=("portfolio_nav",),
        formula="252 * mean(active_return); CI from quarter-block bootstrap of the same series",
        estimand="annualized arithmetic mean active return of the simulated analyst book",
        unavailable_when="fewer than 63 active-return sessions",
        minimum_observations=63,
        in_composite=False,
        inference=_BOOT,
        shrink=True,
        aggregate="annualized_mean",
        null_value=0.0,
    ),
    _m(
        code="max_drawdown",
        family=MetricFamily.RETURN_SKILL,
        unit="return",
        direction="descriptive",
        horizon="daily",
        required_inputs=("portfolio_nav",),
        formula="min(drawdown), drawdown = nav / running_max(nav) - 1",
        estimand="worst peak-to-trough loss of the simulated analyst book",
        unavailable_when="fewer than 2 NAV sessions",
        minimum_observations=2,
        in_composite=False,
        inference="none",
        shrink=False,
    ),
    # --- timing ---------------------------------------------------------------------------
    _m(
        code="revision_drift_20d",
        family=MetricFamily.TIMING,
        unit="return",
        direction="higher",
        horizon="event_window",
        required_inputs=("event_windows",),
        formula="mean(sign(weight_change) * post_event_excess_20d) where weight_change != 0",
        estimand="excess return in the direction of a rating change over the next 20 sessions",
        unavailable_when="no rating change with a complete post-event window",
        minimum_observations=5,
        in_composite=True,
        inference=_BOOT,
        shrink=True,
        null_value=0.0,
    ),
    _m(
        code="signed_car",
        family=MetricFamily.TIMING,
        unit="return",
        direction="descriptive",
        horizon="event_window",
        required_inputs=("event_windows",),
        formula="mean(sign(weight_change) * car_pm1) where weight_change != 0",
        estimand="announcement reaction in the call's direction (influence, not skill)",
        unavailable_when="announcement windows disabled or no complete window",
        minimum_observations=1,
        in_composite=False,
        inference="none",
        shrink=False,
    ),
    _m(
        code="momentum_chasing",
        family=MetricFamily.TIMING,
        unit="return",
        direction="descriptive",
        horizon="event_window",
        required_inputs=("event_windows",),
        formula="mean(sign(weight_change) * pre_event_excess_20d) where weight_change != 0",
        estimand="how far the stock had already moved in the call's direction before it",
        unavailable_when="no rating change with a complete pre-event window",
        minimum_observations=1,
        in_composite=False,
        inference="none",
        shrink=False,
    ),
    # --- price target ---------------------------------------------------------------------
    _m(
        code="target_hit_rate",
        family=MetricFamily.PRICE_TARGET,
        unit="fraction",
        direction="higher",
        horizon="target",
        required_inputs=("target_outcomes",),
        formula="count(hit) / count(is_matured and implied_return != 0)",
        estimand="share of matured targets reached intra-horizon (adjusted high/low)",
        unavailable_when="no matured target with non-zero implied return",
        minimum_observations=1,
        in_composite=True,
        inference=_BOOT,
        shrink=True,
    ),
    _m(
        code="target_absolute_error",
        family=MetricFamily.PRICE_TARGET,
        unit="return",
        direction="lower",
        horizon="target",
        required_inputs=("target_outcomes",),
        formula="mean(abs(realized_return - implied_return)) over matured targets",
        estimand="mean absolute target error at the target horizon, adjusted price basis",
        unavailable_when="no matured target",
        minimum_observations=1,
        in_composite=True,
        inference=_BOOT,
        shrink=True,
    ),
    _m(
        code="target_bias",
        family=MetricFamily.PRICE_TARGET,
        unit="return",
        direction="descriptive",
        horizon="target",
        required_inputs=("target_outcomes",),
        formula="mean(implied_return - realized_return) over matured targets",
        estimand="mean signed target optimism at the target horizon",
        unavailable_when="no matured target",
        minimum_observations=1,
        in_composite=False,
        inference="none",
        shrink=False,
    ),
    # --- behavior -------------------------------------------------------------------------
    _m(
        code="stale_coverage",
        family=MetricFamily.BEHAVIOR,
        unit="fraction",
        direction="lower",
        horizon="none",
        required_inputs=("coverage_daily",),
        formula="count(is_stale) / count(covered sessions)",
        estimand="share of covered sessions past the rating's native expiry (diligence)",
        unavailable_when="no covered sessions",
        minimum_observations=1,
        in_composite=True,
        inference=_BOOT,
        shrink=False,
    ),
    _m(
        code="independence",
        family=MetricFamily.BEHAVIOR,
        unit="ratio",
        direction="descriptive",
        horizon="event_window",
        required_inputs=("event_windows",),
        formula="mean(abs(consensus_deviation)) over events with a consensus",
        estimand="average distance of the call from other analysts' visible consensus",
        unavailable_when="no event with enough visible peer ratings",
        minimum_observations=1,
        in_composite=False,
        inference="none",
        shrink=False,
    ),
    _m(
        code="buy_fraction",
        family=MetricFamily.BEHAVIOR,
        unit="fraction",
        direction="descriptive",
        horizon="none",
        required_inputs=("rating_events",),
        formula="count(strong_buy or buy) / count(investable canonical ratings)",
        estimand="buy-side share of the analyst's investable ratings",
        unavailable_when="no investable rating events",
        minimum_observations=1,
        in_composite=False,
        inference="none",
        shrink=False,
    ),
    _m(
        code="optimism_vs_peers",
        family=MetricFamily.BEHAVIOR,
        unit="fraction",
        direction="descriptive",
        horizon="none",
        required_inputs=("rating_events", "security_classifications"),
        formula="buy_fraction - mean(buy_fraction of peer group)",
        estimand="buy-side skew relative to the analyst's peer group",
        unavailable_when="buy_fraction unavailable or peer group below min_peer_size",
        minimum_observations=1,
        in_composite=False,
        inference="none",
        shrink=False,
    ),
    _m(
        code="ratings_per_year",
        family=MetricFamily.BEHAVIOR,
        unit="per_year",
        direction="descriptive",
        horizon="none",
        required_inputs=("rating_events", "coverage_spells"),
        formula="count(investable rating events) / covered security-years",
        estimand="activity: rating actions per covered name per year",
        unavailable_when="no covered security-time",
        minimum_observations=1,
        in_composite=False,
        inference="none",
        shrink=False,
    ),
    # --- consistency ----------------------------------------------------------------------
    _m(
        code="positive_quarter_fraction",
        family=MetricFamily.CONSISTENCY,
        unit="fraction",
        direction="higher",
        horizon="to_exit",
        required_inputs=("contributions",),
        formula="count(quarters with mean contribution > 0) / count(observed entry quarters)",
        estimand="share of calendar quarters (by entry date) with positive mean contribution",
        unavailable_when="fewer than 4 observed quarters",
        minimum_observations=4,
        in_composite=True,
        inference="none",
        shrink=False,
    ),
    _m(
        code="quarterly_contribution_std",
        family=MetricFamily.CONSISTENCY,
        unit="return",
        direction="descriptive",
        horizon="to_exit",
        required_inputs=("contributions",),
        formula="sample_std(quarterly mean contribution)",
        estimand="dispersion of quarterly call performance",
        unavailable_when="fewer than 4 observed quarters",
        minimum_observations=4,
        in_composite=False,
        inference="none",
        shrink=False,
    ),
    _m(
        code="sector_dispersion",
        family=MetricFamily.CONSISTENCY,
        unit="return",
        direction="descriptive",
        horizon="to_exit",
        required_inputs=("contributions", "security_classifications"),
        formula="sample_std(mean contribution per entry-date sector with >= 3 intervals)",
        estimand="how unevenly performance is spread across covered sectors",
        unavailable_when="fewer than 2 sectors with 3 or more intervals",
        minimum_observations=2,
        in_composite=False,
        inference="none",
        shrink=False,
    ),
    _m(
        code="regime_spread",
        family=MetricFamily.CONSISTENCY,
        unit="return",
        direction="descriptive",
        horizon="to_exit",
        required_inputs=("contributions", "benchmark_daily"),
        formula="mean contribution in up-market quarters - mean in down-market quarters",
        estimand="dependence of call performance on the broad-market regime",
        unavailable_when="no observed quarter in either regime",
        minimum_observations=2,
        in_composite=False,
        inference="none",
        shrink=False,
    ),
)

COMPOSITE_SPEC = CompositeSpec()

_BY_CODE = {definition.code: definition for definition in METRIC_DEFINITIONS}


def metric_definition(code: str) -> MetricDefinition:
    """Look up a registered metric; raises ``KeyError`` for unknown codes."""
    return _BY_CODE[code]


def composite_metrics(family: MetricFamily) -> tuple[MetricDefinition, ...]:
    return tuple(m for m in METRIC_DEFINITIONS if m.family == family and m.in_composite)


def catalog_fingerprint(
    definitions: tuple[MetricDefinition, ...] = METRIC_DEFINITIONS,
    spec: CompositeSpec = COMPOSITE_SPEC,
) -> str:
    payload = {
        "metrics": sorted(
            (d.model_dump(mode="json") for d in definitions), key=lambda d: str(d["code"])
        ),
        "composite": spec.model_dump(mode="json"),
    }
    return hashlib.sha256(canonical_dumps(payload).encode()).hexdigest()


METRIC_CATALOG_FINGERPRINT = catalog_fingerprint()
