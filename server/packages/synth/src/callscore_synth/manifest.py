"""Generation manifest: deterministic per-frame checksums and run metadata."""

import hashlib
from datetime import UTC, date, datetime, time
from decimal import Decimal
from typing import Any

import polars as pl

from callscore_core.methodology import canonical_dumps
from callscore_synth.profiles import Profile


def _jsonable(value: Any) -> Any:
    """Convert a frame value to a JSON-safe primitive."""
    if value is None:
        return None
    if isinstance(value, float):
        if value != value or value in (float("inf"), float("-inf")):
            return None
        return value
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return value


def frame_checksum(frame: pl.DataFrame) -> str:
    """Canonical sha256 over a frame's rows sorted by every column."""
    ordered = frame.sort(by=list(frame.columns)) if frame.width else frame
    rows = [{k: _jsonable(v) for k, v in record.items()} for record in ordered.to_dicts()]
    return hashlib.sha256(canonical_dumps(rows).encode()).hexdigest()


def generation_manifest(
    seed: int,
    profile: Profile,
    frames: dict[str, pl.DataFrame],
    *,
    timestamp: datetime | None = None,
) -> dict[str, Any]:
    """Deterministic manifest for a generation run.

    ``timestamp`` defaults to midnight UTC on ``profile.end_date`` so the
    manifest is a pure function of ``(seed, profile, frames)`` — two runs with
    the same inputs produce byte-identical manifests.
    """
    if timestamp is None:
        timestamp = datetime.combine(profile.end_date, time.min, tzinfo=UTC)
    frame_info = {
        name: {"rows": frame.height, "checksum": frame_checksum(frame)}
        for name, frame in sorted(frames.items())
    }
    overall = hashlib.sha256(
        canonical_dumps({name: info["checksum"] for name, info in frame_info.items()}).encode()
    ).hexdigest()
    return {
        "seed": seed,
        "profile": profile.name,
        "timestamp": timestamp.isoformat(),
        "config": {
            "name": profile.name,
            "seed": profile.seed,
            "securities": profile.securities,
            "analysts": profile.analysts,
            "firms": profile.firms,
            "start_date": profile.start_date.isoformat(),
            "end_date": profile.end_date.isoformat(),
            "target_sessions": profile.target_sessions,
        },
        "frames": frame_info,
        "checksum": overall,
    }
