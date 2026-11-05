"""Hand-computed value tests for every metric family."""

import math
import statistics
from datetime import UTC, date, datetime, timedelta

import polars as pl
import pytest

from callscore_core.contracts import RunContext
from callscore_core.methodology import Methodology
from callscore_engine.metrics import MetricInputs, compute_metrics

AS_OF = date(2024, 12, 31)
TS = datetime(2024, 1, 2, 12, 0, tzinfo=UTC)


def _context(as_of: date = AS_OF) -> RunContext:
    return RunContext(
        as_of_date=as_of,
        knowledge_cutoff=datetime(2024, 12, 31, 23, 59, tzinfo=UTC),
        source_revision="r",
        source_fingerprint="0" * 64,
        universe_fingerprint="0" * 64,
        algorithm_version="v",
    )


def _value(out: pl.DataFrame, analyst: str, code: str) -> dict[str, object]:
    row = out.values.filter(
        (pl.col("analyst_id") == analyst) & (pl.col("metric_code") == code)
    ).row(0, named=True)
    return row


def _contribution(
    interval_id: str,
    analyst: str,
    horizon: str,
    entry: date,
    weight: float | None,
    contribution: float | None,
    excess: float | None = None,
) -> dict[str, object]:
    return {
        "interval_id": interval_id,
        "analyst_id": analyst,
        "horizon": horizon,
        "entry_session": entry,
        "position_weight": weight,
        "excess_return": excess,
        "contribution": contribution,
        "unavailable_reason": None,
    }


def _contributions_frame(rows: list[dict[str, object]]) -> pl.DataFrame:
    return pl.DataFrame(rows)


# --- return / skill -----------------------------------------------------------------------


def test_mean_contribution_and_hit_rate() -> None:
    rows = [
        _contribution("i1", "a1", "to_exit", date(2024, 1, 15), 1.0, 0.10, 0.10),
        _contribution("i2", "a1", "to_exit", date(2024, 2, 10), -0.5, -0.05, 0.10),
        _contribution("i3", "a1", "to_exit", date(2024, 4, 5), 0.0, 0.0, 0.02),
        _contribution("i4", "a1", "to_exit", date(2024, 5, 6), None, 0.07, 0.07),
        _contribution("i5", "a1", "to_exit", date(2024, 6, 3), 1.0, None, None),
        _contribution("i6", "a1", "d126", date(2024, 1, 16), 1.0, 0.5, 0.04),
        _contribution("j1", "a2", "to_exit", date(2024, 3, 1), 0.5, 0.20, 0.20),
        _contribution("j2", "a2", "to_exit", date(2024, 8, 1), 1.0, -0.10, -0.10),
    ]
    out = compute_metrics(
        MetricInputs(contributions=_contributions_frame(rows)),
        ["a1", "a2"],
        Methodology(),
        _context(),
    )
    # a1: mean(0.10, -0.05, 0.0) = 0.05/3; hit_rate: i1 hit, i2 miss, i3 excluded (w=0).
    assert _value(out, "a1", "mean_contribution")["value"] == pytest.approx(0.05 / 3)
    assert _value(out, "a1", "mean_contribution")["n_obs"] == 3
    assert _value(out, "a1", "hit_rate")["value"] == pytest.approx(0.5)
    assert _value(out, "a1", "hit_rate")["n_obs"] == 2
    assert _value(out, "a2", "mean_contribution")["value"] == pytest.approx(0.05)
    assert _value(out, "a2", "hit_rate")["value"] == pytest.approx(0.5)


