"""Tests for reference-data generation: calendar, sectors, spells."""

from datetime import UTC, date, datetime

import numpy as np
import polars as pl

from callscore_synth import reference
from callscore_synth.profiles import Profile
from callscore_synth.reference import SecuritySpec

PROFILE = Profile(
    name="tiny",
    seed=11,
    securities=30,
    analysts=8,
    firms=3,
    start_date=date(2024, 1, 1),
    end_date=date(2025, 12, 31),
    target_sessions=None,
)


def _specs() -> list[SecuritySpec]:
    r = reference.rng(PROFILE.seed, 10 + reference.profile_seed_offset(PROFILE.name))
    return reference.security_specs(PROFILE, r, reference.session_dates(PROFILE))


def test_sessions_are_weekdays_without_nyse_holidays() -> None:
    dates = reference.session_dates(PROFILE)
    assert dates
    assert all(d.weekday() < 5 for d in dates)
    holiday_samples = {
        date(2024, 1, 1),  # New Year
        date(2024, 1, 15),  # MLK
        date(2024, 2, 19),  # Presidents
        date(2024, 3, 29),  # Good Friday
        date(2024, 5, 27),  # Memorial
        date(2024, 6, 19),  # Juneteenth
        date(2024, 7, 4),  # Independence Day
        date(2024, 9, 2),  # Labor
        date(2024, 11, 28),  # Thanksgiving
        date(2024, 12, 25),  # Christmas
    }
    assert not holiday_samples & set(dates)
    # Sanity: ~252 sessions/year.
    assert 480 <= len(dates) <= 510


def test_session_timestamps_utc_dst_aware() -> None:
    frame = reference.session_frame(PROFILE)
    winter = frame.filter(pl.col("session_date") == date(2024, 1, 2)).row(0, named=True)
    summer = frame.filter(pl.col("session_date") == date(2024, 7, 1)).row(0, named=True)
    # 09:30 ET is 14:30 UTC in winter (EST) and 13:30 UTC in summer (EDT).
    assert winter["open_ts"] == datetime(2024, 1, 2, 14, 30, tzinfo=UTC)
    assert summer["open_ts"] == datetime(2024, 7, 1, 13, 30, tzinfo=UTC)
    # 16:00 ET close -> 21:00 / 20:00 UTC.
    assert winter["close_ts"] == datetime(2024, 1, 2, 21, 0, tzinfo=UTC)
    assert summer["close_ts"] == datetime(2024, 7, 1, 20, 0, tzinfo=UTC)
    assert frame["exchange_id"].unique().to_list() == [reference.EXCHANGE_ID]


def test_sector_frame_groups_and_leaves() -> None:
    frame = reference.sector_frame()
    parents = frame.filter(pl.col("parent_sector_id").is_null())
    leaves = frame.filter(pl.col("parent_sector_id").is_not_null())
    assert parents.height == len(reference.SECTOR_GROUPS)
    assert leaves.height == len(reference.SECTOR_IDS)
    parent_ids = set(parents["sector_id"].to_list())
    assert set(leaves["parent_sector_id"].to_list()) <= parent_ids


def test_security_specs_cover_buckets_and_listings() -> None:
    specs = _specs()
    assert len(specs) == PROFILE.securities
    assert {s.cap_bucket for s in specs} <= set(reference.CAP_BUCKETS)
    assert {s.sector_id for s in specs} <= set(reference.SECTOR_IDS)
    assert all(s.listing_date <= PROFILE.end_date for s in specs)
    ids = [s.security_id for s in specs]
    assert ids == sorted(ids) and len(set(ids)) == len(ids)


def _full_reference() -> dict[str, pl.DataFrame]:
    """Build the reference frames the same way ``market.generate`` does."""
    from callscore_synth import market

    prof = PROFILE
    off = reference.profile_seed_offset(prof.name)

    def rng_for(tag: str) -> "np.random.Generator":
        return reference.rng(prof.seed, market._STREAM[tag] + off)

    dates = reference.session_dates(prof)
    specs = reference.security_specs(prof, rng_for("specs"), dates)
    firms = reference.firm_frame(prof, rng_for("firms"))
    analysts = reference.analyst_frame(prof, rng_for("analysts"), firms["firm_id"].to_list())
    employment = reference.employment_frame(prof, analysts, firms, rng_for("employment"))
    delist = market.plan_delistings(prof, specs, dates, rng_for("delistings")).frame
    classification = reference.classification_frame(prof, specs, delist, rng_for("classifications"))
    coverage = reference.coverage_frame(
        prof, specs, analysts, employment, delist, rng_for("coverage")
    )
    identifiers = reference.identifier_frame(specs, delist)
    return {
        "employment": employment,
        "classification": classification,
        "coverage": coverage,
        "identifiers": identifiers,
        "delistings": delist,
    }


def test_classification_spells_non_overlapping() -> None:
    frames = _full_reference()
    cls = frames["classification"]
    for _sid, group in cls.group_by("security_id", maintain_order=True):
        spells = group.sort("from_date")
        prev_to: date | None = None
        for row in spells.iter_rows(named=True):
            assert row["from_date"] <= (row["to_date"] or date.max)
            if prev_to is not None:
                # Next spell must start after the previous spell's inclusive end.
                assert row["from_date"] > prev_to
            prev_to = row["to_date"]
        # Exactly one open-ended spell at most, and it is last.
        open_ended = spells.filter(pl.col("to_date").is_null())
        assert open_ended.height <= 1


def test_coverage_spells_within_delisting() -> None:
    frames = _full_reference()
    delist_dates = {
        row[0]: row[1]
        for row in frames["delistings"].select("security_id", "delisting_date").iter_rows()
    }
    for row in frames["coverage"].iter_rows(named=True):
        assert row["from_date"] <= (row["to_date"] or date.max)
        dd = delist_dates.get(row["security_id"])
        if dd is not None:
            assert row["to_date"] is None or row["to_date"] <= dd


def test_employment_spells_valid() -> None:
    emp = _full_reference()["employment"]
    assert emp.height >= PROFILE.analysts
    for row in emp.iter_rows(named=True):
        assert row["to_date"] is None or row["from_date"] <= row["to_date"]
        assert row["firm_id"].startswith("FIRM_")


def test_identifiers_pit_and_unique_values() -> None:
    frames = _full_reference()
    ids = frames["identifiers"]
    values = list(ids.select("id_type", "value").iter_rows())
    # (type, value) pairs must be unique; values non-empty.
    assert len(set(values)) == len(values)
    assert all(v for _, v in values)
    # Delisted securities carry the delisting date as to_date.
    delist_dates = {
        row[0]: row[1]
        for row in frames["delistings"].select("security_id", "delisting_date").iter_rows()
    }
    for row in ids.iter_rows(named=True):
        dd = delist_dates.get(row["security_id"])
        if dd is not None:
            assert row["to_date"] == dd
