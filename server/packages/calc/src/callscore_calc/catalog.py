"""Datasets the engine can query, pinned to immutable versions.

A catalog normalizes every dataset once, so the engine only ever sees one dtype per logical kind
(Int64, Float64, Decimal(38, s), String, Date, Datetime[us], Boolean), NaN and infinities become
null, and key columns are checked for nulls and duplicates.
"""

from __future__ import annotations

import glob
import hashlib
import os
import threading
from collections import OrderedDict
from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol

import polars as pl

from callscore_calc.dtypes import Kind, from_polars, normalized_dtype
from callscore_calc.errors import DatasetNotFound, SpecError
from callscore_calc.schema import DatasetSchema, Role, build_schema

ROW_INDEX = "__row"
_KEY_KINDS = {Kind.INT, Kind.STR, Kind.DATE, Kind.DATETIME, Kind.BOOL, Kind.DECIMAL}


@dataclass(frozen=True)
class Dataset:
    """One immutable version of a dataset."""

    id: str
    version: str
    schema: DatasetSchema
    kind: Literal["memory", "scan"]
    frame: pl.DataFrame | None = None
    scan: Callable[[], pl.LazyFrame] | None = None

    @property
    def key_columns(self) -> tuple[str, ...]:
        return self.schema.key_columns

    @property
    def has_row_index(self) -> bool:
        """Keyless datasets carry a hidden ``__row`` column so row views have a total order."""
        return not self.schema.key_columns

    def lazy(self) -> pl.LazyFrame:
        if self.frame is not None:
            return self.frame.lazy()
        assert self.scan is not None
        return self.scan()

    @property
    def rows(self) -> int | None:
        return self.frame.height if self.frame is not None else None


class DatasetCatalog(Protocol):
    """What the engine needs from a catalog; implement it to serve datasets your own way."""

    def get(self, dataset_id: str, version: str | None = None) -> Dataset: ...

    def list(self) -> list[DatasetSchema]: ...


class Catalog:
    """Thread-safe in-process catalog of in-memory frames and lazily scanned files.

    Each dataset keeps its ``max_versions`` most recent versions, so scenarios pinned to the
    previous version keep working across one data refresh.
    """

    def __init__(self, *, max_versions: int = 2) -> None:
        if max_versions < 1:
            raise ValueError("max_versions must be at least 1")
        self._max_versions = max_versions
        self._datasets: dict[str, OrderedDict[str, Dataset]] = {}
        self._lock = threading.Lock()

    def register_frame(
        self,
        dataset_id: str,
        frame: pl.DataFrame,
        *,
        key_columns: Sequence[str] = (),
        version: str | None = None,
        editable: Collection[str] | None = None,
        roles: Mapping[str, Role] | None = None,
    ) -> Dataset:
        """Register an in-memory frame. ``version`` defaults to a hash of its contents."""
        keys = tuple(key_columns)
        _check_names(frame.columns)
        frame = normalize(frame.lazy(), frame.schema, keys).collect()
        _check_keys(frame, keys)
        if version is None:
            version = content_version(frame)
        if not keys:
            frame = frame.with_row_index(ROW_INDEX)
        user_schema = {k: v for k, v in frame.schema.items() if k != ROW_INDEX}
        schema = build_schema(
            dataset_id, version, user_schema, keys, editable=editable, roles=roles
        )
        dataset = Dataset(dataset_id, version, schema, "memory", frame=frame)
        self._put(dataset)
        return dataset

    def register_scan(
        self,
        dataset_id: str,
        source: str | Path | Sequence[str | Path],
        *,
        format: Literal["parquet", "ipc"] = "parquet",
        key_columns: Sequence[str] = (),
        version: str | None = None,
        storage_options: Mapping[str, str] | None = None,
        memory_map: bool = True,
        validate_keys: bool = False,
        editable: Collection[str] | None = None,
        roles: Mapping[str, Role] | None = None,
    ) -> Dataset:
        """Register Parquet or Arrow IPC files, read lazily with predicate/projection pushdown.

        ``version`` defaults to a hash of the local files' paths, sizes and modification times;
        remote sources need an explicit version (e.g. an object's ETag or a snapshot id).
        ``validate_keys`` scans the key columns once to check they are unique and non-null.
        """
        keys = tuple(key_columns)
        sources = [str(s) for s in ([source] if isinstance(source, str | Path) else source)]
        if version is None:
            version = file_version(sources)
        options = dict(storage_options) if storage_options else None

        def raw() -> pl.LazyFrame:
            if format == "parquet":
                return pl.scan_parquet(sources, storage_options=options)
            return pl.scan_ipc(sources, storage_options=options, memory_map=memory_map)

        raw_schema = raw().collect_schema()
        _check_names(list(raw_schema))

        def scan() -> pl.LazyFrame:
            lf = normalize(raw(), raw_schema, keys)
            return lf if keys else lf.with_row_index(ROW_INDEX)

        normalized_schema = normalize(raw(), raw_schema, keys).collect_schema()
        for key in keys:
            if key not in normalized_schema:
                raise SpecError(f"key column {key!r} is not in the dataset", code="unknown_column")
        if validate_keys and keys:
            _check_keys(normalize(raw(), raw_schema, keys).select(keys).collect(), keys)
        schema = build_schema(
            dataset_id, version, dict(normalized_schema), keys, editable=editable, roles=roles
        )
        dataset = Dataset(dataset_id, version, schema, "scan", scan=scan)
        self._put(dataset)
        return dataset

    def get(self, dataset_id: str, version: str | None = None) -> Dataset:
        with self._lock:
            versions = self._datasets.get(dataset_id)
            if not versions:
                raise DatasetNotFound(
                    f"unknown dataset: {dataset_id}", detail={"dataset": dataset_id}
                )
            if version is None:
                return next(reversed(versions.values()))
            found = versions.get(version)
            if found is None:
                raise DatasetNotFound(
                    f"dataset {dataset_id} has no version {version} (it may have been replaced)",
                    detail={"dataset": dataset_id, "version": version, "available": list(versions)},
                )
            return found

    def list(self) -> list[DatasetSchema]:
        with self._lock:
            return [next(reversed(v.values())).schema for v in self._datasets.values() if v]

    def drop(self, dataset_id: str, version: str | None = None) -> None:
        with self._lock:
            if version is None:
                self._datasets.pop(dataset_id, None)
            elif dataset_id in self._datasets:
                self._datasets[dataset_id].pop(version, None)

    def _put(self, dataset: Dataset) -> None:
        with self._lock:
            versions = self._datasets.setdefault(dataset.id, OrderedDict())
            versions.pop(dataset.version, None)
            versions[dataset.version] = dataset
            while len(versions) > self._max_versions:
                versions.popitem(last=False)


