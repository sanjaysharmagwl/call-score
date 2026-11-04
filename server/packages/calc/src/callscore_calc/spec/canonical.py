"""Canonical JSON, fingerprints and spec-version upgrades.

The canonical form of a spec object is its JSON dump without default values, with sorted keys
and no whitespace. Two requests that mean the same thing (key order, ``"1.50"`` vs ``1.5``, a
formula string vs its tree) have the same canonical form and therefore the same fingerprint.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from typing import Any

from pydantic import BaseModel

from callscore_calc.errors import SpecError

SPEC_VERSION = 2

# Top-level keys that only version 1 requests have (what-if was built into the core then).
_V1_KEYS = frozenset({"scenario", "what_if", "base_what_if"})


def _v1_to_v2(raw: dict[str, Any]) -> dict[str, Any]:
    """Version 2 moved what-if into the ``whatif`` plugin: ``scenario``, ``what_if`` and
    ``options.strict_edits`` became ``extensions.whatif``; compare's ``base`` and
    ``base_what_if`` became ``base.extensions.whatif``."""
    raw = dict(raw)
    strict = None
    if isinstance(raw.get("options"), dict):
        options = dict(raw["options"])
        strict = options.pop("strict_edits", None)
        raw["options"] = options

    def block(scenario: Any, steps: Any) -> dict[str, Any] | None:
        if scenario is None and not steps:
            return None
        out: dict[str, Any] = {}
        if scenario is not None:
            out["scenario"] = scenario
        if steps:
            out["steps"] = steps
        if strict is not None:
            out["strict_edits"] = strict
        return out

    target = block(raw.pop("scenario", None), raw.pop("what_if", None))
    if target is not None:
        raw["extensions"] = {**(raw.get("extensions") or {}), "whatif": target}
    if "base" in raw or "base_what_if" in raw:
        base = block(raw.pop("base", None), raw.pop("base_what_if", None))
        if base is not None:
            raw["base"] = {"extensions": {"whatif": base}}
    return raw


# Upgraders turn a raw request of version N into version N + 1. Register one whenever the spec
# changes incompatibly, so old requests keep working and keep their meaning.
UPGRADERS: dict[int, Callable[[dict[str, Any]], dict[str, Any]]] = {1: _v1_to_v2}


def request_version(raw: dict[str, Any]) -> Any:
    """The request's ``spec_version``; a request without one is version 1 if it uses keys only
    version 1 had, else the current version."""
    if "spec_version" in raw:
        return raw["spec_version"]
    options = raw.get("options")
    legacy_base = "base" in raw and not isinstance(raw["base"], dict | type(None))
    legacy_base = legacy_base or (isinstance(raw.get("base"), dict) and "id" in raw["base"])
    if (
        _V1_KEYS & raw.keys()
        or legacy_base
        or (isinstance(options, dict) and "strict_edits" in options)
    ):
        return 1
    return SPEC_VERSION


def upgrade(raw: dict[str, Any]) -> dict[str, Any]:
    """Bring a raw request dict up to the current ``spec_version``."""
    version = request_version(raw)
    if not isinstance(version, int) or version < 1 or version > SPEC_VERSION:
        raise SpecError(
            f"unsupported spec_version {version!r}; this engine speaks up to {SPEC_VERSION}",
            code="unsupported_spec_version",
            path="/spec_version",
        )
    while version < SPEC_VERSION:
        raw = {**UPGRADERS[version](raw), "spec_version": version + 1}
        version += 1
    return raw


def to_canonical(value: Any) -> Any:
    """JSON-ready canonical form of a model, or of plain data containing models."""
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json", exclude_defaults=True)
    if isinstance(value, dict):
        return {str(k): to_canonical(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [to_canonical(v) for v in value]
    return value


def canonical_json(value: Any) -> str:
    return json.dumps(
        to_canonical(value), sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )


def fingerprint(value: Any) -> str:
    """SHA-256 of the canonical JSON, as hex."""
    return hashlib.sha256(canonical_json(value).encode()).hexdigest()
