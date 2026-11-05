"""Tests for rating-event, scale and oracle-analyst generation."""

from datetime import date

import polars as pl
import pytest

from callscore_core.enums import CanonicalRating, RatingAction
from callscore_core.frames import RATING_EVENT_FRAME
from callscore_synth import market, ratings
from callscore_synth.profiles import Profile

PROFILE = Profile(
    name="tinyrate",
    seed=11,
    securities=30,
    analysts=8,
    firms=3,
    start_date=date(2024, 1, 1),
    end_date=date(2025, 12, 31),
    target_sessions=None,
)

_DTYPE = {
    "String": pl.String,
    "Date": pl.Date,
    "Datetime[us, UTC]": pl.Datetime("us", "UTC"),
    "Float64": pl.Float64,
    "Int64": pl.Int64,
    "Boolean": pl.Boolean,
}


def _generate(seed: int = 11) -> dict[str, pl.DataFrame]:
    data = market.generate(seed=seed, profile=PROFILE)
    frames = dict(data.frames)
    frames.update(ratings.generate(frames, PROFILE, seed))
    return frames


@pytest.fixture(scope="module")
def frames() -> dict[str, pl.DataFrame]:
    return _generate()


def test_rating_events_contract_shape(frames: dict[str, pl.DataFrame]) -> None:
    ev = frames["rating_events"]
    contract = RATING_EVENT_FRAME
    assert ev.columns == list(contract.column_names)
    for col in contract.columns:
        assert ev.schema[col.name] == _DTYPE[col.dtype], col.name
    assert ev.height > 100
    # Sorted by contract sort keys.
    assert ev.select(list(contract.sort_by)).equals(
        ev.select(list(contract.sort_by)).sort(list(contract.sort_by))
    )


def test_event_ids_unique_and_corrections_reference_base(frames: dict[str, pl.DataFrame]) -> None:
    ev = frames["rating_events"]
    ids = ev["event_id"].to_list()
    assert len(ids) == len(set(ids))
    corr = ev.filter(pl.col("is_correction"))
    assert corr.height > 0
    base_ids = set(ev.filter(~pl.col("is_correction"))["event_id"].to_list())
    for sup in corr["supersedes_event_id"].to_list():
        assert sup in base_ids
    # Corrections never supersede another correction in this generator.
    assert corr["supersedes_event_id"].is_not_null().all()


def test_enums_and_prior_consistency(frames: dict[str, pl.DataFrame]) -> None:
    ev = frames["rating_events"]
    assert set(ev["action_type"].unique().to_list()) <= {a.value for a in RatingAction}
    assert set(ev["canonical_rating"].unique().to_list()) <= {c.value for c in CanonicalRating}
    # Initiations have no prior; every other action has one. Corrections are
    # exempt: a correction's prior is the canonical it corrects.
    base = ev.filter(~pl.col("is_correction"))
    init = base.filter(pl.col("action_type") == "initiation")
    assert init["prior_canonical_rating"].null_count() == init.height
    non_init = base.filter(pl.col("action_type") != "initiation")
    assert non_init["prior_canonical_rating"].null_count() == 0
    # Drops end at not_rated and carry no expiry.
    drops = ev.filter(pl.col("action_type") == "drop")
    assert drops.height > 0
    assert (drops["canonical_rating"] == "not_rated").all()
    assert drops["expiry_date"].null_count() == drops.height


def test_native_ratings_match_scale_map(frames: dict[str, pl.DataFrame]) -> None:
    ev = frames["rating_events"]
    maps = frames["scale_maps"]
    scales = frames["rating_scales"]
    # canonical -> native label per (scale_id, effective_from version).
    label_of: dict[tuple[str, str, str], str] = {}
    for row in maps.iter_rows(named=True):
        label_of[(row["scale_id"], str(row["canonical_rating"]), row["native_label"])] = row[
            "native_label"
        ]
    native_by_scale: dict[tuple[str, str], set[str]] = {}
    for row in maps.iter_rows(named=True):
        native_by_scale.setdefault((row["scale_id"], row["canonical_rating"]), set()).add(
            row["native_label"]
        )
    versions = {
        (r["scale_id"], r["effective_from"]): (r["effective_to"], r["firm_id"])
        for r in scales.iter_rows(named=True)
    }
    firm_of_analyst = {
        r["analyst_id"]: r["firm_id"] for r in frames["analysts"].iter_rows(named=True)
    }
    scale_by_firm = {r["firm_id"]: r["scale_id"] for r in scales.iter_rows(named=True)}
    checked = 0
    for row in ev.iter_rows(named=True):
        firm = firm_of_analyst[row["analyst_id"]]
        scale_id = scale_by_firm[firm]
        day = row["announced_ts"].date()
        # Resolve the scale version effective on the announced date.
        eff_versions = [
            (ef, et)
            for (sid, ef), (et, _f) in versions.items()
            if sid == scale_id and ef <= day and (et is None or day <= et)
        ]
        assert eff_versions, (scale_id, day)
        eff_from = max(eff_versions)[0]
        allowed = {
            m["native_label"]
            for m in maps.iter_rows(named=True)
            if m["scale_id"] == scale_id
            and m["effective_from"] == eff_from
            and m["canonical_rating"] == row["canonical_rating"]
        }
        assert row["native_rating"] in allowed, (row["event_id"], row["native_rating"])
        checked += 1
    assert checked == ev.height
    assert label_of  # silence unused


