"""Scenario backend: saved methodology overrides, execution, comparison.

A *scenario* is a named, persisted bundle::

    {name, description, base methodology (immutable params + fingerprint),
     overrides (subset of ``Methodology`` fields, shallow-merged via
     ``Methodology.with_overrides``), default universe_filter, run history}

The ``scenario`` table only has ``(scenario_id, name, config_json,
created_at)`` — everything except the name lives in ``config_json`` as a
JSON document (there is no ``result_run_id``/archive column; run linkage is
kept inside the config as ``run_ids``/``last_run_id``/``last_execution``/
``sweeps`` — flagged as a schema gap for the store owners).

Function overview::

    create_scenario / list_scenarios / get_scenario
    resolve_base_methodology / merge_overrides      (validation helpers)
    execute_scenario      -> merged methodology + idempotent ``calc_run``
    compare_runs          -> per-analyst/metric/family deltas + rank stats
    sensitivity_sweep     -> bounded (<= MAX_SWEEP_COMBOS) param sweep
    invalidation_report   -> per-stage fingerprint impact of a param change

``compare_runs`` extends the generic ``GET /api/runs/compare`` summary with
score/rank deltas, ranked<->unranked transition counts, a Spearman rank
correlation over commonly-ranked analysts, per-family and per-metric
deltas, and an explicit universe-compatibility report. It does NOT require
runs to be ``succeeded`` — it compares whatever derived rows exist (empty
runs simply yield empty sections).
"""

import hashlib
import itertools
import json
import uuid
from collections.abc import Iterable, Mapping, Sequence
from datetime import UTC, date, datetime
from typing import Any

from sqlalchemy import Engine, func, select

from callscore_api.services.calculation import default_knowledge_cutoff
from callscore_api.services.jobs import JobCoordinator
from callscore_core.contracts import STAGE_METHODOLOGY_FIELDS, STAGE_ORDER
from callscore_core.methodology import Methodology, canonical_dumps
from callscore_core.metrics import metric_definition
from callscore_store.models import (
    AnalystComposite,
    AnalystMetric,
    MethodologyVersion,
    Scenario,
)
from callscore_store.repositories import runs as runs_repo
from callscore_store.repositories.base import Context, table_of, unit_of_work

# Hard cap on sensitivity-sweep combinations (spec: reject >25 with 422).
MAX_SWEEP_COMBOS = 25

_FINGERPRINT_LEN = 64


# ---------------------------------------------------------------------------
# Scenario row <-> view
# ---------------------------------------------------------------------------


def _new_scenario_id() -> str:
    return f"scn-{uuid.uuid4().hex[:16]}"


def _empty_config() -> dict[str, Any]:
    return {
        "description": None,
        "base": {"ref": "default", "fingerprint": None, "params": None},
        "overrides": {},
        "universe_filter": None,
        "run_ids": [],
        "last_run_id": None,
        "last_execution": None,
        "sweeps": [],
    }


def _parse_config(raw: object) -> dict[str, Any]:
    config = _empty_config()
    if isinstance(raw, str) and raw:
        try:
            value = json.loads(raw)
        except json.JSONDecodeError:
            value = None
        if isinstance(value, dict):
            for key in config:
                if key in value:
                    config[key] = value[key]
    # Defensive normalization of list fields.
    for key in ("run_ids", "sweeps"):
        if not isinstance(config[key], list):
            config[key] = []
    if not isinstance(config["overrides"], dict):
        config["overrides"] = {}
    return config


def _scenario_view(row: Mapping[str, Any]) -> dict[str, Any]:
    """Flatten a ``scenario`` row + parsed ``config_json`` into one dict."""
    config = _parse_config(row.get("config_json"))
    base = config["base"] if isinstance(config["base"], dict) else {}
    params = base.get("params")
    methodology: Methodology | None = None
    if isinstance(params, dict):
        try:
            methodology = Methodology.model_validate(params)
        except Exception:  # noqa: BLE001 - stored params should validate; degrade
            methodology = None
    run_ids = [str(r) for r in config["run_ids"]]
    return {
        "scenario_id": str(row["scenario_id"]),
        "name": str(row["name"]),
        "description": config["description"],
        "created_at": row["created_at"],
        "base_methodology": methodology,
        "base_fingerprint": base.get("fingerprint"),
        "base_ref": base.get("ref"),
        "overrides": dict(config["overrides"]),
        "universe_filter": config["universe_filter"],
        "run_ids": run_ids,
        "run_count": len(run_ids),
        "last_run_id": config["last_run_id"],
        "last_execution": config["last_execution"],
        "sweeps": list(config["sweeps"]),
    }


