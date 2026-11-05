"""``callscore-synth`` entry point: generate a synthetic dataset into a SQLite DB."""

import argparse
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import polars as pl
from sqlalchemy import text

from callscore_core.methodology import canonical_dumps
from callscore_store import create_engine_for, schema
from callscore_store.models import GenerationManifest
from callscore_store.repositories import (
    insert_market,
    insert_rating_events,
    insert_reference,
)
from callscore_store.repositories.base import normalize_rows, table_of
from callscore_store.validation import ValidationError, validate_table
from callscore_synth import consensus, market, ratings
from callscore_synth import manifest as manifest_mod
from callscore_synth.profiles import Profile, load_profile

_SCALES = ("small", "medium", "full")

# frames-dict key -> insert_reference keyword.
_REFERENCE_GROUPS = {
    "firms": "firms",
    "analysts": "analysts",
    "exchange": "exchanges",
    "sectors": "sectors",
    "securities": "securities",
    "sessions": "sessions",
    "employment_spells": "employment",
    "coverage_spells": "coverage",
    "security_identifiers": "identifiers",
    "security_classifications": "classifications",
    "rating_scales": "rating_scales",
    "scale_maps": "scale_maps",
}

# frames-dict key -> insert_market keyword.
_MARKET_GROUPS = {
    "benchmark_def": "benchmark_defs",
    "security_daily": "security_daily",
    "corporate_actions": "corporate_actions",
    "delistings": "delistings",
    "benchmark_daily": "benchmark_daily",
    "factor_return_daily": "factor_returns",
    "security_beta": "security_beta",
    "fx_daily": "fx_daily",
    "liquidity_daily": "liquidity_daily",
    "consensus_daily": "consensus_daily",
}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="callscore-synth")
    parser.add_argument("--scale", choices=_SCALES, default="small")
    parser.add_argument("--seed", type=int, default=None, help="Override the profile seed")
    parser.add_argument("--out", type=Path, required=True, help="Output SQLite DB path")
    parser.add_argument(
        "--force",
        action="store_true",
        help="Overwrite an existing non-empty database",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Generate and validate, print counts, write nothing",
    )
    return parser


def generate_frames(
    profile: Profile, seed: int | None
) -> tuple[dict[str, pl.DataFrame], dict[str, Any]]:
    """Generate every frame plus the generation manifest."""
    data = market.generate(seed=seed, profile=profile)
    frames = dict(data.frames)
    frames.update(ratings.generate(frames, profile, data.seed))
    frames["consensus_daily"] = consensus.build_consensus_daily(frames)
    manifest = manifest_mod.generation_manifest(data.seed, profile, frames)
    return frames, manifest


def _rows(frame: pl.DataFrame) -> list[dict[str, object]]:
    return [dict(r) for r in frame.iter_rows(named=True)]


def _prevalidate(frames: dict[str, pl.DataFrame]) -> dict[str, list[str]]:
    """Run ``validate_table`` over generated rows; returns table -> issue strings."""
    from callscore_store.models import (
        BenchmarkDaily,
        Delisting,
        RatingEvent,
        SecurityClassification,
        SecurityDaily,
    )

    checks = {
        "rating_event": (RatingEvent, frames.get("rating_events")),
        "security_daily": (SecurityDaily, frames.get("security_daily")),
        "delisting": (Delisting, frames.get("delistings")),
        "benchmark_daily": (BenchmarkDaily, frames.get("benchmark_daily")),
        "security_classification": (
            SecurityClassification,
            frames.get("security_classifications"),
        ),
    }
    problems: dict[str, list[str]] = {}
    for table, (model, frame) in checks.items():
        if frame is None or frame.height == 0:
            continue
        rows = normalize_rows(model, _rows(frame))
        errors, _warnings = validate_table(table, rows)
        if errors:
            problems[table] = [
                f"row {i.row_index}.{i.column or ''}: {i.message}" for i in errors[:10]
            ]
    return problems


def _print_summary(frames: dict[str, pl.DataFrame], manifest: dict[str, Any], seed: int) -> None:
    print(f"callscore-synth: seed={seed} profile={manifest['profile']}")
    for name in sorted(frames):
        print(f"  {name}: {frames[name].height} rows")
    print(f"  checksum: {manifest['checksum']}")


def _db_is_empty(path: Path) -> bool:
    """True when ``path`` does not exist or is a zero-byte file."""
    return not path.exists() or path.stat().st_size == 0


def _has_user_tables(path: Path) -> bool:
    from sqlalchemy import inspect

    engine = create_engine_for(path)
    try:
        names = set(inspect(engine).get_table_names())
    finally:
        engine.dispose()
    return bool(names - {"sqlite_sequence"})


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    profile = load_profile(args.scale)

    try:
        frames, manifest = generate_frames(profile, args.seed)
    except Exception as exc:  # generation failure -> nonzero exit
        print(f"callscore-synth: generation failed: {exc}", file=sys.stderr)
        return 1

    if args.dry_run:
        _print_summary(frames, manifest, int(manifest["seed"]))
        print("callscore-synth: dry-run, nothing written")
        return 0

    out: Path = args.out
    out.parent.mkdir(parents=True, exist_ok=True)
    if not _db_is_empty(out):
        if not _has_user_tables(out):
            pass  # file exists but holds no tables: treat as empty
        elif not args.force:
            print(
                f"callscore-synth: {out} exists and is non-empty; "
                "refusing to overwrite (use --force)",
                file=sys.stderr,
            )
            return 2
        else:
            out.unlink()

    engine = create_engine_for(out)
    try:
        schema.bootstrap(engine)

        ref_kwargs = {
            kw: _rows(frames[key])
            for key, kw in _REFERENCE_GROUPS.items()
            if key in frames and frames[key].height
        }
        market_kwargs = {
            kw: _rows(frames[key])
            for key, kw in _MARKET_GROUPS.items()
            if key in frames and frames[key].height
        }
        event_rows = _rows(frames["rating_events"])

        # Pre-validate the contract-critical tables so a bad batch prints its
        # row issues *before* anything is written (repositories re-validate
        # atomically per group during insert).
        precheck = _prevalidate(frames)
        if precheck:
            for table, issues in precheck.items():
                print(f"callscore-synth: {table} validation issues:", file=sys.stderr)
                for issue in issues:
                    print(f"  {issue}", file=sys.stderr)
            return 1

        try:
            insert_reference(engine, **ref_kwargs)
            if event_rows:
                insert_rating_events(engine, event_rows, source="synth")
            insert_market(engine, **market_kwargs)
        except ValidationError as exc:
            print(f"callscore-synth: validation failed: {exc}", file=sys.stderr)
            return 1

        with engine.begin() as conn:
            conn.execute(
                table_of(GenerationManifest).insert(),
                [
                    {
                        "seed": int(manifest["seed"]),
                        "profile": str(manifest["profile"]),
                        "config_json": canonical_dumps(manifest["config"]),
                        "table_checksums_json": canonical_dumps(
                            {k: v["checksum"] for k, v in manifest["frames"].items()}
                        ),
                        "created_at": datetime.now(UTC),
                    }
                ],
            )
            # Sanity: the manifest row landed.
            conn.execute(text("SELECT 1"))
    finally:
        engine.dispose()

    _print_summary(frames, manifest, int(manifest["seed"]))
    print(f"callscore-synth: wrote {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
