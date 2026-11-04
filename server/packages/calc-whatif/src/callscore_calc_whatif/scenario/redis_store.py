"""Redis-backed scenario store: shared by every replica of a service.

Needs the ``redis`` extra (``pip install 'callscore-calc-whatif[redis]'``). Each scenario uses three
keys that share a hash tag (``{id}``), so they live in one Redis Cluster slot and the append can
be a single atomic Lua script: check the log length, push the entries, update the head.

Redis persistence (AOF, replication) decides how durable the audit trail is; for audit-grade
retention implement :class:`~callscore_calc_whatif.scenario.store.ScenarioStore` on a database.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Sequence
from typing import Any

try:
    import redis
    from redis.typing import EncodableT
except ImportError as exc:  # pragma: no cover - exercised only without the extra
    raise ImportError(
        "RedisScenarioStore needs the redis package: pip install 'callscore-calc-whatif[redis]'"
    ) from exc

from callscore_calc import VersionConflict

from ..errors import ScenarioNotFound
from .model import LogEntry, Scenario

# KEYS: meta, log, ops. ARGV: client_op_id, expected_version, head, updated_at, entries...
_APPEND = """
local meta, log, ops = KEYS[1], KEYS[2], KEYS[3]
if redis.call('EXISTS', meta) == 0 then return {-1, 0} end
if redis.call('HGET', meta, 'deleted') == '1' then return {-2, 0} end
local op = ARGV[1]
if op ~= '' then
  local seen = redis.call('HGET', ops, op)
  if seen then return {-3, tonumber(seen)} end
end
local n = redis.call('LLEN', log)
if n ~= tonumber(ARGV[2]) then return {-4, n} end
for i = 5, #ARGV do redis.call('RPUSH', log, ARGV[i]) end
local v = n + (#ARGV - 4)
redis.call('HSET', meta, 'version', v, 'head', ARGV[3], 'updated_at', ARGV[4])
if op ~= '' then redis.call('HSET', ops, op, v) end
return {0, v}
"""


def _text(value: Any) -> str:
    return value.decode() if isinstance(value, bytes) else str(value)


class RedisScenarioStore:
    def __init__(self, client: redis.Redis, *, prefix: str = "calc:scn") -> None:
        self._redis = client
        self._prefix = prefix
        self._append = client.register_script(_APPEND)

    def _keys(self, scenario_id: str) -> tuple[str, str, str]:
        base = f"{self._prefix}:{{{scenario_id}}}"
        return f"{base}:meta", f"{base}:log", f"{base}:ops"

    def _index(self, dataset_id: str | None = None) -> str:
        return f"{self._prefix}:index" if dataset_id is None else f"{self._prefix}:ds:{dataset_id}"

    def create(self, scenario: Scenario, entries: Sequence[LogEntry] = ()) -> None:
        meta, log, _ = self._keys(scenario.id)
        fields: dict[EncodableT, EncodableT] = {
            "doc": scenario.model_dump_json(),
            "version": str(scenario.version),
            "head": scenario.head,
            "updated_at": scenario.updated_at.isoformat(),
            "deleted": "0",
        }
        if not self._redis.hsetnx(meta, "doc", fields["doc"]):
            raise VersionConflict(f"scenario {scenario.id} already exists")
        pipe = self._redis.pipeline(transaction=True)
        pipe.hset(meta, mapping=fields)
        if entries:
            pipe.rpush(log, *[e.model_dump_json() for e in entries])
        pipe.execute()
        score = scenario.created_at.timestamp()
        self._redis.zadd(self._index(), {scenario.id: score})
        self._redis.zadd(self._index(scenario.dataset.id), {scenario.id: score})

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
        args = [
            client_op_id or "",
            str(expected_version),
            head,
            updated_at.isoformat(),
            *[e.model_dump_json() for e in entries],
        ]
        code, value = (int(x) for x in self._append(keys=list(self._keys(scenario_id)), args=args))
        if code in (-1, -2):
            raise ScenarioNotFound(f"unknown or deleted scenario: {scenario_id}")
        if code == -4:
            raise VersionConflict(
                f"scenario {scenario_id} is at version {value}, not {expected_version}; "
                "reload it and retry",
                detail={"version": value},
            )
        return self.get(scenario_id)

    def get(self, scenario_id: str) -> Scenario:
        meta, _, _ = self._keys(scenario_id)
        raw = {_text(k): _text(v) for k, v in self._redis.hgetall(meta).items()}
        if "doc" not in raw:
            raise ScenarioNotFound(
                f"unknown scenario: {scenario_id}", detail={"scenario": scenario_id}
            )
        scenario = Scenario.model_validate_json(raw["doc"])
        return scenario.model_copy(
            update={
                "version": int(raw.get("version", scenario.version)),
                "head": raw.get("head", scenario.head),
                "updated_at": dt.datetime.fromisoformat(raw["updated_at"])
                if "updated_at" in raw
                else scenario.updated_at,
                "deleted": raw.get("deleted") == "1",
            }
        )

    def entries(self, scenario_id: str, *, upto: int | None = None) -> list[LogEntry]:
        self.get(scenario_id)
        if upto == 0:
            return []
        _, log, _ = self._keys(scenario_id)
        end = -1 if upto is None else upto - 1
        return [LogEntry.model_validate_json(_text(x)) for x in self._redis.lrange(log, 0, end)]

    def list(self, *, dataset_id: str | None = None) -> list[Scenario]:
        ids = [_text(x) for x in self._redis.zrange(self._index(dataset_id), 0, -1)]
        out = []
        for scenario_id in ids:
            try:
                scenario = self.get(scenario_id)
            except ScenarioNotFound:
                continue
            if not scenario.deleted:
                out.append(scenario)
        return out

    def delete(self, scenario_id: str) -> None:
        meta, _, _ = self._keys(scenario_id)
        self.get(scenario_id)
        self._redis.hset(meta, "deleted", "1")