def _get_scenario_row(ctx: Context, scenario_id: str) -> dict[str, Any]:
    stmt = select(Scenario).where(Scenario.scenario_id == scenario_id)
    if isinstance(ctx, Engine):
        with ctx.connect() as conn:
            row = conn.execute(stmt).mappings().first()
    else:
        row = ctx.execute(stmt).mappings().first()
    if row is None:
        raise KeyError(f"scenario {scenario_id!r} not found")
    return dict(row)


def _insert_scenario(
    ctx: Context,
    *,
    scenario_id: str,
    name: str,
    config: Mapping[str, Any],
) -> None:
    with unit_of_work(ctx) as conn:
        conn.execute(
            table_of(Scenario)
            .insert()
            .values(
                scenario_id=scenario_id,
                name=name,
                config_json=canonical_dumps(dict(config)),
                created_at=datetime.now(UTC),
            )
        )


def _update_scenario_config(ctx: Context, scenario_id: str, config: Mapping[str, Any]) -> None:
    tbl = table_of(Scenario)
    with unit_of_work(ctx) as conn:
        conn.execute(
            tbl.update()
            .where(tbl.c.scenario_id == scenario_id)
            .values(config_json=canonical_dumps(dict(config)))
        )


# ---------------------------------------------------------------------------
# Base resolution + override validation
# ---------------------------------------------------------------------------


def resolve_base_methodology(ctx: Context, base: Methodology | str | None) -> Methodology:
    """Resolve a base-methodology reference to a :class:`Methodology`.

    ``None``/``"default"`` -> the built-in preset; a :class:`Methodology`
    instance -> itself; a 64-hex fingerprint -> the stored
    ``methodology_version.params_json`` (404 via ``KeyError`` when unknown);
    anything else -> ``ValueError`` (422).
    """
    if base is None or base == "default":
        return Methodology()
    if isinstance(base, Methodology):
        return base
    if isinstance(base, str) and len(base) == _FINGERPRINT_LEN:
        with unit_of_work(ctx) as conn:  # read-only; works on Engine/Session/Conn
            params_json = conn.execute(
                select(MethodologyVersion.params_json).where(MethodologyVersion.fingerprint == base)
            ).scalar_one_or_none()
        if params_json is None:
            raise KeyError(f"methodology {base!r} not found")
        return Methodology.model_validate(json.loads(str(params_json)))
    raise ValueError(
        f"unknown base methodology reference {base!r}: expected 'default', "
        "a stored 64-hex fingerprint, or an inline methodology object"
    )


def validate_overrides(overrides: Mapping[str, Any]) -> dict[str, Any]:
    """Check every override key is a real ``Methodology`` field.

    Nested weight sub-objects (``position_weights``, ``family_weights``) are
    merged shallowly by ``Methodology.with_overrides``; their leaf keys are
    validated by pydantic (``extra="forbid"``) at merge time.
    """
    if not isinstance(overrides, Mapping):
        raise ValueError("overrides must be an object mapping field names to values")
    fields = set(Methodology.model_fields)
    bad = sorted(str(k) for k in overrides if str(k) not in fields)
    if bad:
        raise ValueError("unknown methodology override key(s): " + ", ".join(repr(k) for k in bad))
    return dict(overrides)


def merged_methodology(base: Methodology, overrides: Mapping[str, Any]) -> Methodology:
    """Validate ``overrides`` and merge them onto ``base``.

    Raises ``ValueError`` for unknown keys and pydantic ``ValidationError``
    (-> HTTP 422) for type/range/cross-field violations.
    """
    clean = validate_overrides(overrides)
    if not clean:
        return base
    return base.with_overrides(clean)


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------


def create_scenario(
    ctx: Context,
    *,
    name: str,
    description: str | None = None,
    base: Methodology | str | None = None,
    overrides: Mapping[str, Any] | None = None,
    universe_filter: Iterable[str] | None = None,
) -> dict[str, Any]:
    """Persist a scenario; returns its view dict (includes ``scenario_id``).

    The base methodology is resolved, fully validated (merged with
    ``overrides`` must produce a valid ``Methodology``) and stored as
    immutable params + fingerprint inside ``config_json``.
    """
    if not name or not name.strip():
        raise ValueError("scenario name must be non-empty")
    base_methodology = resolve_base_methodology(ctx, base)
    clean_overrides = validate_overrides(overrides or {})
    # Fail fast at creation time if the merged methodology is invalid.
    merged_methodology(base_methodology, clean_overrides)
    fingerprint = base_methodology.fingerprint()
    ref = "inline" if isinstance(base, Methodology) else (base or "default")
    config = _empty_config()
    config.update(
        {
            "description": description,
            "base": {
                "ref": ref if isinstance(ref, str) else "inline",
                "fingerprint": fingerprint,
                "params": json.loads(base_methodology.canonical_json()),
            },
            "overrides": clean_overrides,
            "universe_filter": (
                sorted({str(s) for s in universe_filter}) if universe_filter is not None else None
            ),
        }
    )
    scenario_id = _new_scenario_id()
    _insert_scenario(ctx, scenario_id=scenario_id, name=name.strip(), config=config)
    return get_scenario(ctx, scenario_id)


