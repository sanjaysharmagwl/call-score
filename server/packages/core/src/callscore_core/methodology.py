import hashlib
import json
from collections.abc import Mapping
from typing import Annotated, Literal, Self

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    StrictFloat,
    StrictInt,
    field_validator,
    model_validator,
)

from callscore_core.enums import (
    BenchmarkMode,
    CanonicalRating,
    EntryConvention,
    ExpiryPolicy,
    PeerDimension,
    ReturnBasis,
    ScoringMode,
    ShrinkageMethod,
)

Identifier = Annotated[str, Field(min_length=1, max_length=200, pattern=r"^\S+$")]
PositiveInt = Annotated[StrictInt, Field(gt=0)]
Number = Annotated[StrictFloat | StrictInt, AfterValidator(float)]
NonnegativeFloat = Annotated[Number, Field(ge=0, allow_inf_nan=False)]


def _canonical(value: object) -> object:
    if isinstance(value, float) and value == 0:
        return 0.0
    if isinstance(value, dict):
        return {key: _canonical(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [_canonical(item) for item in value]
    return value


def canonical_dumps(value: object) -> str:
    """Deterministic JSON: sorted keys, compact separators, no NaN, -0.0 folded to 0.0."""
    return json.dumps(
        _canonical(value),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


class FrozenModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)

    def canonical_json(self) -> str:
        return canonical_dumps(self.model_dump(mode="json"))

    def fingerprint(self) -> str:
        return hashlib.sha256(self.canonical_json().encode()).hexdigest()


class PositionWeights(FrozenModel):
    strong_buy: Annotated[Number, Field(gt=0, le=1)] = 1.0
    buy: Annotated[Number, Field(gt=0, le=1)] = 0.5
    hold: Annotated[Number, Field(ge=0, le=0)] = 0.0
    sell: Annotated[Number, Field(ge=-1, lt=0)] = -0.5
    strong_sell: Annotated[Number, Field(ge=-1, lt=0)] = -1.0

    @model_validator(mode="after")
    def ordered(self) -> Self:
        if self.strong_buy < self.buy or self.strong_sell > self.sell:
            raise ValueError("Strong ratings must have at least the magnitude of ordinary ratings")
        return self

    def for_rating(self, rating: CanonicalRating) -> float | None:
        match rating:
            case CanonicalRating.STRONG_BUY:
                return float(self.strong_buy)
            case CanonicalRating.BUY:
                return float(self.buy)
            case CanonicalRating.HOLD:
                return 0.0
            case CanonicalRating.SELL:
                return float(self.sell)
            case CanonicalRating.STRONG_SELL:
                return float(self.strong_sell)
            case _:
                return None


class FamilyWeights(FrozenModel):
    """Composite family weights (decision 0003): skill-led, behavior limited to diligence."""

    return_skill: NonnegativeFloat = 0.40
    timing: NonnegativeFloat = 0.15
    price_target: NonnegativeFloat = 0.15
    behavior: NonnegativeFloat = 0.10
    consistency: NonnegativeFloat = 0.20

    @model_validator(mode="after")
    def unit_sum(self) -> Self:
        total = sum(
            (self.return_skill, self.timing, self.price_target, self.behavior, self.consistency)
        )
        if abs(total - 1.0) > 1e-9:
            raise ValueError("Family weights must sum to one")
        return self


class Methodology(FrozenModel):
    contract_version: Literal[1] = 1
    entry_convention: EntryConvention = EntryConvention.NEXT_OPEN
    expiry_policy: ExpiryPolicy = ExpiryPolicy.HARD_CLOSE
    decay_days: Annotated[StrictInt, Field(gt=0, le=3650)] = 90
    benchmark_mode: BenchmarkMode = BenchmarkMode.SECTOR
    benchmark_id: Identifier | None = "IDX_BROAD"
    scoring_mode: ScoringMode = ScoringMode.ABSOLUTE
    position_weights: PositionWeights = PositionWeights()
    fixed_horizons_days: Annotated[
        tuple[Annotated[StrictInt, Field(gt=0, le=2520)], ...], Field(max_length=10)
    ] = (63, 126, 252)
    primary_fixed_horizon_days: Annotated[StrictInt, Field(gt=0, le=2520)] = 126
    min_consensus_ratings: Annotated[StrictInt, Field(ge=1, le=1000)] = 3
    min_ratings_for_rank: Annotated[StrictInt, Field(gt=0, le=100_000)] = 20
    shrinkage_method: ShrinkageMethod = ShrinkageMethod.EMPIRICAL_BAYES
    peer_group_dims: tuple[PeerDimension, ...] = (
        PeerDimension.CAP_BUCKET,
        PeerDimension.REGION,
        PeerDimension.SECTOR,
    )
    min_peer_size: Annotated[StrictInt, Field(ge=2, le=10_000)] = 5
    peer_fallback: Literal["unranked"] = "unranked"
    transaction_cost_bps: Annotated[Number, Field(ge=0, le=10_000)] = 0.0
    include_announcement_window: bool = True
    return_basis: ReturnBasis = ReturnBasis.TOTAL
    family_weights: FamilyWeights = FamilyWeights()
    bootstrap_draws: Annotated[StrictInt, Field(gt=0, le=100_000)] = 1000
    bootstrap_seed: Annotated[StrictInt, Field(ge=0, le=2**63 - 1)] = 20261006
    confidence_level: Annotated[StrictFloat, Field(gt=0, lt=1)] = 0.95
    reporting_currency: Annotated[str, Field(pattern=r"^[A-Z]{3}$")] = "USD"
    missing_fx_policy: Literal["fail"] = "fail"

    @field_validator("fixed_horizons_days")
    @classmethod
    def unique_horizons(cls, value: tuple[int, ...]) -> tuple[int, ...]:
        if not value or len(value) != len(set(value)):
            raise ValueError("Fixed horizons must be non-empty and unique")
        return tuple(sorted(value))

    @field_validator("peer_group_dims")
    @classmethod
    def unique_dimensions(cls, value: tuple[PeerDimension, ...]) -> tuple[PeerDimension, ...]:
        if len(value) != len(set(value)):
            raise ValueError("Peer dimensions must be unique")
        return tuple(sorted(value))

    @model_validator(mode="after")
    def cross_field(self) -> Self:
        if self.benchmark_mode != BenchmarkMode.SECTOR and self.benchmark_id is None:
            raise ValueError("This benchmark mode requires a benchmark_id")
        if self.primary_fixed_horizon_days not in self.fixed_horizons_days:
            raise ValueError("primary_fixed_horizon_days must be one of fixed_horizons_days")
        return self

    def with_overrides(self, overrides: Mapping[str, object]) -> Self:
        payload = self.model_dump(mode="json")
        for key, value in overrides.items():
            if key in ("position_weights", "family_weights") and isinstance(value, Mapping):
                payload[key] = {**payload[key], **value}
            else:
                payload[key] = value
        return type(self).model_validate(payload)
