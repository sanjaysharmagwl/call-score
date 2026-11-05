"""Tests for ``callscore_engine.composite.compute_composites`` (hand-built frames)."""

import math

import polars as pl
import pytest

from callscore_core.enums import MetricFamily
from callscore_core.frames import COMPOSITE_FRAME
from callscore_core.methodology import Methodology
from callscore_core.metrics import CompositeSpec, MetricDefinition
from callscore_engine.composite import compute_composites

_DTYPES = {
    "String": pl.String(),
    "Date": pl.Date(),
    "Datetime[us, UTC]": pl.Datetime("us", "UTC"),
    "Float64": pl.Float64(),
    "Int64": pl.Int64(),
    "Boolean": pl.Boolean(),
}

RESULTS_SCHEMA = {
    "analyst_id": pl.String(),
    "metric_code": pl.String(),
    "value": pl.Float64(),
    "shrunk_value": pl.Float64(),
}
PEER_SCHEMA = {"analyst_id": pl.String(), "peer_group": pl.String()}
N_INVESTABLE_SCHEMA = {"analyst_id": pl.String(), "n_obs": pl.Int64()}

MC = "mean_contribution"  # return_skill, higher, in_composite, weight 0.40
HIT = "hit_rate"  # return_skill, higher, in_composite
DRIFT = "revision_drift_20d"  # timing, higher, in_composite, weight 0.15
STALE = "stale_coverage"  # behavior, lower, in_composite, weight 0.10
PQ = "positive_quarter_fraction"  # consistency, higher, in_composite, weight 0.20
PT = "target_hit_rate"  # price_target, higher, in_composite, weight 0.15
IC = "information_coefficient"  # return_skill, higher, in_composite


def _methodology(**overrides: object) -> Methodology:
    kwargs: dict[str, object] = {"min_ratings_for_rank": 1, "min_peer_size": 2}
    kwargs.update(overrides)
    return Methodology.model_validate(kwargs)


def _results(
    rows: list[tuple[str, str, float | None, float | None]],
) -> pl.DataFrame:
    return pl.DataFrame(
        {
            "analyst_id": [r[0] for r in rows],
            "metric_code": [r[1] for r in rows],
            "value": [r[2] for r in rows],
            "shrunk_value": [r[3] for r in rows],
        },
        schema=RESULTS_SCHEMA,
    )


def _peers(pairs: list[tuple[str, str | None]]) -> pl.DataFrame:
    return pl.DataFrame(
        {"analyst_id": [p[0] for p in pairs], "peer_group": [p[1] for p in pairs]},
        schema=PEER_SCHEMA,
    )


def _n_investable(pairs: list[tuple[str, int]]) -> pl.DataFrame:
    return pl.DataFrame(
        {"analyst_id": [p[0] for p in pairs], "n_obs": [p[1] for p in pairs]},
        schema=N_INVESTABLE_SCHEMA,
    )


def _row(frame: pl.DataFrame, analyst: str) -> dict[str, object]:
    out = frame.filter(pl.col("analyst_id") == analyst).to_dicts()
    assert len(out) == 1
    return out[0]


def _normal_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def test_output_matches_composite_contract() -> None:
    rows = [
        ("a1", MC, 0.1, None),
        ("a2", MC, 0.0, None),
        ("a1", STALE, 0.2, None),
        ("a2", STALE, 0.3, None),
        ("a1", PQ, 0.6, None),
        ("a2", PQ, 0.4, None),
    ]
    out = compute_composites(
        _results(rows),
        _peers([("a1", "g1"), ("a2", "g1")]),
        _n_investable([("a1", 30), ("a2", 30)]),
        _methodology(),
    )
    expected = [c.name for c in COMPOSITE_FRAME.columns]
    assert out.columns == expected
    for column in COMPOSITE_FRAME.columns:
        assert out.schema[column.name] == _DTYPES[column.dtype], column.name
    assert out.sort("analyst_id").equals(out)


