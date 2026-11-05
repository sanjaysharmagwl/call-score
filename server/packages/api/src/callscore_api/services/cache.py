"""On-disk Parquet stage cache for engine pipeline outputs.

``StageCache`` stores one ``.parquet`` file per stage-output frame under
``<root>/<stage>/<cache_fingerprint>/<frame>.parquet`` plus a JSON sidecar
(`<frame>.json`) recording the fingerprint, row count and column dtypes.

Keys are the coarse stage fingerprints produced by
``callscore_engine.runner.run_pipeline`` (``contracts.stage_fingerprint``),
which already encode the methodology slice for the stage, the run context
(as-of, knowledge cutoff, source/universe fingerprints, algorithm version),
the upstream stage fingerprints and — for METRICS/STATISTICS — the metric
catalog fingerprint. Changing any of those yields a different directory, so
stale entries are simply never read; there is no invalidation bookkeeping.

Reads are validating: a missing/corrupt/truncated parquet file, a sidecar
mismatch or a schema/row-count drift all count as a cache *miss* and the
caller recomputes. Writes are atomic (temp file + ``os.replace``) so a
crash never leaves a half-written entry that looks valid.
"""

import json
import os
import re
from collections.abc import Callable, Iterable, Mapping
from pathlib import Path
from typing import Any

import polars as pl

from callscore_core.enums import Stage

_FINGERPRINT_RE = re.compile(r"^[0-9a-f]{64}$")
_META_VERSION = 1
_TMP_SUFFIX = ".tmp"


def _check_fingerprint(fingerprint: str) -> None:
    if not _FINGERPRINT_RE.match(fingerprint):
        raise ValueError(f"invalid stage fingerprint: {fingerprint!r}")


def _schema_pairs(frame: pl.DataFrame) -> list[list[str]]:
    """Ordered ``[name, dtype]`` pairs (JSON objects would lose column order)."""
    return [[name, str(dtype)] for name, dtype in frame.schema.items()]


