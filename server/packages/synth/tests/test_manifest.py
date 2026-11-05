"""Tests for the generation manifest."""

from datetime import date

import polars as pl

from callscore_synth.manifest import frame_checksum, generation_manifest
from callscore_synth.profiles import Profile

PROFILE = Profile(
    name="tiny",
    seed=3,
    securities=5,
    analysts=2,
    firms=1,
    start_date=date(2024, 1, 1),
    end_date=date(2024, 12, 31),
    target_sessions=None,
)


def _frames() -> dict[str, pl.DataFrame]:
    return {
        "a": pl.DataFrame({"x": [2, 1], "y": ["b", "a"]}),
        "b": pl.DataFrame({"z": [1.5, None]}),
    }


def test_manifest_deterministic() -> None:
    m1 = generation_manifest(3, PROFILE, _frames())
    m2 = generation_manifest(3, PROFILE, _frames())
    assert m1 == m2
    assert m1["checksum"] == m2["checksum"]


def test_manifest_seed_and_config_echo() -> None:
    m = generation_manifest(42, PROFILE, _frames())
    assert m["seed"] == 42
    assert m["profile"] == "tiny"
    cfg = m["config"]
    assert cfg["securities"] == 5
    assert cfg["start_date"] == "2024-01-01"
    # Timestamp is deterministic (profile end date).
    assert m["timestamp"].startswith("2024-12-31")


def test_manifest_frame_counts_and_checksums() -> None:
    m = generation_manifest(1, PROFILE, _frames())
    assert m["frames"]["a"]["rows"] == 2
    assert m["frames"]["b"]["rows"] == 2
    assert len(m["frames"]["a"]["checksum"]) == 64
    # Row order does not affect the checksum.
    reordered = {"a": _frames()["a"].reverse(), "b": _frames()["b"]}
    assert (
        generation_manifest(1, PROFILE, reordered)["frames"]["a"]["checksum"]
        == (m["frames"]["a"]["checksum"])
    )


def test_frame_checksum_changes_with_data() -> None:
    f1 = pl.DataFrame({"x": [1]})
    f2 = pl.DataFrame({"x": [2]})
    assert frame_checksum(f1) != frame_checksum(f2)