def test_information_coefficient_spearman() -> None:
    rows = []
    for i in range(10):
        rows.append(
            _contribution(
                f"p{i}",
                "a1",
                "d126",
                date(2024, 1, 2) + timedelta(days=i),
                0.1 * (i + 1),
                None,
                0.01 * (i + 1),
            )
        )
    for i in range(10):
        rows.append(
            _contribution(
                f"q{i}",
                "a2",
                "d126",
                date(2024, 1, 2) + timedelta(days=i),
                0.5,
                None,
                0.01 * (i + 1),
            )
        )
    out = compute_metrics(
        MetricInputs(contributions=_contributions_frame(rows)),
        ["a1", "a2"],
        Methodology(),
        _context(),
    )
    # a1: weight and excess return both strictly increase -> Spearman = 1.
    assert _value(out, "a1", "information_coefficient")["value"] == pytest.approx(1.0)
    assert _value(out, "a1", "information_coefficient")["n_obs"] == 10
    # a2: position_weight constant -> zero_variance.
    row = _value(out, "a2", "information_coefficient")
    assert row["value"] is None
    assert row["unavailable_reason"] == "zero_variance"
    assert row["n_obs"] == 10


def test_information_coefficient_below_minimum() -> None:
    rows = [
        _contribution(
            f"p{i}", "a1", "d126", date(2024, 1, 2) + timedelta(days=i), 0.1 * (i + 1), None, 0.01
        )
        for i in range(9)
    ]
    out = compute_metrics(
        MetricInputs(contributions=_contributions_frame(rows)), ["a1"], Methodology(), _context()
    )
    row = _value(out, "a1", "information_coefficient")
    assert row["value"] is None
    assert row["unavailable_reason"] == "insufficient_observations"
    assert row["n_obs"] == 9


def _nav_rows(analyst: str, n: int, active: list[float] | None = None) -> list[dict[str, object]]:
    rows = []
    for i in range(n):
        ar = None if active is None else active[i]
        rows.append(
            {
                "analyst_id": analyst,
                "session_date": date(2024, 1, 2) + timedelta(days=i),
                "nav": 1.0,
                "benchmark_nav": 1.0,
                "active_return": ar,
                "gross_exposure": 1.0,
                "turnover": 0.0,
                "cost": 0.0,
                "drawdown": 0.0,
            }
        )
    return rows


def test_information_ratio_and_annualized_alpha() -> None:
    active = [0.002 + (0.001 if i % 2 == 0 else -0.001) for i in range(64)]
    nav = pl.DataFrame(_nav_rows("a1", 64, active))
    out = compute_metrics(MetricInputs(portfolio_nav=nav), ["a1"], Methodology(), _context())
    mean = statistics.fmean(active)
    sd = statistics.stdev(active)
    alpha = _value(out, "a1", "annualized_alpha")
    ir = _value(out, "a1", "information_ratio")
    assert alpha["value"] == pytest.approx(252.0 * mean)
    assert alpha["n_obs"] == 64
    assert ir["value"] == pytest.approx(math.sqrt(252.0) * mean / sd)
    assert ir["n_obs"] == 64


def test_daily_metrics_below_minimum() -> None:
    nav = pl.DataFrame(_nav_rows("a1", 10, [0.001] * 10))
    out = compute_metrics(MetricInputs(portfolio_nav=nav), ["a1"], Methodology(), _context())
    for code in ("information_ratio", "annualized_alpha"):
        row = _value(out, "a1", code)
        assert row["value"] is None
        assert row["unavailable_reason"] == "insufficient_observations"
        assert row["n_obs"] == 10
    # max_drawdown only needs 2 sessions.
    assert _value(out, "a1", "max_drawdown")["value"] == pytest.approx(0.0)


def test_max_drawdown_is_min_drawdown() -> None:
    rows = _nav_rows("a1", 4)
    for row, dd in zip(rows, [0.0, -0.05, -0.02, -0.10], strict=True):
        row["drawdown"] = dd
    out = compute_metrics(
        MetricInputs(portfolio_nav=pl.DataFrame(rows)), ["a1"], Methodology(), _context()
    )
    row = _value(out, "a1", "max_drawdown")
    assert row["value"] == pytest.approx(-0.10)
    assert row["n_obs"] == 4


# --- timing -------------------------------------------------------------------------------