def test_hand_computed_composite_and_ranks() -> None:
    # Three analysts, one peer group, three scored families (weights .40/.10/.20
    # renormalized over 0.70). x = value (shrunk_value null).
    #
    # mean_contribution z (higher): values .10/.05/-.05, mean .033333,
    #   std .076376 -> z = .872872 / .218218 / -1.091089
    # stale_coverage z (lower): .20/.10/.30, mean .20, std .10 -> raw z =
    #   0 / -1 / 1, oriented x-1 -> 0 / 1 / -1
    # positive_quarter_fraction z (higher): .60/.50/.40, mean .50, std .10 ->
    #   1 / 0 / -1
    #
    # c = (w1*z1 + w2*z2 + w3*z3)/0.70:
    #   p1 = (.4*.872872 + .2*1)/.7      = .784499
    #   p2 = (.4*.218218 + .1*1)/.7      = .267553
    #   p3 = (.4*-1.091089 - .1 - .2)/.7 = -1.052051
    # s = std(c, ddof=1) = .947058; score = 100*Phi(c/s).
    analysts = ["p1", "p2", "p3"]
    metric_values = {
        MC: [0.10, 0.05, -0.05],
        STALE: [0.20, 0.10, 0.30],
        PQ: [0.60, 0.50, 0.40],
    }
    rows = [
        (a, code, v, None)
        for code, vals in metric_values.items()
        for a, v in zip(analysts, vals, strict=True)
    ]
    out = compute_composites(
        _results(rows),
        _peers([(a, "g1") for a in analysts]),
        _n_investable([(a, 30) for a in analysts]),
        _methodology(),
    )
    # c = (w1*z1 + w2*z2 + w3*z3)/0.70, recomputed exactly:
    #   mc z = (v - 1/30) / sqrt(7/1200); stale oriented z = (0.25-v)/0.1;
    #   pq z = (v - 0.5) / 0.1.
    mc_vals = [0.10, 0.05, -0.05]
    mc_mean = sum(mc_vals) / 3
    mc_std = math.sqrt(sum((v - mc_mean) ** 2 for v in mc_vals) / 2)
    expected_c = {}
    for a, mc, sc, pq in zip(
        analysts, mc_vals, [0.20, 0.10, 0.30], [0.60, 0.50, 0.40], strict=True
    ):
        z_mc = (mc - mc_mean) / mc_std
        z_sc = -(sc - 0.20) / 0.1  # lower is better
        z_pq = (pq - 0.50) / 0.1
        expected_c[a] = (0.40 * z_mc + 0.10 * z_sc + 0.20 * z_pq) / 0.70
    cs = [expected_c[a] for a in analysts]
    s = math.sqrt(sum((c - sum(cs) / 3) ** 2 for c in cs) / 2)
    for a in analysts:
        row = _row(out, a)
        assert row["is_ranked"] is True
        assert row["unranked_reason"] is None
        assert row["composite_score"] == pytest.approx(
            100.0 * _normal_cdf(expected_c[a] / s), abs=1e-6
        )
        assert row["peer_group"] == "g1"
    scores = [float(_row(out, a)["composite_score"]) for a in analysts]  # type: ignore[arg-type]
    assert scores == pytest.approx(
        [100.0 * _normal_cdf(expected_c[a] / s) for a in analysts], abs=1e-6
    )
    assert _row(out, "p1")["rank_overall"] == 1
    assert _row(out, "p2")["rank_overall"] == 2
    assert _row(out, "p3")["rank_overall"] == 3
    assert [_row(out, a)["rank_in_peer_group"] for a in analysts] == [1, 2, 3]


def test_unranked_reasons() -> None:
    # Default spec needs 3 scored families; every analyst below has only MC.
    methodology = _methodology(min_ratings_for_rank=5)
    rows = [
        ("r1", MC, 0.1, None),  # only 1 family -> insufficient_families
        ("r2", MC, 0.2, None),
        ("r3", MC, 0.3, None),
        ("r4", MC, 0.4, None),
        ("r5", MC, 0.5, None),
        ("r6", MC, 0.6, None),
    ]
    peers = _peers(
        [
            ("r1", "g1"),
            ("r2", "g1"),
            ("r3", "g1"),
            ("r4", "g_small"),  # alone in its group
            ("r5", None),  # no peer group
            ("r6", "g1"),
        ]
    )
    investable = _n_investable(
        [("r1", 30), ("r2", 30), ("r3", 30), ("r4", 30), ("r5", 30), ("r6", 0)]
    )
    out = compute_composites(_results(rows), peers, investable, methodology)
    assert _row(out, "r6")["unranked_reason"] == "insufficient_ratings"
    assert _row(out, "r5")["unranked_reason"] == "no_peer_group"
    assert _row(out, "r4")["unranked_reason"] == "peer_group_too_small"
    # r1..r3 all have only one scored family -> insufficient_families.
    for a in ("r1", "r2", "r3"):
        row = _row(out, a)
        assert row["unranked_reason"] == "insufficient_families"
        assert row["is_ranked"] is False
        assert row["rank_overall"] is None
        assert row["rank_in_peer_group"] is None
        # Still gets a composite_score (one scored family available).
        assert row["composite_score"] is not None


