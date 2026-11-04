"""``callscore-synth`` entry point: generate a synthetic dataset into a SQLite DB."""

import argparse
import sys
from pathlib import Path

from callscore_store import create_engine_for

_SCALES = ("small", "medium", "full")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="callscore-synth")
    parser.add_argument("--scale", choices=_SCALES, default="small")
    parser.add_argument("--out", type=Path, required=True, help="Output SQLite DB path")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine_for(args.out)
    with engine.connect():
        pass
    print(f"callscore-synth: would generate a '{args.scale}' dataset into {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