def get_scenario(ctx: Context, scenario_id: str) -> dict[str, Any]:
    """Fetch one scenario view; ``KeyError`` (404) when unknown."""
    return _scenario_view(_get_scenario_row(ctx, scenario_id))


def list_scenarios(
    ctx: Context, *, limit: int = 50, offset: int = 0
) -> tuple[list[dict[str, Any]], int]:
    """List scenario views newest-first; returns ``(page, total)``."""
    tbl = table_of(Scenario)
    stmt = select(tbl).order_by(tbl.c.created_at.desc(), tbl.c.scenario_id)
    if isinstance(ctx, Engine):
        with ctx.connect() as conn:
            total = int(conn.execute(select(func.count()).select_from(tbl)).scalar_one())
            rows = [dict(r) for r in conn.execute(stmt.limit(limit).offset(offset)).mappings()]
    else:
        total = int(ctx.execute(select(func.count()).select_from(tbl)).scalar_one())
        rows = [dict(r) for r in ctx.execute(stmt.limit(limit).offset(offset)).mappings()]
    return [_scenario_view(r) for r in rows], total


# ---------------------------------------------------------------------------
# Execution
# ---------------------------------------------------------------------------


def _scenario_methodology(view: Mapping[str, Any]) -> Methodology:
    """Rebuild a scenario's *effective* methodology (base + overrides)."""
    base = view.get("base_methodology")
    if not isinstance(base, Methodology):
        raise ValueError("scenario base methodology is missing or failed validation")
    return merged_methodology(base, view.get("overrides") or {})


def _record_execution(
    ctx: Context,
    scenario_id: str,
    *,
    run_id: str,
    as_of_date: date,
    knowledge_cutoff: datetime,
    universe_filter: list[str] | None,
    methodology_fingerprint: str,
    created: bool,
) -> None:
    """Append ``run_id`` to the scenario's config (linkage lives in JSON —
    the ``scenario`` table has no ``result_run_id`` column)."""
    row = _get_scenario_row(ctx, scenario_id)
    config = _parse_config(row.get("config_json"))
    run_ids = [str(r) for r in config["run_ids"]]
    if run_id not in run_ids:
        run_ids.append(run_id)
    config["run_ids"] = run_ids
    config["last_run_id"] = run_id
    config["last_execution"] = {
        "run_id": run_id,
        "as_of_date": as_of_date.isoformat(),
        "knowledge_cutoff": knowledge_cutoff.isoformat(),
        "universe_filter": universe_filter,
        "methodology_fingerprint": methodology_fingerprint,
        "created": created,
        "at": datetime.now(UTC).isoformat(),
    }
    _update_scenario_config(ctx, scenario_id, config)


def _spec_parts(
    ctx: Context, spec: Mapping[str, Any]
) -> tuple[str | None, str | None, Methodology, dict[str, Any], list[str] | None]:
    """Normalize an inline scenario spec into (name, description, base,
    overrides, universe_filter)."""
    name = spec.get("name")
    description = spec.get("description")
    base = resolve_base_methodology(ctx, spec.get("base_methodology"))
    overrides = validate_overrides(spec.get("overrides") or {})
    merged_methodology(base, overrides)  # validate eagerly
    universe = spec.get("universe_filter")
    if isinstance(universe, str):
        raise ValueError("universe_filter must be a list of security ids, not a str")
    universe_list = (
        sorted({str(s) for s in universe})
        if universe is not None and not isinstance(universe, str)
        else None
    )
    return (
        str(name) if name is not None else None,
        str(description) if description is not None else None,
        base,
        overrides,
        universe_list,
    )