def test_timestamps_utc_and_ordered(frames: dict[str, pl.DataFrame]) -> None:
    ev = frames["rating_events"]
    for col in ("announced_ts", "effective_ts", "knowledge_ts"):
        vals = ev[col].to_list()
        assert all(v.tzinfo is not None for v in vals)
    # knowledge >= announced always (validation warns otherwise).
    assert (
        ev.select((pl.col("knowledge_ts") >= pl.col("announced_ts")).all().alias("ok")).item()
        is True
    )
    # A fraction has meaningful ingestion lag.
    lag = (
        ev.select((pl.col("knowledge_ts") - pl.col("announced_ts")).dt.total_hours())
        .to_series()
        .to_list()
    )
    assert sum(1 for h in lag if h > 1.0) > 0
    # Expiry ~3-12 months after effective for non-drop events.
    dated = ev.filter(pl.col("expiry_date").is_not_null())
    assert dated.height > 0
    deltas = [
        (row["expiry_date"] - row["effective_ts"].date()).days
        for row in dated.iter_rows(named=True)
    ]
    assert all(60 <= d <= 400 for d in deltas)


def test_pt_change_carries_target_fields(frames: dict[str, pl.DataFrame]) -> None:
    ev = frames["rating_events"]
    pt_events = ev.filter(pl.col("action_type") == "pt_change")
    assert pt_events.height > 0
    base_pt = pt_events.filter(~pl.col("is_correction"))
    assert base_pt["price_target"].null_count() == 0
    assert pt_events["pt_currency"].null_count() == 0
    assert pt_events["pt_horizon_months"].null_count() == 0
    assert set(pt_events["pt_horizon_months"].unique().to_list()) <= {3, 6, 12}
    assert (pt_events["price_target"] > 0).all()


def test_oracle_analysts_present_with_coverage(frames: dict[str, pl.DataFrame]) -> None:
    for aid in ratings.ORACLE_ANALYST_IDS:
        assert aid in set(frames["analysts"]["analyst_id"].to_list())
        assert aid in set(frames["coverage_spells"]["analyst_id"].to_list())
        n_events = frames["rating_events"].filter(pl.col("analyst_id") == aid).height
        assert n_events > 0, aid
    # The four active oracles + herder have plenty of observations.
    for aid in ("A_ORACLE", "A_INVERSE", "A_NOISE", "A_HERDER"):
        n = frames["rating_events"].filter(pl.col("analyst_id") == aid).height
        assert n >= 20, (aid, n)
    # A_LUCKY stays below min_ratings_for_rank (20).
    n_lucky = frames["rating_events"].filter(pl.col("analyst_id") == "A_LUCKY").height
    assert 0 < n_lucky < 20


def test_oracle_inverse_separation(frames: dict[str, pl.DataFrame]) -> None:
    """Oracle buys outperformers / sells underperformers; inverse flips."""
    ev = frames["rating_events"]
    daily = frames["security_daily"]
    bench = frames["benchmark_daily"].filter(pl.col("benchmark_id") == "IDX_BROAD")

    tr = {
        (r[0], r[1]): (r[2], r[3])
        for r in daily.select("security_id", "session_date", "tr_index", "close").iter_rows()
    }
    sec_dates: dict[str, list[date]] = {}
    for r in daily.select("security_id", "session_date").iter_rows():
        sec_dates.setdefault(r[0], []).append(r[1])
    bclose = {r[0]: r[1] for r in bench.select("session_date", "close_level").iter_rows()}

    weight = {"strong_buy": 1.0, "buy": 0.5, "hold": 0.0, "sell": -0.5, "strong_sell": -1.0}

    def fwd_excess(sid: str, day: date) -> float | None:
        dates = sec_dates.get(sid)
        if not dates or (sid, day) not in tr:
            return None
        i = dates.index(day)
        j = min(i + ratings.SKILL_HORIZON_SESSIONS, len(dates) - 1)
        if j <= i:
            return None
        t0, t1 = tr[(sid, dates[i])][0], tr[(sid, dates[j])][0]
        b0, b1 = bclose.get(dates[i]), bclose.get(dates[j])
        if not b0 or not b1:
            return None
        return (t1 / t0 - 1.0) - (b1 / b0 - 1.0)

    def directional_score(aid: str) -> float:
        rows = ev.filter(
            (pl.col("analyst_id") == aid) & pl.col("canonical_rating").is_in(list(weight))
        ).iter_rows(named=True)
        vals = []
        for r in rows:
            day = r["announced_ts"].date()
            ex = fwd_excess(r["security_id"], day)
            if ex is not None:
                vals.append(weight[r["canonical_rating"]] * ex)
        return sum(vals) / len(vals) if vals else 0.0

    oracle_score = directional_score("A_ORACLE")
    inverse_score = directional_score("A_INVERSE")
    noise_score = directional_score("A_NOISE")
    assert oracle_score > 0.002, oracle_score
    assert inverse_score < -0.002, inverse_score
    assert abs(noise_score) < abs(oracle_score)


def test_determinism(frames: dict[str, pl.DataFrame]) -> None:
    again = _generate()
    for key in ("rating_events", "rating_scales", "scale_maps"):
        assert frames[key].equals(again[key]), key


def test_different_seed_differs(frames: dict[str, pl.DataFrame]) -> None:
    other = _generate(seed=22)
    assert not frames["rating_events"].equals(other["rating_events"])


def test_scale_versions_effective_dated(frames: dict[str, pl.DataFrame]) -> None:
    scales = frames["rating_scales"]
    assert scales.height >= frames["firms"].height
    # PK (scale_id, effective_from) unique.
    assert scales.select("scale_id", "effective_from").n_unique() == scales.height
    maps = frames["scale_maps"]
    # Every scale version has a complete map incl. admin canonicals.
    for _key, grp in maps.group_by("scale_id", "effective_from"):
        canon = set(grp["canonical_rating"].to_list())
        assert {"strong_buy", "buy", "hold", "sell", "strong_sell"} <= canon or (
            {"buy", "hold", "sell"} <= canon
        )
        assert {"under_review", "restricted", "not_rated"} <= canon