def _event_window(
    event_id: str,
    analyst: str,
    weight_change: float | None,
    car: float | None,
    pre: float | None,
    post: float | None,
    consensus: float | None = None,
) -> dict[str, object]:
    return {
        "event_id": event_id,
        "analyst_id": analyst,
        "security_id": "S1",
        "event_session": date(2024, 3, 1),
        "weight_change": weight_change,
        "consensus_deviation": consensus,
        "car_pm1": car,
        "pre_event_excess_20d": pre,
        "post_event_excess_20d": post,
        "unavailable_reason": None,
    }


def test_revision_drift_signed_car_and_momentum() -> None:
    rows = [
        _event_window("e1", "a1", 1.0, 0.02, 0.10, 0.10, 0.5),
        _event_window("e2", "a1", -0.5, 0.01, -0.20, 0.20, -0.25),
        _event_window("e3", "a1", 0.5, -0.01, 0.05, -0.30, None),
        _event_window("e4", "a1", -1.0, 0.03, None, None, 0.0),
        _event_window("e5", "a1", 0.0, 0.50, 0.50, 0.50, 0.5),
        _event_window("e6", "a1", None, 0.40, 0.40, 0.40, 0.5),
        _event_window("e7", "a1", 1.0, 0.05, -0.10, 0.40, 1.0),
        _event_window("e8", "a1", -1.0, -0.02, 0.30, -0.20, None),
    ]
    out = compute_metrics(
        MetricInputs(event_windows=pl.DataFrame(rows)), ["a1"], Methodology(), _context()
    )
    # revision_drift: sign(wc)*post over wc!=0, post not null -> e1,e2,e3,e7,e8:
    # +0.10, -0.20, -0.30, +0.40, +0.20 -> mean 0.04 over 5 obs.
    drift = _value(out, "a1", "revision_drift_20d")
    assert drift["value"] == pytest.approx(0.04)
    assert drift["n_obs"] == 5
    # signed_car: same rows but car present incl. e4 -> signed:
    # 0.02, -0.01, -0.01, -0.03, 0.05, +0.02 -> mean 0.04/6.
    car = _value(out, "a1", "signed_car")
    assert car["value"] == pytest.approx(0.04 / 6)
    assert car["n_obs"] == 6
    # momentum: sign(wc)*pre -> 0.10, +0.20, 0.05, -0.10, -0.30 -> mean -0.01.
    mom = _value(out, "a1", "momentum_chasing")
    assert mom["value"] == pytest.approx(-0.01)
    assert mom["n_obs"] == 5
    # independence: mean |consensus_deviation| over non-null -> e1,e2,e4,e5,e6,e7:
    # (0.5 + 0.25 + 0.0 + 0.5 + 0.5 + 1.0) / 6.
    ind = _value(out, "a1", "independence")
    assert ind["value"] == pytest.approx(2.75 / 6)
    assert ind["n_obs"] == 6


def test_signed_car_disabled_by_methodology() -> None:
    rows = [_event_window("e1", "a1", 1.0, 0.02, None, None)]
    methodology = Methodology(include_announcement_window=False)
    out = compute_metrics(
        MetricInputs(event_windows=pl.DataFrame(rows)), ["a1"], methodology, _context()
    )
    row = _value(out, "a1", "signed_car")
    assert row["value"] is None
    assert row["unavailable_reason"] == "announcement_window_disabled"
    # Other timing metrics are unaffected.
    assert _value(out, "a1", "momentum_chasing")["value"] is None  # no pre window data


# --- price target -------------------------------------------------------------------------


def _target(
    event_id: str,
    analyst: str,
    implied: float,
    realized: float | None,
    hit: bool | None,
    matured: bool,
) -> dict[str, object]:
    return {
        "event_id": event_id,
        "analyst_id": analyst,
        "entry_session": date(2024, 1, 2),
        "horizon_end_session": date(2024, 7, 1),
        "entry_price": 100.0,
        "target_price": 100.0 * (1.0 + implied),
        "implied_return": implied,
        "realized_return": realized,
        "hit": hit,
        "is_matured": matured,
    }


