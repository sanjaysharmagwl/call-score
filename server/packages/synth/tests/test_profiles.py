"""Tests for profile loading/normalization (``years`` vs ``trading_days``)."""

from datetime import date

import pytest

from callscore_synth.profiles import list_profiles, load_profile


def test_committed_profiles_load() -> None:
    names = list_profiles()
    assert {"small", "medium", "full"} <= set(names)
    for name in ("small", "medium", "full"):
        profile = load_profile(name)
        assert profile.name == name
        assert profile.seed > 0
        assert profile.securities > 0
        assert profile.analysts > 0
        assert profile.firms > 0
        assert profile.start_date < profile.end_date


def test_years_normalizes_to_target_sessions() -> None:
    small = load_profile("small")
    medium = load_profile("medium")
    assert small.target_sessions == 2 * 252
    assert medium.target_sessions == 5 * 252


def test_trading_days_passes_through() -> None:
    full = load_profile("full")
    assert full.target_sessions == 2520


def test_actual_dates_authoritative() -> None:
    # ``years`` is only a target hint; the calendar comes from start/end dates.
    profile = load_profile("small")
    assert profile.start_date == date(2024, 1, 1)
    assert profile.end_date == date(2025, 12, 31)


def test_missing_profile_raises() -> None:
    with pytest.raises(FileNotFoundError):
        load_profile("does-not-exist")


def test_profile_is_frozen() -> None:
    profile = load_profile("small")
    with pytest.raises(AttributeError):
        profile.seed = 0  # type: ignore[misc]
