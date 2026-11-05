"""``callscore`` command-line interface.

Commands::

    callscore run --db PATH [--scale SCALE] --as-of DATE \
        --methodology NAME|PATH [--knowledge-cutoff ISO] [--run-id ID]
    callscore leaderboard --db PATH --run ID [--limit N]

``run`` registers the methodology, creates a ``queued`` ``calc_run`` and
executes it via :func:`callscore_api.services.calculation.execute_run`.
``leaderboard`` prints the run's composite scores with provenance
(run id, methodology fingerprint, as-of date).
"""

import argparse
import json
import re
import sys
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, TextIO

from sqlalchemy import Engine, select

from callscore_api.services import calculation
from callscore_core.methodology import Methodology
from callscore_store import create_engine_for
from callscore_store.models import AnalystComposite, CalcRun, MethodologyVersion
from callscore_store.repositories import runs
from callscore_store.schema import bootstrap

_SCALES = ("small", "medium", "full")
_PRESET_METHODOLOGIES = {"default": Methodology()}


def _parse_date(value: str) -> date:
    return date.fromisoformat(value)


def _parse_cutoff(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise argparse.ArgumentTypeError("knowledge-cutoff must be timezone-aware")
    return parsed.astimezone(UTC)


def _load_methodology(spec: str, ctx: Engine | None = None) -> Methodology:
    """Resolve ``NAME|PATH``: preset name, JSON file path, or DB fingerprint."""
    if spec in _PRESET_METHODOLOGIES:
        return _PRESET_METHODOLOGIES[spec]
    path = Path(spec)
    if path.suffix == ".json" or path.exists():
        return Methodology.model_validate(json.loads(path.read_text()))
    if ctx is not None and re.fullmatch(r"[0-9a-f]{64}", spec):
        with ctx.connect() as conn:
            params_json = conn.execute(
                select(MethodologyVersion.params_json).where(MethodologyVersion.fingerprint == spec)
            ).scalar_one_or_none()
        if params_json is not None:
            return Methodology.model_validate(json.loads(str(params_json)))
    raise ValueError(
        f"unknown methodology {spec!r}: expected 'default', a .json path, "
        "or the fingerprint of a stored methodology_version"
    )


def _unique_run_id(ctx: Engine, base: str) -> str:
    """Return ``base`` or ``base-rN`` if the run id is already taken."""
    candidate = base
    suffix = 2
    while runs.get_calc_run(ctx, candidate) is not None:
        candidate = f"{base}-r{suffix}"
        suffix += 1
    return candidate


def _cmd_run(args: argparse.Namespace, out: TextIO, err: TextIO) -> int:
    as_of = _parse_date(args.as_of)
    cutoff = (
        _parse_cutoff(args.knowledge_cutoff)
        if args.knowledge_cutoff
        else calculation.default_knowledge_cutoff(as_of)
    )
    engine = create_engine_for(args.db)
    bootstrap(engine)
    try:
        methodology = _load_methodology(args.methodology, ctx=engine)
    except (OSError, ValueError) as exc:
        print(f"callscore: cannot load methodology: {exc}", file=err)
        return 2
    universe_filter_json = json.dumps({"scale": args.scale}, sort_keys=True) if args.scale else None
    base = args.run_id or f"{as_of.isoformat()}-{methodology.fingerprint()[:12]}"
    run_id = _unique_run_id(engine, base)
    calculation.prepare_run(
        engine,
        methodology,
        run_id=run_id,
        as_of_date=as_of,
        knowledge_cutoff=cutoff,
        universe_filter_json=universe_filter_json,
    )
    try:
        counts = calculation.execute_run(engine, run_id)
    except Exception as exc:  # noqa: BLE001 - CLI reports any run failure
        run = runs.get_calc_run(engine, run_id)
        error = json.loads(run["error_json"]) if run and run.get("error_json") else {}
        print(
            f"callscore: run {run_id} failed: {error.get('type', type(exc).__name__)}: "
            f"{error.get('message', exc)}",
            file=err,
        )
        return 1
    total = sum(counts.values())
    print(
        f"run_id={run_id} status=succeeded methodology={methodology.fingerprint()} "
        f"as_of={as_of.isoformat()} rows={total}",
        file=out,
    )
    for table, count in sorted(counts.items()):
        print(f"  {table}: {count}", file=out)
    return 0


def _cmd_leaderboard(args: argparse.Namespace, out: TextIO, err: TextIO) -> int:
    engine = create_engine_for(args.db)
    stmt = (
        select(
            AnalystComposite.analyst_id,
            AnalystComposite.peer_group,
            AnalystComposite.composite_score,
            AnalystComposite.rank_overall,
            AnalystComposite.is_ranked,
            AnalystComposite.unranked_reason,
            CalcRun.run_id,
            CalcRun.as_of_date,
            MethodologyVersion.fingerprint,
        )
        .join(CalcRun, AnalystComposite.run_id == CalcRun.run_id)
        .join(MethodologyVersion, CalcRun.methodology_version_id == MethodologyVersion.id)
        .where(AnalystComposite.run_id == args.run)
        .order_by(
            AnalystComposite.rank_overall.is_(None),
            AnalystComposite.rank_overall,
            AnalystComposite.analyst_id,
        )
        .limit(args.limit)
    )
    with engine.connect() as conn:
        rows = [dict(r) for r in conn.execute(stmt).mappings()]
    if not rows:
        print(f"callscore: run {args.run!r} has no composite rows", file=err)
        return 1
    first = rows[0]
    print(f"# run_id={first['run_id']} as_of={first['as_of_date']}", file=out)
    print(f"# methodology_fingerprint={first['fingerprint']}", file=out)
    print(f"# analysts={len(rows)} limit={args.limit}", file=out)
    header = ("analyst_id", "peer_group", "score", "rank", "ranked", "reason")
    print("\t".join(header), file=out)
    for row in rows:
        score = row["composite_score"]
        print(
            "\t".join(
                [
                    str(row["analyst_id"]),
                    "-" if row["peer_group"] is None else str(row["peer_group"]),
                    "-" if score is None else f"{float(score):.4f}",
                    "-" if row["rank_overall"] is None else str(row["rank_overall"]),
                    "yes" if row["is_ranked"] else "no",
                    "-" if row["unranked_reason"] is None else str(row["unranked_reason"]),
                ]
            ),
            file=out,
        )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="callscore")
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="execute a scoring run")
    run.add_argument("--db", type=Path, required=True, help="SQLite DB path")
    run.add_argument("--scale", choices=_SCALES, default=None, help="data scale hint")
    run.add_argument("--as-of", required=True, help="as-of date (YYYY-MM-DD)")
    run.add_argument(
        "--methodology",
        required=True,
        help="'default', a methodology JSON path, or a stored fingerprint",
    )
    run.add_argument(
        "--knowledge-cutoff",
        default=None,
        help="ISO-8601 aware datetime; default = as_of + 1 day 00:00 UTC",
    )
    run.add_argument("--run-id", default=None, help="explicit run id")
    run.set_defaults(handler=_cmd_run)

    board = sub.add_parser("leaderboard", help="print a run's composite leaderboard")
    board.add_argument("--db", type=Path, required=True, help="SQLite DB path")
    board.add_argument("--run", required=True, help="run id")
    board.add_argument("--limit", type=int, default=20, help="max rows (default 20)")
    board.set_defaults(handler=_cmd_leaderboard)
    return parser


def main(
    argv: list[str] | None = None, *, out: TextIO | None = None, err: TextIO | None = None
) -> int:
    args = build_parser().parse_args(argv)
    handler: Any = args.handler
    return int(handler(args, out or sys.stdout, err or sys.stderr))


if __name__ == "__main__":
    sys.exit(main())
