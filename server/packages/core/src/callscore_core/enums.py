from enum import StrEnum


class CanonicalRating(StrEnum):
    STRONG_BUY = "strong_buy"
    BUY = "buy"
    HOLD = "hold"
    SELL = "sell"
    STRONG_SELL = "strong_sell"
    NOT_RATED = "not_rated"
    UNDER_REVIEW = "under_review"
    RESTRICTED = "restricted"


class RatingAction(StrEnum):
    INITIATION = "initiation"
    UPGRADE = "upgrade"
    DOWNGRADE = "downgrade"
    REITERATION = "reiteration"
    PT_CHANGE = "pt_change"
    SUSPEND = "suspend"
    RESTRICT = "restrict"
    RESUME = "resume"
    DROP = "drop"


class EntryConvention(StrEnum):
    NEXT_OPEN = "next_open"
    SAME_CLOSE = "same_close"
    T_PLUS_2_OPEN = "t_plus_2_open"


class ExpiryPolicy(StrEnum):
    HARD_CLOSE = "hard_close"
    CARRY_FORWARD = "carry_forward"
    DECAY = "decay"


class BenchmarkMode(StrEnum):
    MARKET = "market"
    SECTOR = "sector"
    BETA_ADJUSTED = "beta_adjusted"
    CUSTOM = "custom"


class ScoringMode(StrEnum):
    ABSOLUTE = "absolute"
    RELATIVE_TO_CONSENSUS = "relative_to_consensus"


class ReturnBasis(StrEnum):
    TOTAL = "total"
    PRICE = "price"


class ShrinkageMethod(StrEnum):
    NONE = "none"
    EMPIRICAL_BAYES = "empirical_bayes"


class PeerDimension(StrEnum):
    SECTOR = "sector"
    REGION = "region"
    CAP_BUCKET = "cap_bucket"


class ExitReason(StrEnum):
    SUPERSEDED = "superseded"
    EXPIRED = "expired"
    DROPPED = "dropped"
    DELISTED = "delisted"
    COVERAGE_ENDED = "coverage_ended"
    ANALYST_LEFT = "analyst_left"
    RUN_END = "run_end"


class RunStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    INTERRUPTED = "interrupted"


class Stage(StrEnum):
    INTERVALS = "intervals"
    RETURNS = "returns"
    EXPOSURES = "exposures"
    METRICS = "metrics"
    STATISTICS = "statistics"


class MetricFamily(StrEnum):
    RETURN_SKILL = "return_skill"
    TIMING = "timing"
    PRICE_TARGET = "price_target"
    BEHAVIOR = "behavior"
    CONSISTENCY = "consistency"
