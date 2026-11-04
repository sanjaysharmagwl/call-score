import subprocess
import sys
import time
from pathlib import Path

import polars as pl
import pytest

from callscore_calc import CalcTimeout, runtime_check
from callscore_calc.exec import Executor, cgroup_cpu_limit


def test_importing_the_package_skips_optional_extras() -> None:
    code = (
        "import sys, callscore_calc; "
        "bad = [m for m in ('fastapi','starlette','redis','hypothesis','callscore_calc_whatif') "
        "if m in sys.modules]; "
        "print(','.join(bad))"
    )
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)
    assert out.stdout.strip() == ""


def test_cgroup_limits(tmp_path: Path) -> None:
    (tmp_path / "cpu.max").write_text("150000 100000\n")
    assert cgroup_cpu_limit(str(tmp_path)) == 1.5
    (tmp_path / "cpu.max").write_text("max 100000\n")
    assert cgroup_cpu_limit(str(tmp_path)) is None
    v1 = tmp_path / "v1"
    (v1 / "cpu").mkdir(parents=True)
    (v1 / "cpu" / "cpu.cfs_quota_us").write_text("200000")
    (v1 / "cpu" / "cpu.cfs_period_us").write_text("100000")
    assert cgroup_cpu_limit(str(v1)) == 2.0
    assert cgroup_cpu_limit(str(tmp_path / "missing")) is None


def test_runtime_check_reports_threads() -> None:
    report = runtime_check(warn=False)
    assert report.polars_threads == pl.thread_pool_size()


def test_timeout_cancels_the_query() -> None:
    executor = Executor()
    heavy = pl.LazyFrame({"a": range(30_000_000)}).select(
        pl.col("a").cast(pl.Float64).sqrt().sort().sum()
    )
    started = time.monotonic()
    with pytest.raises(CalcTimeout):
        executor.collect(heavy, engine="in-memory", deadline=time.monotonic() + 0.001)
    assert time.monotonic() - started < 5


def test_timeouts_do_not_crash_the_process() -> None:
    # A cancelled Polars background query panics (and aborts the process) if its handle is
    # dropped before the worker thread finishes; the executor must keep it alive.
    code = """
import time, polars as pl
from callscore_calc.errors import CalcTimeout
from callscore_calc.exec import Executor
executor = Executor()
heavy = pl.LazyFrame({"a": range(20_000_000)})
heavy = heavy.select(pl.col("a").cast(pl.Float64).sqrt().sort().sum())
for _ in range(3):
    try:
        executor.collect(heavy, engine="in-memory", deadline=time.monotonic() + 0.001)
    except CalcTimeout:
        pass
time.sleep(3)
print("alive")
"""
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=120)
    assert out.returncode == 0, out.stderr[-2000:]
    assert out.stdout.strip() == "alive"