def test_target_metrics() -> None:
    rows = [
        _target("t1", "a1", 0.20, 0.10, True, True),
        _target("t2", "a1", 0.10, 0.30, True, True),
        _target("t3", "a1", -0.10, -0.05, False, True),
        _target("t4", "a1", 0.0, 0.05, True, True),  # implied == 0: out of hit_rate only
        _target("t5", "a1", 0.20, None, None, False),  # unmatured: excluded everywhere
        _target("t6", "a1", 0.15, None, None, True),  # matured but unrealized: errors skip
    ]
    out = compute_metrics(
        MetricInputs(target_outcomes=pl.DataFrame(rows)), ["a1"], Methodology(), _context()
    )
    hit = _value(out, "a1", "target_hit_rate")
    assert hit["value"] == pytest.approx(2 / 3)
    assert hit["n_obs"] == 3
    err = _value(out, "a1", "target_absolute_error")
    assert err["value"] == pytest.approx((0.10 + 0.20 + 0.05 + 0.05) / 4)
    assert err["n_obs"] == 4
    bias = _value(out, "a1", "target_bias")
    assert bias["value"] == pytest.approx((0.10 - 0.20 - 0.05 - 0.05) / 4)
    assert bias["n_obs"] == 4


# --- behavior -----------------------------------------------------------------------------


def test_stale_coverage() -> None:
    rows = [
        {
            "analyst_id": "a1",
            "security_id": "S1",
            "session_date": date(2024, 1, 2),
            "is_stale": False,
        },
        {
            "analyst_id": "a1",
            "security_id": "S1",
            "session_date": date(2024, 1, 3),
            "is_stale": True,
        },
        {
            "analyst_id": "a1",
            "security_id": "S2",
            "session_date": date(2024, 1, 4),
            "is_stale": False,
        },
        {
            "analyst_id": "a2",
            "security_id": "S1",
            "session_date": date(2024, 1, 2),
            "is_stale": True,
        },
    ]
    out = compute_metrics(
        MetricInputs(coverage_daily=pl.DataFrame(rows)), ["a1", "a2"], Methodology(), _context()
    )
    assert _value(out, "a1", "stale_coverage")["value"] == pytest.approx(1 / 3)
    assert _value(out, "a1", "stale_coverage")["n_obs"] == 3
    assert _value(out, "a2", "stale_coverage")["value"] == pytest.approx(1.0)


def _rating_event(
    event_id: str,
    analyst: str,
    canonical: str,
    is_correction: bool = False,
    supersedes: str | None = None,
) -> dict[str, object]:
    return {
        "event_id": event_id,
        "analyst_id": analyst,
        "firm_id": "F1",
        "security_id": "S1",
        "announced_ts": TS,
        "effective_ts": TS,
        "knowledge_ts": TS,
        "action_type": "initiation",
        "native_rating": canonical,
        "canonical_rating": canonical,
        "prior_canonical_rating": None,
        "scale_id": "scale1",
        "price_target": None,
        "pt_currency": None,
        "pt_horizon_months": None,
        "expiry_date": None,
        "supersedes_event_id": supersedes,
        "is_correction": is_correction,
    }


def test_buy_fraction_excludes_corrections_and_non_investable() -> None:
    rows = [
        _rating_event("r1", "a1", "strong_buy"),
        _rating_event("r2", "a1", "buy"),
        _rating_event("r3", "a1", "hold"),
        _rating_event("r4", "a1", "sell"),
        _rating_event("r5", "a1", "not_rated"),
        # Correction replacing r2 with a hold: r2 drops out, r6 counts as hold.
        _rating_event("r6", "a1", "hold", is_correction=True, supersedes="r2"),
        _rating_event("r7", "a2", "buy"),
        _rating_event("r8", "a2", "strong_buy"),
    ]
    out = compute_metrics(
        MetricInputs(rating_events=pl.DataFrame(rows)),
        ["a1", "a2"],
        Methodology(),
        _context(),
    )
    # a1 investable: r1(buy), r3, r4, r6 -> 1 buy of 4.
    assert _value(out, "a1", "buy_fraction")["value"] == pytest.approx(0.25)
    assert _value(out, "a1", "buy_fraction")["n_obs"] == 4
    assert _value(out, "a2", "buy_fraction")["value"] == pytest.approx(1.0)
    assert _value(out, "a2", "buy_fraction")["n_obs"] == 2


