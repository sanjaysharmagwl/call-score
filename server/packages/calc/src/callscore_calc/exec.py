"""Running Polars plans: concurrency slots, timeouts with cancellation, runtime checks.

Polars already parallelizes one query across all its threads, so running many queries at once
only makes them compete. The executor therefore admits a small number of concurrent requests
(default 2) and makes the rest wait, up to a queue timeout.
"""

from __future__ import annotations

import contextlib
import math
import os
import threading
import time
import warnings
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Literal

import polars as pl
from polars.lazyframe.in_process import InProcessQuery

from callscore_calc.errors import CalcTimeout, ComputeError, EngineBusy

EngineName = Literal["in-memory", "streaming"]


class Executor:
    def __init__(self, *, max_concurrent: int = 2, queue_timeout_s: float = 10.0) -> None:
        if max_concurrent < 1:
            raise ValueError("max_concurrent must be at least 1")
        self._slots = threading.BoundedSemaphore(max_concurrent)
        self._queue_timeout_s = queue_timeout_s

    @contextmanager
    def slot(self) -> Iterator[None]:
        """Hold one execution slot, or raise :class:`EngineBusy` after the queue timeout."""
        if not self._slots.acquire(timeout=self._queue_timeout_s):
            raise EngineBusy(
                f"the calculation engine is busy; no slot freed up in {self._queue_timeout_s:g}s"
            )
        try:
            yield
        finally:
            self._slots.release()

    def collect(
        self, lf: pl.LazyFrame, *, engine: EngineName, deadline: float | None
    ) -> pl.DataFrame:
        """Collect ``lf``; past ``deadline`` (a ``time.monotonic()`` value) cancel and time out."""
        try:
            if deadline is None:
                return lf.collect(engine=engine)
            query = lf.collect(engine=engine, background=True)
            finished = False
            try:
                pause = 0.0005
                while True:
                    result = query.fetch()
                    if result is not None:
                        finished = True
                        return result
                    if time.monotonic() >= deadline:
                        raise CalcTimeout("the calculation took longer than its time limit")
                    time.sleep(pause)
                    pause = min(pause * 2, 0.02)
            finally:
                if not finished:
                    _abandon(query)
        except pl.exceptions.PolarsError as exc:
            raise ComputeError(_polars_message(exc)) from exc


def _abandon(query: InProcessQuery) -> None:
    """Cancel a background query and keep its handle alive until the worker is done.

    Polars' worker thread unwraps the send of its result; if the handle is dropped first, that
    panics inside a Polars thread and aborts the whole process. Draining it avoids that.
    """
    query.cancel()

    def drain() -> None:
        with contextlib.suppress(Exception):
            query.fetch_blocking()

    threading.Thread(target=drain, name="callscore-calc-drain", daemon=True).start()


def _polars_message(exc: Exception) -> str:
    text = str(exc).strip().splitlines()
    return text[0] if text else type(exc).__name__


@dataclass(frozen=True)
class RuntimeReport:
    polars_threads: int
    cpu_limit: float | None
    cpu_count: int | None
    warnings: tuple[str, ...]


def runtime_check(*, warn: bool = True) -> RuntimeReport:
    """Compare the Polars thread pool with the container's CPU limit (cgroup v1 or v2).

    Set ``POLARS_MAX_THREADS`` to the pod's CPU limit before Polars is first imported; more
    threads than CPUs only adds contention.
    """
    threads = pl.thread_pool_size()
    limit = cgroup_cpu_limit()
    messages = []
    if limit is not None and threads > math.ceil(limit):
        messages.append(
            f"Polars uses {threads} threads but the container is limited to {limit:g} CPUs; "
            f"set POLARS_MAX_THREADS={max(1, math.floor(limit))} before importing polars"
        )
    if warn:
        for message in messages:
            warnings.warn(message, RuntimeWarning, stacklevel=2)
    return RuntimeReport(threads, limit, os.cpu_count(), tuple(messages))


def cgroup_cpu_limit(root: str = "/sys/fs/cgroup") -> float | None:
    """CPU limit from cgroup v2 ``cpu.max`` or v1 ``cpu.cfs_quota_us``; None if unlimited."""
    try:
        with open(os.path.join(root, "cpu.max")) as fh:
            quota, period = fh.read().split()[:2]
        return None if quota == "max" else int(quota) / int(period)
    except (OSError, ValueError):
        pass
    try:
        with open(os.path.join(root, "cpu", "cpu.cfs_quota_us")) as fh:
            quota_us = int(fh.read().strip())
        with open(os.path.join(root, "cpu", "cpu.cfs_period_us")) as fh:
            period_us = int(fh.read().strip())
        return None if quota_us <= 0 else quota_us / period_us
    except (OSError, ValueError):
        return None