def execute_scenario(
    coordinator: JobCoordinator,
    *,
    as_of_date: date,
    scenario_id: str | None = None,
    spec: Mapping[str, Any] | None = None,
    persist: bool = False,
    knowledge_cutoff: datetime | None = None,
    universe_filter: Iterable[str] | None = None,
) -> dict[str, Any]:
    """Execute a stored (``scenario_id``) or inline (``spec``) scenario.

    Resolves the effective methodology (base + overrides), registers it as a
    ``methodology_version`` and submits an idempotent ``calc_run`` through
    the coordinator. For stored scenarios the run is linked back into the
    scenario config (``last_run_id``/``run_ids``). With ``persist=True`` an
    inline ``spec`` is first saved as a scenario row.

    ``coordinator.engine`` is used for all DB access — the coordinator and
    the scenario store must point at the same database.
    """
    engine = coordinator.engine
    name: str | None = None
    base_methodology: Methodology | None = None
    base_universe: list[str] | None = None

    if scenario_id is not None:
        if spec is not None:
            raise ValueError("pass either scenario_id or spec, not both")
        view = get_scenario(engine, scenario_id)
        methodology = _scenario_methodology(view)  # raises if base invalid
        base_candidate = view.get("base_methodology")
        base_methodology = (
            base_candidate if isinstance(base_candidate, Methodology) else Methodology()
        )
        base_universe = view.get("universe_filter")
    elif spec is not None:
        name, description, base, overrides, spec_universe = _spec_parts(engine, spec)
        base_methodology = base
        methodology = merged_methodology(base, overrides)
        base_universe = spec_universe
        if persist:
            created = create_scenario(
                engine,
                name=name or "inline-scenario",
                description=description,
                base=base,
                overrides=overrides,
                universe_filter=spec_universe,
            )
            scenario_id = str(created["scenario_id"])
    else:
        raise ValueError("execute_scenario requires scenario_id or spec")
    assert base_methodology is not None  # set on every non-raising path

    cutoff = knowledge_cutoff or default_knowledge_cutoff(as_of_date)
    if cutoff.tzinfo is None or cutoff.utcoffset() is None:
        cutoff = cutoff.replace(tzinfo=UTC)
    else:
        cutoff = cutoff.astimezone(UTC)

    effective_universe = (
        sorted({str(s) for s in universe_filter}) if universe_filter is not None else base_universe
    )

    invalidation = invalidation_report(base_methodology, methodology)
    submit = coordinator.submit(
        methodology=methodology,
        as_of_date=as_of_date,
        knowledge_cutoff=cutoff,
        universe_filter=effective_universe,
    )
    if scenario_id is not None:
        _record_execution(
            engine,
            scenario_id,
            run_id=submit.run_id,
            as_of_date=as_of_date,
            knowledge_cutoff=cutoff,
            universe_filter=effective_universe,
            methodology_fingerprint=methodology.fingerprint(),
            created=submit.created,
        )
    return {
        "scenario_id": scenario_id,
        "name": name,
        "run_id": submit.run_id,
        "status": submit.status,
        "created": submit.created,
        "methodology_fingerprint": methodology.fingerprint(),
        "as_of_date": as_of_date,
        "knowledge_cutoff": cutoff,
        "universe_filter": effective_universe,
        "invalidation": invalidation,
    }


# ---------------------------------------------------------------------------
# Run comparison
# ---------------------------------------------------------------------------


def _universe_list(raw: object) -> list[str] | None:
    if not isinstance(raw, str) or not raw:
        return None
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if isinstance(value, list) and all(isinstance(s, str) for s in value):
        return sorted(str(s) for s in value)
    return None


def _fingerprint_for_version(engine: Engine, version_id: int) -> str | None:
    with engine.connect() as conn:
        value = conn.execute(
            select(MethodologyVersion.fingerprint).where(MethodologyVersion.id == version_id)
        ).scalar_one_or_none()
    return str(value) if value is not None else None


def _composite_rows(engine: Engine, run_id: str) -> dict[str, dict[str, Any]]:
    stmt = select(AnalystComposite).where(AnalystComposite.run_id == run_id)
    with engine.connect() as conn:
        return {str(r["analyst_id"]): dict(r) for r in conn.execute(stmt).mappings()}


def _metric_rows(engine: Engine, run_id: str) -> dict[tuple[str, str], dict[str, Any]]:
    stmt = select(AnalystMetric).where(AnalystMetric.run_id == run_id)
    with engine.connect() as conn:
        return {
            (str(r["analyst_id"]), str(r["metric_code"])): dict(r)
            for r in conn.execute(stmt).mappings()
        }


def _pearson(xs: Sequence[float], ys: Sequence[float]) -> float | None:
    n = len(xs)
    if n < 2:
        return None
    mx = sum(xs) / n
    my = sum(ys) / n
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys, strict=True))
    sxx = sum((x - mx) ** 2 for x in xs)
    syy = sum((y - my) ** 2 for y in ys)
    if sxx <= 0.0 or syy <= 0.0:
        return None
    return float(sxy / (sxx * syy) ** 0.5)


def _transition(is_ranked_a: bool, is_ranked_b: bool) -> str:
    if is_ranked_a and is_ranked_b:
        return "ranked"
    if is_ranked_a:
        return "lost_rank"
    if is_ranked_b:
        return "gained_rank"
    return "unranked"