def test_peer_group_too_small_counts_only_base_eligible() -> None:
    # g1 has r1 (eligible) + r2 (insufficient ratings): eligible size is 1.
    methodology = _methodology(min_ratings_for_rank=5, min_peer_size=2)
    rows = [
        ("r1", MC, 0.1, None),
        ("r2", MC, 0.2, None),
        ("r3", MC, 0.3, None),
        ("r4", MC, 0.4, None),
    ]
    peers = _peers([("r1", "g1"), ("r2", "g1"), ("r3", "g2"), ("r4", "g2")])
    investable = _n_investable([("r1", 30), ("r2", 0), ("r3", 30), ("r4", 30)])
    out = compute_composites(_results(rows), peers, investable, methodology)
    assert _row(out, "r1")["unranked_reason"] == "peer_group_too_small"
    assert _row(out, "r2")["unranked_reason"] == "insufficient_ratings"
    # g2 has two eligible members but each has only one family.
    assert _row(out, "r3")["unranked_reason"] == "insufficient_families"
    assert _row(out, "r4")["unranked_reason"] == "insufficient_families"


def test_missing_family_renormalizes_weights() -> None:
    # Custom two-metric catalog: m1 in return_skill (0.40), m2 in timing (0.15).
    def make(code: str, family: MetricFamily) -> MetricDefinition:
        return MetricDefinition.model_validate(
            {
                "code": code,
                "family": family,
                "unit": "ratio",
                "direction": "higher",
                "horizon": "none",
                "required_inputs": ("contributions",),
                "formula": "test",
                "estimand": "test",
                "unavailable_when": "test",
                "minimum_observations": 1,
                "in_composite": True,
                "inference": "none",
                "shrink": False,
                "aggregate": "none",
            }
        )

    catalog = (make("m_one", MetricFamily.RETURN_SKILL), make("m_two", MetricFamily.TIMING))
    spec = CompositeSpec(min_scored_families=1)
    # a2 lacks m_two entirely -> only the return_skill family.
    # m_one x: a1=1.0, a2=0.0, a3=0.0 -> mean 1/3, std 1/sqrt(3)
    #   z = 2/sqrt(3) / -1/sqrt(3) / -1/sqrt(3)  =  1.154701 / -.577350 / -.577350
    # m_two x: a1=3.0, a3=0.0 (a2 absent) -> mean 1.5, std 1.5 -> z = 1.0 / -1.0
    rows = [
        ("a1", "m_one", 1.0, None),
        ("a2", "m_one", 0.0, None),
        ("a3", "m_one", 0.0, None),
        ("a1", "m_two", 3.0, None),
        ("a3", "m_two", 0.0, None),
    ]
    out = compute_composites(
        _results(rows),
        _peers([("a1", "g1"), ("a2", "g1"), ("a3", "g1")]),
        _n_investable([("a1", 30), ("a2", 30), ("a3", 30)]),
        _methodology(),
        spec=spec,
        catalog=catalog,
    )
    # c = sum(w_f * family_score) / sum(w_f over present families):
    #   a1: (.4*1.154701 + .15*0.707107)/.55 = 1.032630
    #   a2: .4*(-.577350)/.4               = -.577350   (weights renormalize to 1)
    #   a3: (.4*(-.577350) + .15*(-0.707107))/.55 = -.612738
    z_hi = 2.0 / math.sqrt(3.0)
    z_lo = -1.0 / math.sqrt(3.0)
    z_pair = 1.0 / math.sqrt(2.0)  # ddof=1 std of two points 3 apart is 3/sqrt(2)
    expected_c = {
        "a1": (0.4 * z_hi + 0.15 * z_pair) / 0.55,
        "a2": z_lo,
        "a3": (0.4 * z_lo - 0.15 * z_pair) / 0.55,
    }
    cs = [expected_c[a] for a in ("a1", "a2", "a3")]
    mean_c = sum(cs) / 3
    s = math.sqrt(sum((c - mean_c) ** 2 for c in cs) / 2)
    for a in ("a1", "a2", "a3"):
        assert _row(out, a)["composite_score"] == pytest.approx(
            100.0 * _normal_cdf(expected_c[a] / s), abs=1e-9
        )
        assert _row(out, a)["is_ranked"] is True  # >= min_scored_families=1


