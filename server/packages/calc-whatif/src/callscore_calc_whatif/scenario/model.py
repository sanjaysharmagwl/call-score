"""Scenario records: an append-only log of steps over a pinned dataset version.

``version`` is the log length, so ``(scenario id, version)`` names an immutable prefix of the
log forever. Each entry is hashed together with the previous hash; ``head`` is the latest hash,
which makes the log tamper-evident and gives a content address for caching.
"""

from __future__ import annotations

import datetime as dt
import hashlib
from collections.abc import Sequence

from pydantic import BaseModel, ConfigDict

from callscore_calc import DatasetRef, canonical_json, fingerprint

from ..spec import ScenarioRef, ScenarioStep


class Scenario(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str
    name: str
    description: str | None = None
    dataset: DatasetRef
    owner: str | None = None
    forked_from: ScenarioRef | None = None
    version: int = 0
    genesis: str
    head: str
    created_at: dt.datetime
    updated_at: dt.datetime
    deleted: bool = False

    @property
    def ref(self) -> ScenarioRef:
        return ScenarioRef(id=self.id, version=self.version)


class LogEntry(BaseModel):
    model_config = ConfigDict(frozen=True)

    seq: int
    step: ScenarioStep
    author: str | None = None
    at: dt.datetime
    note: str | None = None
    client_op_id: str | None = None
    hash: str


def now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def genesis_hash(
    scenario_id: str, dataset: DatasetRef, forked_from: ScenarioRef | None, created_at: dt.datetime
) -> str:
    return fingerprint(
        {
            "scenario": scenario_id,
            "dataset": dataset,
            "forked_from": forked_from,
            "created_at": created_at.isoformat(),
        }
    )


def entry_hash(
    previous: str,
    seq: int,
    step: ScenarioStep,
    author: str | None,
    at: dt.datetime,
    note: str | None,
) -> str:
    body = canonical_json(
        {"seq": seq, "step": step, "author": author, "at": at.isoformat(), "note": note}
    )
    return hashlib.sha256((previous + body).encode()).hexdigest()


def make_entries(
    previous: str,
    first_seq: int,
    steps: Sequence[ScenarioStep],
    *,
    author: str | None,
    at: dt.datetime,
    note: str | None,
    client_op_id: str | None,
) -> list[LogEntry]:
    entries = []
    for offset, step in enumerate(steps):
        seq = first_seq + offset
        digest = entry_hash(previous, seq, step, author, at, note)
        entries.append(
            LogEntry(
                seq=seq,
                step=step,
                author=author,
                at=at,
                note=note,
                client_op_id=client_op_id,
                hash=digest,
            )
        )
        previous = digest
    return entries


def verify_chain(scenario: Scenario, entries: Sequence[LogEntry]) -> bool:
    """True if the entries hash to the scenario's head and are numbered 1..version."""
    expected = genesis_hash(
        scenario.id, scenario.dataset, scenario.forked_from, scenario.created_at
    )
    if expected != scenario.genesis:
        return False
    previous = scenario.genesis
    for index, entry in enumerate(entries, start=1):
        if entry.seq != index:
            return False
        digest = entry_hash(previous, entry.seq, entry.step, entry.author, entry.at, entry.note)
        if digest != entry.hash:
            return False
        previous = digest
    return len(entries) == scenario.version and previous == scenario.head