def compare_runs(ctx: Context, run_a: str, run_b: str) -> dict[str, Any]:
    """Extended delta report between two ``calc_run`` results.

    Universe compatibility is reported explicitly: runs are *compatible*
    only when their ``universe_filter`` sets are identical (both ``None``
    counts as identical). When both filters are concrete lists an
    ``intersection`` is provided; deltas are still computed over all
    commonly-present analysts regardless — callers must check
    ``universe.compatible`` before interpreting magnitudes.
    """
    if not isinstance(ctx, Engine):
        raise TypeError("compare_runs requires an Engine context")
    engine = ctx
    row_a = runs_repo.get_calc_run(engine, run_a)
    row_b = runs_repo.get_calc_run(engine, run_b)
    if row_a is None:
        raise KeyError(f"calc_run {run_a!r} not found")
    if row_b is None:
        raise KeyError(f"calc_run {run_b!r} not found")

    uni_a = _universe_list(row_a.get("universe_filter_json"))
    uni_b = _universe_list(row_b.get("universe_filter_json"))
    universe = {
        "a": uni_a,
        "b": uni_b,
        "compatible": uni_a == uni_b,
        "intersection": (
            sorted(set(uni_a) & set(uni_b)) if uni_a is not None and uni_b is not None else None
        ),
    }

    fp_a = _fingerprint_for_version(engine, int(row_a["methodology_version_id"]))
    fp_b = _fingerprint_for_version(engine, int(row_b["methodology_version_id"]))

    comp_a = _composite_rows(engine, run_a)
    comp_b = _composite_rows(engine, run_b)
    met_a = _metric_rows(engine, run_a)
    met_b = _metric_rows(engine, run_b)

    all_analysts = sorted(set(comp_a) | set(comp_b))
    analysts: list[dict[str, Any]] = []
    transitions = {"ranked": 0, "lost_rank": 0, "gained_rank": 0, "unranked": 0}
    only_in_a = 0
    only_in_b = 0
    spearman_pairs: list[tuple[float, float]] = []

    for analyst_id in all_analysts:
        ca = comp_a.get(analyst_id)
        cb = comp_b.get(analyst_id)
        if ca is None:
            only_in_b += 1
        elif cb is None:
            only_in_a += 1
        score_a = ca.get("composite_score") if ca else None
        score_b = cb.get("composite_score") if cb else None
        score_delta = (
            float(score_b) - float(score_a) if score_a is not None and score_b is not None else None
        )
        rank_a = ca.get("rank_overall") if ca else None
        rank_b = cb.get("rank_overall") if cb else None
        rank_delta = (
            int(rank_b) - int(rank_a) if rank_a is not None and rank_b is not None else None
        )
        peer_a = ca.get("rank_in_peer_group") if ca else None
        peer_b = cb.get("rank_in_peer_group") if cb else None
        peer_delta = (
            int(peer_b) - int(peer_a) if peer_a is not None and peer_b is not None else None
        )
        is_ranked_a = bool(ca.get("is_ranked")) if ca else False
        is_ranked_b = bool(cb.get("is_ranked")) if cb else False
        transition = (
            _transition(is_ranked_a, is_ranked_b)
            if ca is not None and cb is not None
            else ("only_in_a" if ca is not None else "only_in_b")
        )
        if ca is not None and cb is not None:
            transitions[transition] += 1
        if (
            ca is not None
            and cb is not None
            and is_ranked_a
            and is_ranked_b
            and rank_a is not None
            and rank_b is not None
        ):
            spearman_pairs.append((float(rank_a), float(rank_b)))
        analysts.append(
            {
                "analyst_id": analyst_id,
                "in_a": ca is not None,
                "in_b": cb is not None,
                "composite_score_a": float(score_a) if score_a is not None else None,
                "composite_score_b": float(score_b) if score_b is not None else None,
                "score_delta": score_delta,
                "rank_overall_a": int(rank_a) if rank_a is not None else None,
                "rank_overall_b": int(rank_b) if rank_b is not None else None,
                "rank_delta": rank_delta,
                "rank_in_peer_group_a": int(peer_a) if peer_a is not None else None,
                "rank_in_peer_group_b": int(peer_b) if peer_b is not None else None,
                "peer_rank_delta": peer_delta,
                "is_ranked_a": is_ranked_a,
                "is_ranked_b": is_ranked_b,
                "transition": transition,
            }
        )

    # Spearman rank correlation over analysts ranked in BOTH runs. The
    # stored ``rank_overall`` values are already ranks, so Spearman's rho is
    # the Pearson correlation of the rank pairs (ties in rank_overall are
    # kept as-is; peer ranks are dense enough that this is exact in
    # practice).
    spearman: float | None = None
    if spearman_pairs:
        spearman = _pearson([p[0] for p in spearman_pairs], [p[1] for p in spearman_pairs])

    # Metric-level deltas on ``value`` (b - a) over (analyst, metric) pairs
    # where both sides are non-null.
    metric_acc: dict[str, dict[str, Any]] = {}
    common_keys = sorted(set(met_a) & set(met_b))
    for key in common_keys:
        va = met_a[key].get("value")
        vb = met_b[key].get("value")
        if va is None or vb is None:
            continue
        code = key[1]
        acc = metric_acc.setdefault(code, {"n": 0, "sum": 0.0, "sum_abs": 0.0, "max_abs": 0.0})
        delta = float(vb) - float(va)
        acc["n"] += 1
        acc["sum"] += delta
        acc["sum_abs"] += abs(delta)
        acc["max_abs"] = max(acc["max_abs"], abs(delta))

    metric_deltas: list[dict[str, Any]] = []
    family_acc: dict[str, dict[str, Any]] = {}
    for code in sorted(metric_acc):
        acc = metric_acc[code]
        n = int(acc["n"])
        mean_delta = float(acc["sum"]) / n
        try:
            family = metric_definition(code).family.value
        except KeyError:
            family = "other"
        metric_deltas.append(
            {
                "metric_code": code,
                "family": family,
                "n_pairs": n,
                "mean_delta": mean_delta,
                "mean_abs_delta": float(acc["sum_abs"]) / n,
                "max_abs_delta": float(acc["max_abs"]),
            }
        )
        fam = family_acc.setdefault(family, {"n_metrics": 0, "sum": 0.0})
        fam["n_metrics"] += 1
        fam["sum"] += mean_delta
    # Largest average movement first.
    metric_deltas.sort(key=lambda m: -abs(float(m["mean_abs_delta"])))
    family_deltas = [
        {
            "family": fam,
            "n_metrics": int(v["n_metrics"]),
            # Unweighted mean of per-metric mean deltas (native units differ
            # across metrics; this is a coarse family-level signal only).
            "mean_delta": float(v["sum"]) / int(v["n_metrics"]),
        }
        for fam, v in sorted(family_acc.items())
    ]

    n_common = sum(1 for a in analysts if a["in_a"] and a["in_b"])
    return {
        "run_a_id": run_a,
        "run_b_id": run_b,
        "run_a_status": str(row_a["status"]),
        "run_b_status": str(row_b["status"]),
        "as_of_date_a": row_a["as_of_date"].isoformat()
        if isinstance(row_a["as_of_date"], date)
        else str(row_a["as_of_date"]),
        "as_of_date_b": row_b["as_of_date"].isoformat()
        if isinstance(row_b["as_of_date"], date)
        else str(row_b["as_of_date"]),
        "methodology_a_fingerprint": fp_a,
        "methodology_b_fingerprint": fp_b,
        "universe": universe,
        "summary": {
            "n_analysts_a": len(comp_a),
            "n_analysts_b": len(comp_b),
            "n_common": n_common,
            "only_in_a": only_in_a,
            "only_in_b": only_in_b,
            "ranked_to_ranked": transitions["ranked"],
            "ranked_to_unranked": transitions["lost_rank"],
            "unranked_to_ranked": transitions["gained_rank"],
            "unranked_to_unranked": transitions["unranked"],
        },
        "rank_correlation": {
            "method": "spearman_overall_rank",
            "n": len(spearman_pairs),
            "spearman": spearman,
        },
        "analysts": analysts,
        "metric_deltas": metric_deltas,
        "family_deltas": family_deltas,
    }


