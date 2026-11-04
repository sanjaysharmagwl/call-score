import os
from decimal import Decimal
from pathlib import Path

import polars as pl
import pytest

from callscore_calc import CalcEngine, Catalog, DatasetNotFound, SpecError


def test_normalizes_dtypes_and_nan() -> None:
    frame = pl.DataFrame(
        {
            "k": pl.Series([1, 2, 3], dtype=pl.Int32),
            "f": pl.Series([1.5, float("nan"), float("inf")], dtype=pl.Float32),
            "c": pl.Series(["a", "b", "a"], dtype=pl.Categorical),
            "d": pl.Series([Decimal("1.5"), None, Decimal("2")], dtype=pl.Decimal(10, 1)),
        }
    )
    catalog = Catalog()
    dataset = catalog.register_frame("t", frame, key_columns=["k"])
    assert dataset.frame is not None
    assert dataset.frame.schema == pl.Schema(
        {"k": pl.Int64, "f": pl.Float64, "c": pl.Categorical(), "d": pl.Decimal(38, 1)}
    )
    assert dataset.frame["f"].to_list() == [1.5, None, None]
    kinds = {c.name: (c.kind.value, c.role, c.editable) for c in dataset.schema.columns}
    assert kinds == {
        "k": ("int", "key", False),
        "f": ("float", "measure", True),
        "c": ("str", "dimension", True),
        "d": ("decimal", "measure", True),
    }


def test_categorical_keys_become_strings_and_enums_become_strings() -> None:
    frame = pl.DataFrame(
        {
            "k": pl.Series(["x", "y"], dtype=pl.Categorical),
            "e": pl.Series(["b", "a"], dtype=pl.Enum(["b", "a"])),
        }
    )
    dataset = Catalog().register_frame("t", frame, key_columns=["k"])
    assert dataset.frame is not None
    assert dataset.frame.schema == pl.Schema({"k": pl.String, "e": pl.String})


@pytest.mark.parametrize(
    ("frame", "keys", "message"),
    [
        (pl.DataFrame({"k": [1, 1]}), ["k"], "not unique"),
        (pl.DataFrame({"k": [1, None]}), ["k"], "nulls"),
        (pl.DataFrame({"k": [1.5]}), ["k"], "cannot be of type"),
        (pl.DataFrame({"k": [1]}), ["x"], "not in the dataset"),
        (pl.DataFrame({"__k": [1]}), [], "reserved"),
    ],
)
def test_rejects_bad_keys_and_names(frame: pl.DataFrame, keys: list[str], message: str) -> None:
    with pytest.raises(SpecError, match=message):
        Catalog().register_frame("t", frame, key_columns=keys)


def test_versions() -> None:
    catalog = Catalog(max_versions=2)
    frame = pl.DataFrame({"k": [1, 2], "v": [1.0, 2.0]})
    first = catalog.register_frame("t", frame, key_columns=["k"])
    again = Catalog().register_frame("t", frame, key_columns=["k"])
    assert first.version == again.version and first.version.startswith("h:")
    changed = catalog.register_frame("t", frame.with_columns(v=pl.col("v") * 2), key_columns=["k"])
    assert changed.version != first.version
    assert catalog.get("t").version == changed.version
    assert catalog.get("t", first.version).version == first.version
    catalog.register_frame("t", frame, key_columns=["k"], version="explicit")
    assert catalog.get("t").version == "explicit"
    assert catalog.get("t", changed.version).version == changed.version
    with pytest.raises(DatasetNotFound):
        catalog.get("t", first.version)  # only the two newest versions are kept
    with pytest.raises(DatasetNotFound):
        catalog.get("missing")


def test_editable_and_roles() -> None:
    frame = pl.DataFrame({"k": [1], "price": [1.0], "code": [7]})
    dataset = Catalog().register_frame(
        "t", frame, key_columns=["k"], editable=["price"], roles={"code": "dimension"}
    )
    meta = {c.name: (c.role, c.editable) for c in dataset.schema.columns}
    assert meta == {"k": ("key", False), "price": ("measure", True), "code": ("dimension", False)}


def test_scan_parquet(tmp_path: Path) -> None:
    path = tmp_path / "positions.parquet"
    pl.DataFrame(
        {"id": [1, 2, 3], "sector": ["a", "b", "a"], "qty": pl.Series([1, 2, 3], dtype=pl.Int32)}
    ).write_parquet(path)
    catalog = Catalog()
    dataset = catalog.register_scan("pq", path, key_columns=["id"], validate_keys=True)
    assert dataset.kind == "scan" and dataset.version.startswith("f:")
    engine = CalcEngine(catalog)
    result = engine.run(
        {
            "dataset": "pq",
            "query": {
                "group_by": ["sector"],
                "measures": [{"name": "q", "fn": "sum", "of": "qty"}],
            },
        }
    )
    assert result.meta.engine == "streaming"
    assert result.frame.to_dicts() == [{"sector": "a", "q": 4}, {"sector": "b", "q": 2}]
    stat = os.stat(path)
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 10**9))
    assert catalog.register_scan("pq", path, key_columns=["id"]).version != dataset.version


def test_scan_ipc_keyless(tmp_path: Path) -> None:
    path = tmp_path / "rows.arrow"
    pl.DataFrame({"v": [3.0, 1.0, 2.0]}).write_ipc(path)
    catalog = Catalog()
    catalog.register_scan("ipc", path, format="ipc")
    engine = CalcEngine(catalog)
    rows = engine.run({"dataset": "ipc", "query": {"page": {"offset": 1, "limit": 5}}})
    assert rows.frame.to_dicts() == [{"v": 1.0}, {"v": 2.0}]
    assert rows.meta.total_rows == 3


def test_remote_scans_need_a_version() -> None:
    with pytest.raises(SpecError, match="explicit version"):
        Catalog().register_scan("s3", "s3://bucket/data.parquet")