class StageCache:
    """Filesystem cache of stage output frames keyed by stage fingerprint.

    ``root`` is any writable directory; nothing outside it is touched.
    """

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)

    # -- paths ---------------------------------------------------------

    def stage_dir(self, stage: Stage, fingerprint: str) -> Path:
        _check_fingerprint(fingerprint)
        return self.root / stage.value / fingerprint

    def _parquet_path(self, stage: Stage, fingerprint: str, frame_name: str) -> Path:
        return self.stage_dir(stage, fingerprint) / f"{frame_name}.parquet"

    def _meta_path(self, stage: Stage, fingerprint: str, frame_name: str) -> Path:
        return self.stage_dir(stage, fingerprint) / f"{frame_name}.json"

    # -- writes --------------------------------------------------------

    @staticmethod
    def _atomic_write(path: Path, write: Callable[[Path], object]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + _TMP_SUFFIX)
        try:
            write(tmp)
            os.replace(tmp, path)
        finally:
            tmp.unlink(missing_ok=True)

    def put_frame(
        self, stage: Stage, fingerprint: str, frame_name: str, frame: pl.DataFrame
    ) -> Path:
        """Write ``frame`` under ``(stage, fingerprint, frame_name)`` atomically."""
        if not isinstance(frame, pl.DataFrame):
            raise TypeError(f"expected pl.DataFrame, got {type(frame).__name__}")
        _check_fingerprint(fingerprint)
        parquet_path = self._parquet_path(stage, fingerprint, frame_name)
        meta_path = self._meta_path(stage, fingerprint, frame_name)

        self._atomic_write(parquet_path, frame.write_parquet)
        meta: dict[str, Any] = {
            "version": _META_VERSION,
            "stage": stage.value,
            "fingerprint": fingerprint,
            "frame": frame_name,
            "row_count": frame.height,
            "columns": _schema_pairs(frame),
        }
        self._atomic_write(meta_path, lambda p: p.write_text(json.dumps(meta, sort_keys=True)))
        return parquet_path

    def store_stage(
        self,
        stage: Stage,
        fingerprint: str,
        frames: Mapping[str, pl.DataFrame],
    ) -> None:
        """Store every ``{frame_name: DataFrame}`` for one stage fingerprint."""
        for frame_name, frame in frames.items():
            self.put_frame(stage, fingerprint, frame_name, frame)

    # -- reads ---------------------------------------------------------

    def _read_meta(self, stage: Stage, fingerprint: str, frame_name: str) -> dict[str, Any] | None:
        try:
            raw = json.loads(self._meta_path(stage, fingerprint, frame_name).read_text())
        except (OSError, json.JSONDecodeError):
            return None
        if not isinstance(raw, dict):
            return None
        if (
            raw.get("version") != _META_VERSION
            or raw.get("stage") != stage.value
            or raw.get("fingerprint") != fingerprint
            or raw.get("frame") != frame_name
        ):
            return None
        columns = raw.get("columns")
        row_count = raw.get("row_count")
        if (
            not isinstance(columns, list)
            or not all(
                isinstance(pair, list) and len(pair) == 2 and all(isinstance(x, str) for x in pair)
                for pair in columns
            )
            or not isinstance(row_count, int)
        ):
            return None
        # Ordered map name -> dtype string (duplicates rejected).
        ordered = {name: dtype for name, dtype in columns}
        if len(ordered) != len(columns):
            return None
        raw["columns"] = ordered
        return raw

    def get_frame(
        self,
        stage: Stage,
        fingerprint: str,
        frame_name: str,
        *,
        expected_schema: Mapping[str, str] | None = None,
    ) -> pl.DataFrame | None:
        """Return the cached frame, or ``None`` on any miss/corruption.

        Validation: sidecar fingerprint/stage/frame match, parquet parses,
        height equals the recorded ``row_count`` and column names/dtypes equal
        the recorded schema (and ``expected_schema`` when given).
        """
        try:
            _check_fingerprint(fingerprint)
        except ValueError:
            return None
        meta = self._read_meta(stage, fingerprint, frame_name)
        if meta is None:
            return None
        parquet_path = self._parquet_path(stage, fingerprint, frame_name)
        try:
            frame = pl.read_parquet(parquet_path)
        except Exception:  # noqa: BLE001 - any read/parse failure is a miss
            return None
        recorded: dict[str, Any] = meta["columns"]
        if frame.height != meta["row_count"]:
            return None
        if frame.columns != list(recorded):
            return None
        actual = {name: str(dtype) for name, dtype in frame.schema.items()}
        if actual != {str(k): str(v) for k, v in recorded.items()}:
            return None
        if expected_schema is not None:
            expected = {str(k): str(v) for k, v in expected_schema.items()}
            if actual != expected:
                return None
        return frame

    def load_stage(
        self,
        stage: Stage,
        fingerprint: str,
        frame_names: Iterable[str],
    ) -> dict[str, pl.DataFrame] | None:
        """Load all ``frame_names`` for a stage; ``None`` if any is missing."""
        names = list(frame_names)
        frames: dict[str, pl.DataFrame] = {}
        for name in names:
            frame = self.get_frame(stage, fingerprint, name)
            if frame is None:
                return None
            frames[name] = frame
        return frames

    def cached_or_compute(
        self,
        stage: Stage,
        fingerprint: str,
        frame_name: str,
        compute_fn: Callable[[], pl.DataFrame],
        *,
        expected_schema: Mapping[str, str] | None = None,
    ) -> pl.DataFrame:
        """Return the cached frame or compute, store and return a fresh one."""
        frame = self.get_frame(stage, fingerprint, frame_name, expected_schema=expected_schema)
        if frame is not None:
            return frame
        frame = compute_fn()
        self.put_frame(stage, fingerprint, frame_name, frame)
        return frame

    # -- maintenance ---------------------------------------------------

    def remove_stage(self, stage: Stage, fingerprint: str) -> None:
        """Delete every cached frame for ``(stage, fingerprint)``."""
        import shutil

        shutil.rmtree(self.stage_dir(stage, fingerprint), ignore_errors=True)

    def clear(self) -> None:
        """Delete the whole cache tree."""
        import shutil

        shutil.rmtree(self.root, ignore_errors=True)


__all__ = ["StageCache"]