# ---------------------------------------------------------------------------
# Sensitivity sweep
# ---------------------------------------------------------------------------


def _set_path(payload: dict[str, Any], path: str, value: Any) -> None:
    """Set ``payload`` at a dotted/indexed path like ``a.b`` or ``fixed_horizons_days[0]``.

    Walks dict keys and list indices; the final segment receives ``value``.
    Unknown keys, non-container descent and out-of-range indices raise
    ``ValueError`` (-> 422). Type validity of ``value`` is enforced later by
    ``Methodology.model_validate``.
    """
    if not path or not path.strip():
        raise ValueError("param_path must be non-empty")
    # Tokenize into (key_or_index, is_index) steps.
    steps: list[tuple[str, bool]] = []
    for seg in path.split("."):
        if not seg:
            raise ValueError(f"malformed path {path!r}")
        head, _, rest = seg.partition("[")
        if head:
            steps.append((head, False))
        while rest:
            close = rest.find("]")
            if close < 0:
                raise ValueError(f"malformed path segment {seg!r} in {path!r}")
            idx = rest[:close]
            if not idx.isdigit():
                raise ValueError(f"malformed path segment {seg!r} in {path!r}")
            steps.append((idx, True))
            rest = rest[close + 1 :]
            if rest.startswith("["):
                rest = rest[1:]
            elif rest:
                raise ValueError(f"malformed path segment {seg!r} in {path!r}")
    if not steps:
        raise ValueError(f"malformed path {path!r}")

    node: Any = payload
    for pos, (token, is_index) in enumerate(steps):
        last = pos == len(steps) - 1
        if is_index:
            if not isinstance(node, list):
                raise ValueError(f"cannot index into {type(node).__name__} at {path!r}")
            index = int(token)
            if not 0 <= index < len(node):
                raise ValueError(f"path {path!r} index {index} out of range")
            if last:
                node[index] = value
                return
            node = node[index]
        else:
            if not isinstance(node, dict):
                raise ValueError(f"cannot descend into {type(node).__name__} at {path!r}")
            if token not in node:
                raise ValueError(f"unknown methodology path {path!r}: no field {token!r}")
            if last:
                node[token] = value
                return
            node = node[token]
    raise ValueError(f"could not set path {path!r}")  # pragma: no cover - unreachable