def _peer_inputs() -> MetricInputs:
    events = [
        _rating_event("r1", "a1", "strong_buy"),
        _rating_event("r2", "a1", "buy"),
        _rating_event("r3", "a1", "sell"),
        _rating_event("r4", "a1", "hold"),
        _rating_event("r5", "a2", "buy"),
        _rating_event("r6", "a2", "strong_buy"),
        _rating_event("r7", "a3", "buy"),  # a3 has no coverage -> no peer group
    ]
    spells = [
        {
            "analyst_id": "a1",
            "security_id": "SEC_X",
            "from_date": date(2024, 1, 1),
            "to_date": None,
            "is_lead": True,
        },
        {
            "analyst_id": "a2",
            "security_id": "SEC_X",
            "from_date": date(2024, 1, 1),
            "to_date": None,
            "is_lead": False,
        },
    ]
    classifications = [
        {
            "security_id": "SEC_X",
            "from_date": date(2020, 1, 1),
            "to_date": None,
            "exchange_id": "XNYS",
            "currency": "USD",
            "sector_id": "TECH",
            "region": "US",
            "cap_bucket": "LARGE",
            "listing_date": date(2010, 1, 1),
            "delisting_date": None,
        }
    ]
    return MetricInputs(
        rating_events=pl.DataFrame(events),
        coverage_spells=pl.DataFrame(spells),
        security_classifications=pl.DataFrame(classifications),
    )


def test_optimism_vs_peers() -> None:
    methodology = Methodology(min_peer_size=2)
    out = compute_metrics(_peer_inputs(), ["a1", "a2", "a3"], methodology, _context())
    # a1 buy_fraction = 2/4 = 0.5; a2 = 2/2 = 1.0; peer mean = 0.75.
    assert _value(out, "a1", "optimism_vs_peers")["value"] == pytest.approx(-0.25)
    assert _value(out, "a2", "optimism_vs_peers")["value"] == pytest.approx(0.25)
    row = _value(out, "a3", "optimism_vs_peers")
    assert row["value"] is None
    assert row["unavailable_reason"] == "no_peer_group"


def test_optimism_vs_peers_group_too_small() -> None:
    methodology = Methodology(min_peer_size=3)
    out = compute_metrics(_peer_inputs(), ["a1", "a2", "a3"], methodology, _context())
    for analyst in ("a1", "a2"):
        row = _value(out, analyst, "optimism_vs_peers")
        assert row["value"] is None
        assert row["unavailable_reason"] == "peer_group_too_small"


def test_ratings_per_year_clips_spells_to_as_of() -> None:
    events = [
        _rating_event("r1", "a1", "buy"),
        _rating_event("r2", "a1", "sell"),
        _rating_event("r3", "a2", "buy"),
    ]
    spells = [
        {
            "analyst_id": "a1",
            "security_id": "SEC_X",
            "from_date": date(2024, 1, 1),
            "to_date": date(2024, 7, 1),  # 183 days inclusive
            "is_lead": True,
        },
        {
            "analyst_id": "a3",
            "security_id": "SEC_Y",
            "from_date": date(2025, 1, 1),  # entirely after as_of: ignored
            "to_date": None,
            "is_lead": True,
        },
    ]
    out = compute_metrics(
        MetricInputs(rating_events=pl.DataFrame(events), coverage_spells=pl.DataFrame(spells)),
        ["a1", "a2", "a3"],
        Methodology(),
        _context(),
    )
    expected = 2 / (183 / 365.25)
    row = _value(out, "a1", "ratings_per_year")
    assert row["value"] == pytest.approx(expected)
    assert row["n_obs"] == 1
    # a2 has events but no coverage -> insufficient; a3's spell is post-as_of.
    assert _value(out, "a2", "ratings_per_year")["unavailable_reason"] == (
        "insufficient_observations"
    )
    assert _value(out, "a3", "ratings_per_year")["unavailable_reason"] == (
        "insufficient_observations"
    )


