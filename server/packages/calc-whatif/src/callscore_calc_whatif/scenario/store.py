"""Scenario storage: the protocol, and a thread-safe in-memory implementation.

Stores are deliberately dumb: validation and hashing happen in :class:`ScenarioManager`; a store
only persists records and performs the compare-and-append atomically.
"""

from __future__ import annotations

import datetime as dt
import threading
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Protocol

from callscore_calc import VersionConflict

from ..errors import ScenarioNotFound
from .model import LogEntry, Scenario


class ScenarioStore(Protocol):
    def create(self, scenario: Scenario, entries: Sequence[LogEntry] = ()) -> None:
        """Insert a new scenario (with initial entries when forking)."""

    def append(
        self,
        scenario_id: str,
        entries: Sequence[LogEntry],
        *,
        expected_version: int,
        head: str,
        updated_at: dt.datetime,
        client_op_id: str | None,
    ) -> Scenario:
        """Append atomically if the log length is still ``expected_version``.

        Raises :class:`VersionConflict` otherwise. If ``client_op_id`` was already applied, the
        append is a no-op that returns the current scenario (safe retries).
        """

    def get(self, scenario_id: str) -> Scenario:
        """Raise :class:`ScenarioNotFound` if missing (soft-deleted scenarios are returned)."""

    def entries(self, scenario_id: str, *, upto: int | None = None) -> list[LogEntry]:
        """Log entries 1..upto (default: all)."""

    def list(self, *, dataset_id: str | None = None) -> list[Scenario]:
        """Scenarios that are not deleted, oldest first."""

    def delete(self, scenario_id: str) -> None:
        """Soft delete: the log is kept for audit."""


@dataclass
class _Record:
    scenario: Scenario
    entries: list[LogEntry] = field(default_factory=list)
    ops: dict[str, int] = field(default_factory=dict)


class InMemoryScenarioStore:
    """Process-local store for tests and single-replica services."""

    def __init__(self) -> None:
        self._records: dict[str, _Record] = {}
        self._lock = threading.Lock()

    def create(self, scenario: Scenario, entries: Sequence[LogEntry] = ()) -> None:
        with self._lock:
            if scenario.id in self._records:
                raise VersionConflict(f"scenario {scenario.id} already exists")
            self._records[scenario.id] = _Record(scenario, list(entries))

    def append(
        self,
        scenario_id: str,
        entries: Sequence[LogEntry],
        *,
        expected_version: int,
        head: str,
        updated_at: dt.datetime,
        client_op_id: str | None,
    ) -> Scenario:
        with self._lock:
            record = self._record(scenario_id)
            if record.scenario.deleted:
                raise ScenarioNotFound(f"scenario {scenario_id} was deleted")
            if client_op_id is not None and client_op_id in record.ops:
                return record.scenario
            if len(record.entries) != expected_version:
                raise VersionConflict(
                    f"scenario {scenario_id} is at version {len(record.entries)}, "
                    f"not {expected_version}; reload it and retry",
                    detail={"version": len(record.entries)},
                )
            record.entries.extend(entries)
            version = len(record.entries)
            record.scenario = record.scenario.model_copy(
                update={"version": version, "head": head, "updated_at": updated_at}
            )
            if client_op_id is not None:
                record.ops[client_op_id] = version
            return record.scenario

    def get(self, scenario_id: str) -> Scenario:
        with self._lock:
            return self._record(scenario_id).scenario

    def entries(self, scenario_id: str, *, upto: int | None = None) -> list[LogEntry]:
        with self._lock:
            entries = self._record(scenario_id).entries
            return list(entries if upto is None else entries[:upto])

    def list(self, *, dataset_id: str | None = None) -> list[Scenario]:
        with self._lock:
            scenarios = [r.scenario for r in self._records.values() if not r.scenario.deleted]
        if dataset_id is not None:
            scenarios = [s for s in scenarios if s.dataset.id == dataset_id]
        return sorted(scenarios, key=lambda s: (s.created_at, s.id))

    def delete(self, scenario_id: str) -> None:
        with self._lock:
            record = self._record(scenario_id)
            record.scenario = record.scenario.model_copy(update={"deleted": True})

    def _record(self, scenario_id: str) -> _Record:
        record = self._records.get(scenario_id)
        if record is None:
            raise ScenarioNotFound(
                f"unknown scenario: {scenario_id}", detail={"scenario": scenario_id}
            )
        return record