def _shrunk_fixture(shrunk: float | None) -> pl.DataFrame:
    rows = [
        ("a1", MC, 0.9, shrunk),
        ("a2", MC, 0.0, 0.0),
        ("a3", MC, 0.5, 0.5),
        ("a1", STALE, 0.2, None),
        ("a2", STALE, 0.3, None),
        ("a3", STALE, 0.25, None),
        ("a1", PQ, 0.6, None),
        ("a2", PQ, 0.4, None),
        ("a3", PQ, 0.5, None),
    ]
    return compute_composites(
        _results(rows),
        _peers([("a1", "g1"), ("a2", "g1"), ("a3", "g1")]),
        _n_investable([("a1", 30), ("a2", 30), ("a3", 30)]),
        _methodology(),
    )


def test_shrunk_value_preferred_over_raw() -> None:
    # With shrunk_value present, x = .1; without it x = .9. The mc z-scores
    # (and hence a1's composite) must differ between the two runs.
    shrunk_run = _shrunk_fixture(0.1)
    raw_run = _shrunk_fixture(None)
    assert _row(shrunk_run, "a1")["composite_score"] != _row(raw_run, "a1")["composite_score"]
    # a2 and a3 have identical x in both runs but their z's shift with a1's x.
    assert _row(shrunk_run, "a2")["composite_score"] != _row(raw_run, "a2")["composite_score"]


def test_tie_break_by_analyst_id_and_degenerate_cohort() -> None:
    # Identical metric values -> identical c -> cohort std 0 -> score 50,
    # ranks break ties by analyst_id ascending.
    rows = [
        ("b1", MC, 0.1, None),
        ("a1", MC, 0.1, None),
        ("b1", STALE, 0.2, None),
        ("a1", STALE, 0.2, None),
        ("b1", PQ, 0.5, None),
        ("a1", PQ, 0.5, None),
    ]
    out = compute_composites(
        _results(rows),
        _peers([("a1", "g1"), ("b1", "g1")]),
        _n_investable([("a1", 30), ("b1", 30)]),
        _methodology(),
    )
    for a in ("a1", "b1"):
        row = _row(out, a)
        assert row["is_ranked"] is True
        assert row["composite_score"] == 50.0
    assert _row(out, "a1")["rank_overall"] == 1
    assert _row(out, "b1")["rank_overall"] == 2
    assert _row(out, "a1")["rank_in_peer_group"] == 1
    assert _row(out, "b1")["rank_in_peer_group"] == 2


def test_row_order_invariance() -> None:
    rows = [
        ("a1", MC, 0.1, None),
        ("a2", MC, 0.0, None),
        ("a3", MC, -0.1, None),
        ("a1", STALE, 0.2, None),
        ("a2", STALE, 0.3, None),
        ("a3", STALE, 0.1, None),
        ("a1", PQ, 0.6, None),
        ("a2", PQ, 0.5, None),
        ("a3", PQ, 0.4, None),
    ]
    peers = [("a1", "g1"), ("a2", "g1"), ("a3", "g1")]
    investable = [("a1", 30), ("a2", 30), ("a3", 30)]
    methodology = _methodology()
    forward = compute_composites(
        _results(rows), _peers(peers), _n_investable(investable), methodology
    )
    backward = compute_composites(
        _results(list(reversed(rows))),
        _peers(list(reversed(peers))),
        _n_investable(list(reversed(investable))),
        methodology,
    )
    assert forward.equals(backward)


def test_unavailable_metric_value_is_skipped() -> None:
    # a2: MC unavailable and only STALE + DRIFT available -> 2 families < 3.
    rows = [
        ("a1", MC, 0.1, None),
        ("a2", MC, None, None),
        ("a1", STALE, 0.2, None),
        ("a2", STALE, 0.3, None),
        ("a1", PQ, 0.6, None),
        ("a1", DRIFT, 0.01, None),
        ("a2", DRIFT, 0.02, None),
        ("a1", PT, 0.5, None),
    ]
    out = compute_composites(
        _results(rows),
        _peers([("a1", "g1"), ("a2", "g1")]),
        _n_investable([("a1", 30), ("a2", 30)]),
        _methodology(),
    )
    assert _row(out, "a1")["is_ranked"] is True  # 5 families
    row2 = _row(out, "a2")
    assert row2["is_ranked"] is False
    assert row2["unranked_reason"] == "insufficient_families"
    assert row2["rank_overall"] is None
    assert row2["composite_score"] is not None  # still scored on 2 families