# --- consistency --------------------------------------------------------------------------


def _quarter_rows() -> list[dict[str, object]]:
    spec = [
        ("q1a", date(2024, 1, 15), 0.10),
        ("q1b", date(2024, 2, 10), -0.05),  # Q1 mean 0.025
        ("q2a", date(2024, 4, 5), 0.0),  # Q2 mean 0.0
        ("q3a", date(2024, 7, 1), 0.30),  # Q3 mean 0.30
        ("q4a", date(2024, 10, 1), -0.20),  # Q4 mean -0.20
        ("q5a", date(2025, 1, 10), 0.15),  # Q1-2025 mean 0.15
    ]
    return [_contribution(iid, "a1", "to_exit", entry, 1.0, c, c) for iid, entry, c in spec]


def test_positive_quarter_fraction_and_std() -> None:
    out = compute_metrics(
        MetricInputs(contributions=_contributions_frame(_quarter_rows())),
        ["a1"],
        Methodology(),
        _context(),
    )
    pqf = _value(out, "a1", "positive_quarter_fraction")
    assert pqf["value"] == pytest.approx(3 / 5)
    assert pqf["n_obs"] == 5
    qstd = _value(out, "a1", "quarterly_contribution_std")
    assert qstd["value"] == pytest.approx(statistics.stdev([0.025, 0.0, 0.30, -0.20, 0.15]))
    assert qstd["n_obs"] == 5


def test_quarter_metrics_below_minimum() -> None:
    rows = _quarter_rows()[:3]  # only 2 observed quarters
    out = compute_metrics(
        MetricInputs(contributions=_contributions_frame(rows)), ["a1"], Methodology(), _context()
    )
    for code in ("positive_quarter_fraction", "quarterly_contribution_std"):
        row = _value(out, "a1", code)
        assert row["value"] is None
        assert row["unavailable_reason"] == "insufficient_observations"
        assert row["n_obs"] == 2


def _sector_inputs() -> MetricInputs:
    contributions = [
        _contribution("i1", "a1", "to_exit", date(2024, 1, 10), 1.0, 0.30, 0.30),
        _contribution("i2", "a1", "to_exit", date(2024, 2, 10), 1.0, 0.30, 0.30),
        _contribution("i3", "a1", "to_exit", date(2024, 3, 10), 1.0, 0.30, 0.30),
        _contribution("i4", "a1", "to_exit", date(2024, 4, 10), 1.0, 0.10, 0.10),
        _contribution("i5", "a1", "to_exit", date(2024, 5, 10), 1.0, 0.10, 0.10),
        _contribution("i6", "a1", "to_exit", date(2024, 6, 10), 1.0, 0.10, 0.10),
    ]
    intervals = pl.DataFrame(
        {
            "interval_id": ["i1", "i2", "i3", "i4", "i5", "i6"],
            "security_id": ["SEC_A"] * 3 + ["SEC_B"] * 3,
        }
    )
    classifications = pl.DataFrame(
        [
            {
                "security_id": "SEC_A",
                "from_date": date(2020, 1, 1),
                "to_date": None,
                "exchange_id": "XNYS",
                "currency": "USD",
                "sector_id": "TECH",
                "region": "US",
                "cap_bucket": "LARGE",
                "listing_date": date(2010, 1, 1),
                "delisting_date": None,
            },
            {
                "security_id": "SEC_B",
                "from_date": date(2020, 1, 1),
                "to_date": None,
                "exchange_id": "XNYS",
                "currency": "USD",
                "sector_id": "ENER",
                "region": "US",
                "cap_bucket": "LARGE",
                "listing_date": date(2010, 1, 1),
                "delisting_date": None,
            },
        ]
    )
    return MetricInputs(
        contributions=_contributions_frame(contributions),
        intervals=intervals,
        security_classifications=classifications,
    )