def normalize(
    lf: pl.LazyFrame, schema: Mapping[str, pl.DataType], keys: Sequence[str] = ()
) -> pl.LazyFrame:
    """Cast to the engine's canonical dtypes and turn NaN/infinite floats into nulls.

    Categorical key columns become strings, since edits look rows up by key value.
    """
    exprs = []
    for name, dtype in schema.items():
        target = normalized_dtype(dtype)
        if name in keys and isinstance(dtype, pl.Categorical):
            target = pl.String()
        expr = pl.col(name)
        changed = False
        if target is not None:
            expr = expr.cast(target)
            changed = True
        if from_polars(dtype).kind is Kind.FLOAT:
            expr = pl.when(expr.is_finite()).then(expr)
            changed = True
        if changed:
            exprs.append(expr.alias(name))
    return lf.with_columns(exprs) if exprs else lf


def content_version(frame: pl.DataFrame) -> str:
    """A version id derived from the data (row order included). Stable within a Polars version."""
    digest = hashlib.sha256(str(list(frame.schema.items())).encode())
    digest.update(str(frame.height).encode())
    if frame.height and frame.width:
        hashed = frame.with_row_index("__i").hash_rows(seed=0, seed_1=1, seed_2=2, seed_3=3)
        digest.update(str(hashed.sum()).encode())
    return "h:" + digest.hexdigest()[:16]


def file_version(sources: Sequence[str]) -> str:
    if any("://" in s for s in sources):
        raise SpecError(
            "remote sources need an explicit version (e.g. an ETag or snapshot id)",
            code="version_required",
        )
    paths = sorted({p for s in sources for p in (glob.glob(s) or [s])})
    digest = hashlib.sha256()
    for path in paths:
        try:
            stat = os.stat(path)
        except FileNotFoundError:
            raise SpecError(f"no such file: {path}", code="file_not_found") from None
        digest.update(f"{path}|{stat.st_size}|{stat.st_mtime_ns}".encode())
    return "f:" + digest.hexdigest()[:16]


def _check_names(names: Sequence[str]) -> None:
    for name in names:
        if name.startswith("__"):
            raise SpecError(
                f"column names starting with '__' are reserved: {name}", code="name_conflict"
            )


def _check_keys(frame: pl.DataFrame, keys: tuple[str, ...]) -> None:
    if not keys:
        return
    for key in keys:
        if key not in frame.columns:
            raise SpecError(f"key column {key!r} is not in the dataset", code="unknown_column")
        kind = from_polars(frame.schema[key]).kind
        if kind not in _KEY_KINDS:
            raise SpecError(f"key column {key} cannot be of type {kind.value}", code="invalid_key")
    checks = frame.select(
        pl.any_horizontal([pl.col(k).is_null() for k in keys]).any().alias("nulls"),
        pl.struct(list(keys)).is_duplicated().any().alias("dupes"),
    ).row(0)
    if checks[0]:
        raise SpecError("key columns contain nulls", code="invalid_key")
    if checks[1]:
        raise SpecError("key columns are not unique", code="invalid_key")