def _sweep_combinations(
    param_path: str | None,
    values: Sequence[Any] | None,
    overrides: Mapping[str, Any] | None,
) -> list[dict[str, Any]]:
    """Expand sweep inputs into a bounded list of per-combo specs.

    Each spec is ``{"param_path": p, "value": v}`` or
    ``{"overrides": {...}}``. Exactly one of ``param_path``/``overrides``
    must be given; ``overrides`` values may be lists (cartesian product).
    """
    if param_path is not None and overrides is not None:
        raise ValueError("pass either param_path or overrides, not both")
    if param_path is not None:
        if values is None:
            raise ValueError("param_path requires a non-empty values list")
        if isinstance(values, (str, bytes)) or not values:
            raise ValueError("values must be a non-empty list")
        combos = [{"param_path": param_path, "value": v} for v in values]
    elif overrides is not None:
        if not overrides:
            raise ValueError("overrides must be non-empty for a sweep")
        keys = list(overrides)
        lists = [
            overrides[k] if isinstance(overrides[k], list | tuple) else [overrides[k]] for k in keys
        ]
        combos = [
            {"overrides": {k: v for k, v in zip(keys, combo, strict=True)}}
            for combo in itertools.product(*lists)
        ]
    else:
        raise ValueError("sensitivity sweep requires param_path or overrides")
    if len(combos) > MAX_SWEEP_COMBOS:
        raise ValueError(
            f"sensitivity sweep of {len(combos)} combinations exceeds the "
            f"maximum of {MAX_SWEEP_COMBOS}"
        )
    return combos


def _combo_methodology(base: Methodology, spec: Mapping[str, Any]) -> Methodology:
    if "param_path" in spec:
        payload = json.loads(base.canonical_json())
        _set_path(payload, str(spec["param_path"]), spec["value"])
        return Methodology.model_validate(payload)
    return base.with_overrides(dict(spec["overrides"]))


def sensitivity_sweep(
    coordinator: JobCoordinator,
    *,
    as_of_date: date,
    param_path: str | None = None,
    values: Sequence[Any] | None = None,
    overrides: Mapping[str, Any] | None = None,
    base: Methodology | str | None = None,
    scenario_id: str | None = None,
    knowledge_cutoff: datetime | None = None,
    universe_filter: Iterable[str] | None = None,
) -> dict[str, Any]:
    """Submit a bounded parameter sweep; each combo is a methodology version
    + idempotent ``calc_run`` via the coordinator.

    ``scenario_id`` (optional) resolves the sweep base to that scenario's
    *effective* methodology (base + stored overrides); otherwise ``base``
    (``None`` -> default preset) is used. Raises ``ValueError`` (-> 422) when
    the combination count exceeds :data:`MAX_SWEEP_COMBOS` or inputs are
    malformed — nothing is submitted in that case.
    """
    engine = coordinator.engine
    combos = _sweep_combinations(param_path, values, overrides)

    if scenario_id is not None:
        view = get_scenario(engine, scenario_id)
        base_methodology = _scenario_methodology(view)
        spec_universe = view.get("universe_filter")
    else:
        base_methodology = resolve_base_methodology(engine, base)
        spec_universe = None
    effective_universe = (
        sorted({str(s) for s in universe_filter})
        if universe_filter is not None
        else (list(spec_universe) if spec_universe else None)
    )

    cutoff = knowledge_cutoff or default_knowledge_cutoff(as_of_date)
    if cutoff.tzinfo is None or cutoff.utcoffset() is None:
        cutoff = cutoff.replace(tzinfo=UTC)
    else:
        cutoff = cutoff.astimezone(UTC)

    jobs: list[dict[str, Any]] = []
    for index, spec in enumerate(combos):
        methodology = _combo_methodology(base_methodology, spec)
        submit = coordinator.submit(
            methodology=methodology,
            as_of_date=as_of_date,
            knowledge_cutoff=cutoff,
            universe_filter=effective_universe,
        )
        jobs.append(
            {
                "index": index,
                "spec": dict(spec),
                "methodology_fingerprint": methodology.fingerprint(),
                "run_id": submit.run_id,
                "status": submit.status,
                "created": submit.created,
            }
        )

    sweep_key = {
        "as_of_date": as_of_date.isoformat(),
        "base_fingerprint": base_methodology.fingerprint(),
        "combos": combos,
        "knowledge_cutoff": cutoff.isoformat(),
        "universe_filter": effective_universe,
    }
    sweep_id = "sweep-" + hashlib.sha256(canonical_dumps(sweep_key).encode()).hexdigest()[:24]

    if scenario_id is not None:
        row = _get_scenario_row(engine, scenario_id)
        config = _parse_config(row.get("config_json"))
        config["sweeps"].append(
            {
                "sweep_id": sweep_id,
                "at": datetime.now(UTC).isoformat(),
                "as_of_date": as_of_date.isoformat(),
                "knowledge_cutoff": cutoff.isoformat(),
                "universe_filter": effective_universe,
                "combos": combos,
                "run_ids": [j["run_id"] for j in jobs],
            }
        )
        _update_scenario_config(engine, scenario_id, config)

    return {
        "sweep_id": sweep_id,
        "count": len(jobs),
        "as_of_date": as_of_date,
        "knowledge_cutoff": cutoff,
        "universe_filter": effective_universe,
        "scenario_id": scenario_id,
        "jobs": jobs,
    }