def test_sector_dispersion() -> None:
    out = compute_metrics(_sector_inputs(), ["a1"], Methodology(), _context())
    row = _value(out, "a1", "sector_dispersion")
    # Sector means 0.30 (TECH) and 0.10 (ENER); sample std of two values.
    assert row["value"] == pytest.approx(statistics.stdev([0.30, 0.10]))
    assert row["n_obs"] == 2


def test_sector_dispersion_requires_three_per_sector() -> None:
    inputs = _sector_inputs()
    contributions = inputs["contributions"].filter(pl.col("interval_id") != "i3")
    intervals = inputs["intervals"].filter(pl.col("interval_id") != "i3")
    out = compute_metrics(
        MetricInputs(
            contributions=contributions,
            intervals=intervals,
            security_classifications=inputs["security_classifications"],
        ),
        ["a1"],
        Methodology(),
        _context(),
    )
    row = _value(out, "a1", "sector_dispersion")
    assert row["value"] is None
    assert row["unavailable_reason"] == "insufficient_observations"


def _benchmark_rows() -> pl.DataFrame:
    rows = []
    # Q1-2024 up: 100 -> 110; Q2-2024 down: 110 -> 100; Q3-2024 up: 100 -> 105.
    spec = [
        (date(2024, 1, 2), 100.0),
        (date(2024, 3, 28), 110.0),
        (date(2024, 4, 1), 110.0),
        (date(2024, 6, 28), 100.0),
        (date(2024, 7, 1), 100.0),
        (date(2024, 9, 30), 105.0),
    ]
    for day, close in spec:
        rows.append(
            {
                "benchmark_id": "IDX_BROAD",
                "session_date": day,
                "open_level": close,
                "close_level": close,
            }
        )
    return pl.DataFrame(rows)


def test_regime_spread() -> None:
    contributions = [
        _contribution("r1", "a1", "to_exit", date(2024, 1, 10), 1.0, 0.10, 0.10),
        _contribution("r2", "a1", "to_exit", date(2024, 2, 10), 1.0, 0.20, 0.20),
        _contribution("r3", "a1", "to_exit", date(2024, 4, 10), 1.0, 0.05, 0.05),
        _contribution("r4", "a1", "to_exit", date(2024, 7, 10), 1.0, 0.30, 0.30),
    ]
    out = compute_metrics(
        MetricInputs(
            contributions=_contributions_frame(contributions),
            benchmark_daily=_benchmark_rows(),
        ),
        ["a1"],
        Methodology(),
        _context(),
    )
    row = _value(out, "a1", "regime_spread")
    # Up quarters (Q1, Q3): mean(0.10, 0.20, 0.30) = 0.20; down (Q2): 0.05.
    assert row["value"] == pytest.approx(0.15)
    assert row["n_obs"] == 3


def test_regime_spread_single_regime_unavailable() -> None:
    contributions = [
        _contribution("r1", "a1", "to_exit", date(2024, 1, 10), 1.0, 0.10, 0.10),
        _contribution("r2", "a1", "to_exit", date(2024, 2, 10), 1.0, 0.20, 0.20),
        _contribution("r3", "a1", "to_exit", date(2024, 7, 10), 1.0, 0.30, 0.30),
    ]
    out = compute_metrics(
        MetricInputs(
            contributions=_contributions_frame(contributions),
            benchmark_daily=_benchmark_rows(),
        ),
        ["a1"],
        Methodology(),
        _context(),
    )
    row = _value(out, "a1", "regime_spread")
    assert row["value"] is None
    assert row["unavailable_reason"] == "missing_regime"
    assert row["n_obs"] == 2