# ---------------------------------------------------------------------------
# Invalidation report
# ---------------------------------------------------------------------------


def _diff_values(a: Any, b: Any, path: str, out: list[dict[str, Any]]) -> None:
    """Leaf-level diff of two JSON-ish values (same shape as the
    ``/methodologies/diff`` endpoint's ``FieldChange`` rows)."""
    if isinstance(a, dict) and isinstance(b, dict):
        for key in sorted(set(a) | set(b), key=str):
            child = f"{path}.{key}" if path else str(key)
            if key not in a:
                out.append({"path": child, "a": None, "b": b[key], "a_missing": True})
            elif key not in b:
                out.append({"path": child, "a": a[key], "b": None, "b_missing": True})
            else:
                _diff_values(a[key], b[key], child, out)
        return
    if isinstance(a, list) and isinstance(b, list):
        for i in range(max(len(a), len(b))):
            child = f"{path}[{i}]"
            if i >= len(a):
                out.append({"path": child, "a": None, "b": b[i], "a_missing": True})
            elif i >= len(b):
                out.append({"path": child, "a": a[i], "b": None, "b_missing": True})
            else:
                _diff_values(a[i], b[i], child, out)
        return
    if a != b:
        out.append({"path": path or "$", "a": a, "b": b})


def invalidation_report(methodology_a: Methodology, methodology_b: Methodology) -> dict[str, Any]:
    """Which pipeline stages are invalidated by a methodology change.

    Stage cache fingerprints chain upstreams (``contracts.stage_fingerprint``),
    so a change to any field consumed by stage *i* also changes the cache keys
    of every stage after *i*. ``stages`` reports, per stage, the
    ``STAGE_METHODOLOGY_FIELDS`` entries that differ directly;
    ``affected_stages``/``fingerprint_changes`` mark every stage whose cache
    fingerprint will differ (all stages from the earliest direct change
    onward). E.g. a ``position_weights`` change flags EXPOSURES and all
    downstream stages (METRICS, STATISTICS).
    """
    params_a = methodology_a.model_dump(mode="json")
    params_b = methodology_b.model_dump(mode="json")

    changed_fields: list[dict[str, Any]] = []
    _diff_values(params_a, params_b, "", changed_fields)

    direct: dict[str, list[str]] = {}
    first_affected_idx: int | None = None
    stage_rows: list[dict[str, Any]] = []
    for idx, stage in enumerate(STAGE_ORDER):
        fields = [f for f in STAGE_METHODOLOGY_FIELDS[stage] if params_a.get(f) != params_b.get(f)]
        direct[stage.value] = fields
        if fields and first_affected_idx is None:
            first_affected_idx = idx

    for idx, stage in enumerate(STAGE_ORDER):
        fingerprint_changes = first_affected_idx is not None and idx >= first_affected_idx
        stage_rows.append(
            {
                "stage": stage.value,
                "fields_changed": direct[stage.value],
                "fingerprint_changes": fingerprint_changes,
            }
        )

    return {
        "methodology_a_fingerprint": methodology_a.fingerprint(),
        "methodology_b_fingerprint": methodology_b.fingerprint(),
        "identical": not changed_fields,
        "changed_fields": changed_fields,
        "stages": stage_rows,
        "affected_stages": [s["stage"] for s in stage_rows if s["fingerprint_changes"]],
        "first_affected_stage": (
            STAGE_ORDER[first_affected_idx].value if first_affected_idx is not None else None
        ),
    }


__all__ = [
    "MAX_SWEEP_COMBOS",
    "compare_runs",
    "create_scenario",
    "execute_scenario",
    "get_scenario",
    "invalidation_report",
    "list_scenarios",
    "merged_methodology",
    "resolve_base_methodology",
    "sensitivity_sweep",
    "validate_overrides",
]
