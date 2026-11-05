"""Read-side query layer for the analyst profile and leaderboard endpoints.

All SQL lives here. Every function is scoped to a single ``run_id`` — derived
tables are run-scoped and immutable, so results never mix runs. Queries are
plain SQLAlchemy Core selects with bound parameters; pagination is
``limit``/``offset`` with ``limit`` clamped to ``MAX_LIMIT`` and sort keys
restricted to per-endpoint whitelists.

No engine calls happen here: the single-rating arithmetic explanation is
assembled from stored ``interval_exposure`` / ``interval_return`` /
``interval_contribution`` rows.
"""

import hashlib
import math
from collections.abc import Iterable, Mapping, Sequence
from datetime import date
from typing import Any

from sqlalchemy import Engine, func, nullslast, select

from callscore_core.enums import MetricFamily
from callscore_core.metrics import COMPOSITE_SPEC, METRIC_DEFINITIONS, MetricDefinition
from callscore_store.models import (
    Analyst,
    AnalystComposite,
    AnalystCoverage,
    AnalystEmployment,
    AnalystMetric,
    CalcRun,
    EventWindow,
    Firm,
    IntervalContribution,
    IntervalExposure,
    IntervalReturn,
    MethodologyVersion,
    PortfolioNav,
    PortfolioPosition,
    RatingEvent,
    RatingInterval,
    TargetOutcome,
)
from callscore_store.repositories.base import Context

MAX_LIMIT = 500
DEFAULT_LIMIT = 50
MAX_PORTFOLIO_POINTS = 2000
MAX_POSITIONS = 500

KEY_METRICS: tuple[str, ...] = (
    "mean_contribution",
    "hit_rate",
    "information_ratio",
    "target_hit_rate",
)

ANALYST_SORTS: frozenset[str] = frozenset(
    {"score", "rank_overall", "rank_in_peer_group", "analyst_id", "name", "n_obs"}
)
LEADERBOARD_BASE_SORTS: frozenset[str] = frozenset(
    {"score", "rank_overall", "rank_in_peer_group", "analyst_id", "name", "n_obs"}
)

_CATALOG: dict[str, MetricDefinition] = {d.code: d for d in METRIC_DEFINITIONS}
_SCORED = tuple(d for d in METRIC_DEFINITIONS if d.in_composite)
_FAMILY_ORDER: tuple[MetricFamily, ...] = tuple(MetricFamily)


class QueryError(ValueError):
    """Invalid query parameter (maps to HTTP 422 in the router layer)."""


# --------------------------------------------------------------------------- #
# Run provenance / ETag
# --------------------------------------------------------------------------- #


def get_run(ctx: Context, run_id: str) -> dict[str, Any] | None:
    """``calc_run`` row plus methodology fingerprint; ``None`` if unknown."""
    stmt = (
        select(
            CalcRun.run_id,
            CalcRun.status,
            CalcRun.as_of_date,
            CalcRun.knowledge_cutoff,
            CalcRun.source_fingerprint,
            CalcRun.universe_filter_json,
            MethodologyVersion.fingerprint.label("methodology_fingerprint"),
        )
        .join(MethodologyVersion, CalcRun.methodology_version_id == MethodologyVersion.id)
        .where(CalcRun.run_id == run_id)
    )
    row = _one(ctx, stmt)
    return None if row is None else dict(row)


def run_etag(run: Mapping[str, Any]) -> str:
    """ETag for immutable run-scoped data: hash of run_id + fingerprint."""
    payload = f"{run['run_id']}:{run['methodology_fingerprint']}"
    return hashlib.sha256(payload.encode()).hexdigest()


def provenance(run: Mapping[str, Any]) -> dict[str, Any]:
    """Provenance block embedded in run-scoped responses."""
    return {
        "run_id": run["run_id"],
        "methodology_fingerprint": run["methodology_fingerprint"],
        "as_of_date": _iso(run["as_of_date"]),
        "run_status": run["status"],
    }


def _iso(value: Any) -> Any:
    if isinstance(value, date):
        return value.isoformat()
    return value


# --------------------------------------------------------------------------- #
# Pagination / sort helpers
# --------------------------------------------------------------------------- #


def clamp_limit(limit: int | None, *, default: int = DEFAULT_LIMIT) -> int:
    if limit is None:
        return default
    if limit < 1:
        raise QueryError("limit must be >= 1")
    return min(limit, MAX_LIMIT)


def clamp_offset(offset: int | None) -> int:
    if offset is None:
        return 0
    if offset < 0:
        raise QueryError("offset must be >= 0")
    return offset


def _order_by(
    sort: str | None,
    allowed: Mapping[str, Any],
    *,
    default: str,
    order: str,
    tiebreak: Any,
) -> list[Any]:
    """Build a stable ORDER BY: whitelisted sort key + mandatory tiebreak."""
    if sort is None:
        sort = default
    if sort not in allowed:
        raise QueryError(f"unsupported sort {sort!r}; allowed: {sorted(allowed)}")
    col = allowed[sort]
    direction = col.desc() if order == "desc" else col.asc()
    # Nulls always sort last so unranked/unavailable rows never lead a page.
    return [nullslast(direction), tiebreak.asc()]


def _one(ctx: Context, stmt: Any) -> dict[str, Any] | None:
    if isinstance(ctx, Engine):
        with ctx.connect() as conn:
            row = conn.execute(stmt).mappings().first()
    else:
        row = ctx.execute(stmt).mappings().first()
    return None if row is None else dict(row)


def _all(ctx: Context, stmt: Any) -> list[dict[str, Any]]:
    if isinstance(ctx, Engine):
        with ctx.connect() as conn:
            return [dict(r) for r in conn.execute(stmt).mappings()]
    return [dict(r) for r in ctx.execute(stmt).mappings()]


def _count(ctx: Context, stmt: Any) -> int:
    sub = stmt.order_by(None).subquery()
    count_stmt = select(func.count()).select_from(sub)
    if isinstance(ctx, Engine):
        with ctx.connect() as conn:
            return int(conn.execute(count_stmt).scalar_one())
    return int(ctx.execute(count_stmt).scalar_one())


def analyst_exists(ctx: Context, analyst_id: str) -> bool:
    return _one(ctx, select(Analyst.analyst_id).where(Analyst.analyst_id == analyst_id)) is not None


# --------------------------------------------------------------------------- #
# Shared derived-data fetchers (one query each — never per-row)
# --------------------------------------------------------------------------- #


def _investable_counts(ctx: Context, run_id: str) -> dict[str, int]:
    """``analyst_id -> count(is_investable intervals)`` for the run."""
    stmt = (
        select(RatingInterval.analyst_id, func.count().label("n_obs"))
        .where(RatingInterval.run_id == run_id, RatingInterval.is_investable.is_(True))
        .group_by(RatingInterval.analyst_id)
    )
    return {str(r["analyst_id"]): int(r["n_obs"]) for r in _all(ctx, stmt)}


def _metric_rows(
    ctx: Context,
    run_id: str,
    *,
    analyst_id: str | None = None,
    codes: Iterable[str] | None = None,
) -> list[dict[str, Any]]:
    """All ``analyst_metric`` rows for a run (optionally one analyst/codes)."""
    stmt = select(AnalystMetric).where(AnalystMetric.run_id == run_id)
    if analyst_id is not None:
        stmt = stmt.where(AnalystMetric.analyst_id == analyst_id)
    if codes is not None:
        stmt = stmt.where(AnalystMetric.metric_code.in_(list(codes)))
    return _all(ctx, stmt)


def _composite_rows(ctx: Context, run_id: str) -> list[dict[str, Any]]:
    return _all(ctx, select(AnalystComposite).where(AnalystComposite.run_id == run_id))


def _metric_catalog() -> dict[str, dict[str, Any]]:
    """``metric_code -> catalog metadata`` for response enrichment."""
    return {
        d.code: {
            "family": d.family.value,
            "unit": d.unit,
            "direction": d.direction,
            "minimum_observations": d.minimum_observations,
            "in_composite": d.in_composite,
            "formula": d.formula,
            "unavailable_when": d.unavailable_when,
        }
        for d in METRIC_DEFINITIONS
    }


def _metric_detail(row: Mapping[str, Any]) -> dict[str, Any]:
    """Serialize one ``analyst_metric`` row + catalog metadata."""
    catalog = _metric_catalog().get(str(row["metric_code"]), {})
    out = {
        k: row.get(k)
        for k in (
            "metric_code",
            "value",
            "n_obs",
            "std_err",
            "t_stat",
            "ci_low",
            "ci_high",
            "shrunk_value",
            "peer_percentile",
            "is_significant",
            "unavailable_reason",
        )
    }
    out.update(catalog)
    return out


def _inline_metrics(rows: Sequence[Mapping[str, Any]]) -> dict[str, dict[str, Any]]:
    """``{metric_code: {value, n_obs, unavailable_reason}}`` for list rows."""
    return {
        str(r["metric_code"]): {
            "value": r.get("value"),
            "n_obs": r.get("n_obs"),
            "unavailable_reason": r.get("unavailable_reason"),
        }
        for r in rows
    }


# --------------------------------------------------------------------------- #
# Composite family breakdown (display-level mirror of engine composite pass 2-3)
# --------------------------------------------------------------------------- #


def family_breakdown(
    metric_rows: Sequence[Mapping[str, Any]],
    composites: Sequence[Mapping[str, Any]],
) -> dict[str, dict[str, float | None]]:
    """Per-analyst per-family mean oriented peer-z score.

    Mirrors ``callscore_engine.composite.compute_composites`` passes 2-3 for
    display only: scored (``in_composite``) catalog metrics are z-scored
    within the analyst's peer group over *ranked* analysts' ``x`` values
    (``shrunk_value`` else ``value``), clipped to ``+-COMPOSITE_SPEC.z_clip``
    and oriented by metric direction; a family's score is the mean of its
    available oriented z values. Stored rows only — no engine calls.
    """
    peer_of = {str(c["analyst_id"]): c.get("peer_group") for c in composites}
    ranked = {str(c["analyst_id"]) for c in composites if c.get("is_ranked")}

    # (analyst, metric) -> (value, x)
    values: dict[tuple[str, str], tuple[float | None, float | None]] = {}
    for r in metric_rows:
        v = r.get("value")
        s = r.get("shrunk_value")
        # x mirrors the engine: shrunk_value when present, else value.
        x = float(s) if s is not None else (float(v) if v is not None else None)
        values[(str(r["analyst_id"]), str(r["metric_code"]))] = (
            None if v is None else float(v),
            x,
        )

    # z stats per (metric_code, peer_group) over ranked analysts.
    z_stats: dict[tuple[str, str], tuple[float, float] | None] = {}
    for definition in _SCORED:
        by_group: dict[str, list[float]] = {}
        for (aid, code), (value, shrunk) in values.items():
            if code != definition.code or aid not in ranked or value is None:
                continue
            group = peer_of.get(aid)
            if group is None:
                continue
            x = shrunk if shrunk is not None else value
            by_group.setdefault(str(group), []).append(x)
        for group, xs in by_group.items():
            if len(xs) < 2:
                z_stats[(definition.code, group)] = None
                continue
            mean = sum(xs) / len(xs)
            var = sum((x - mean) ** 2 for x in xs) / (len(xs) - 1)
            std = math.sqrt(var)
            z_stats[(definition.code, group)] = None if std == 0.0 else (mean, std)

    def oriented_z(analyst: str, definition: MetricDefinition) -> float | None:
        entry = values.get((analyst, definition.code))
        if entry is None:
            return None
        value, shrunk = entry
        if value is None:
            return None
        group = peer_of.get(analyst)
        if group is None:
            return 0.0
        stats = z_stats.get((definition.code, str(group)))
        x = shrunk if shrunk is not None else value
        z = 0.0 if stats is None else (x - stats[0]) / stats[1]
        z = max(-COMPOSITE_SPEC.z_clip, min(COMPOSITE_SPEC.z_clip, z))
        return z * definition.orientation

    out: dict[str, dict[str, float | None]] = {}
    analysts = set(peer_of) | {a for a, _ in values}
    for analyst in analysts:
        per_family: dict[MetricFamily, list[float]] = {}
        for definition in _SCORED:
            z = oriented_z(analyst, definition)
            if z is None:
                continue
            per_family.setdefault(definition.family, []).append(z)
        out[analyst] = {
            family.value: (sum(zs) / len(zs) if zs else None)
            for family in _FAMILY_ORDER
            for zs in [per_family.get(family, [])]
        }
    return out


# --------------------------------------------------------------------------- #
# GET /api/analysts
# --------------------------------------------------------------------------- #


def list_analysts(
    ctx: Context,
    run_id: str,
    *,
    q: str | None = None,
    peer_group: str | None = None,
    eligible: str | None = None,
    sort: str | None = None,
    order: str = "asc",
    limit: int | None = None,
    offset: int | None = None,
) -> dict[str, Any]:
    """Paginated analyst list for one run; returns ``{items,total,...}``."""
    if order not in ("asc", "desc"):
        raise QueryError("order must be 'asc' or 'desc'")
    lim = clamp_limit(limit)
    off = clamp_offset(offset)

    ac = AnalystComposite.__table__
    an = Analyst.__table__
    fm = Firm.__table__
    n_obs_sq = (
        select(RatingInterval.analyst_id, func.count().label("n_obs"))
        .where(RatingInterval.run_id == run_id, RatingInterval.is_investable.is_(True))
        .group_by(RatingInterval.analyst_id)
        .subquery()
    )

    stmt = (
        select(
            ac.c.analyst_id,
            an.c.name,
            an.c.firm_id,
            fm.c.name.label("firm_name"),
            ac.c.peer_group,
            ac.c.composite_score,
            ac.c.rank_overall,
            ac.c.rank_in_peer_group,
            ac.c.is_ranked,
            ac.c.unranked_reason,
            func.coalesce(n_obs_sq.c.n_obs, 0).label("n_obs"),
        )
        .select_from(
            ac.join(an, ac.c.analyst_id == an.c.analyst_id, isouter=True)
            .join(fm, an.c.firm_id == fm.c.firm_id, isouter=True)
            .join(n_obs_sq, ac.c.analyst_id == n_obs_sq.c.analyst_id, isouter=True)
        )
        .where(ac.c.run_id == run_id)
    )
    if q:
        pattern = f"%{q}%"
        stmt = stmt.where(an.c.analyst_id.ilike(pattern) | an.c.name.ilike(pattern))
    if peer_group is not None:
        stmt = stmt.where(ac.c.peer_group == peer_group)
    if eligible == "ranked":
        stmt = stmt.where(ac.c.is_ranked.is_(True))
    elif eligible == "unranked":
        stmt = stmt.where(ac.c.is_ranked.is_(False))
    elif eligible not in (None, "all"):
        raise QueryError("eligible must be one of: ranked, unranked, all")

    sort_cols: dict[str, Any] = {
        "score": ac.c.composite_score,
        "rank_overall": ac.c.rank_overall,
        "rank_in_peer_group": ac.c.rank_in_peer_group,
        "analyst_id": ac.c.analyst_id,
        "name": an.c.name,
        "n_obs": n_obs_sq.c.n_obs,
    }
    if sort is None:
        sort = "rank_overall"
    order_exprs = _order_by(
        sort, sort_cols, default="rank_overall", order=order, tiebreak=ac.c.analyst_id
    )
    # Default ranking order is rank ascending; score/name/etc. follow `order`.
    total = _count(ctx, stmt)
    rows = _all(ctx, stmt.order_by(*order_exprs).limit(lim).offset(off))

    key_rows = _metric_rows(ctx, run_id, codes=KEY_METRICS)
    key_by_analyst: dict[str, list[dict[str, Any]]] = {}
    for r in key_rows:
        key_by_analyst.setdefault(str(r["analyst_id"]), []).append(r)

    items = []
    for r in rows:
        aid = str(r["analyst_id"])
        items.append(
            {
                "analyst_id": aid,
                "name": r.get("name"),
                "firm_id": r.get("firm_id"),
                "firm_name": r.get("firm_name"),
                "peer_group": r.get("peer_group"),
                "composite_score": r.get("composite_score"),
                "rank_overall": r.get("rank_overall"),
                "rank_in_peer_group": r.get("rank_in_peer_group"),
                "is_ranked": bool(r.get("is_ranked")),
                "unranked_reason": r.get("unranked_reason"),
                "n_obs": int(r.get("n_obs") or 0),
                "key_metrics": _inline_metrics(key_by_analyst.get(aid, [])),
            }
        )
    return {"items": items, "total": total, "limit": lim, "offset": off}


# --------------------------------------------------------------------------- #
# GET /api/analysts/{id}
# --------------------------------------------------------------------------- #


def analyst_profile(ctx: Context, run_id: str, analyst_id: str) -> dict[str, Any] | None:
    """Full profile; ``None`` when the analyst is unknown to the store."""
    an = Analyst.__table__
    fm = Firm.__table__
    row = _one(
        ctx,
        select(an.c.analyst_id, an.c.name, an.c.firm_id, fm.c.name.label("firm_name"))
        .select_from(an.join(fm, an.c.firm_id == fm.c.firm_id, isouter=True))
        .where(an.c.analyst_id == analyst_id),
    )
    if row is None:
        return None

    employment = _all(
        ctx,
        select(AnalystEmployment)
        .where(AnalystEmployment.analyst_id == analyst_id)
        .order_by(AnalystEmployment.from_date, AnalystEmployment.firm_id),
    )
    coverage = _all(
        ctx,
        select(AnalystCoverage)
        .where(AnalystCoverage.analyst_id == analyst_id)
        .order_by(AnalystCoverage.security_id, AnalystCoverage.from_date),
    )
    composite = _one(
        ctx,
        select(AnalystComposite).where(
            AnalystComposite.run_id == run_id, AnalystComposite.analyst_id == analyst_id
        ),
    )
    metric_rows = _metric_rows(ctx, run_id, analyst_id=analyst_id)
    n_obs = _investable_counts(ctx, run_id).get(analyst_id, 0)

    # Family breakdown needs the whole run's metric rows + composites (2 queries).
    all_metrics = _metric_rows(ctx, run_id)
    composites = _composite_rows(ctx, run_id)
    families = family_breakdown(all_metrics, composites).get(analyst_id, {})

    shrunk_used = any(r.get("shrunk_value") is not None for r in metric_rows)
    composite_block: dict[str, Any] | None = None
    if composite is not None:
        composite_block = {
            "peer_group": composite.get("peer_group"),
            "composite_score": composite.get("composite_score"),
            "rank_overall": composite.get("rank_overall"),
            "rank_in_peer_group": composite.get("rank_in_peer_group"),
            "is_ranked": bool(composite.get("is_ranked")),
            "unranked_reason": composite.get("unranked_reason"),
            "n_obs": n_obs,
            "family_scores": families,
            "shrinkage": {
                "method": "empirical_bayes",
                "shrunk_values_present": shrunk_used,
            },
        }

    return {
        "analyst_id": analyst_id,
        "name": row.get("name"),
        "firm_id": row.get("firm_id"),
        "firm_name": row.get("firm_name"),
        "employment_spells": [_spell(e) for e in employment],
        "coverage_spells": [_spell(c) for c in coverage],
        "peer_group": composite.get("peer_group") if composite else None,
        "composite": composite_block,
        "metrics": [_metric_detail(r) for r in metric_rows],
    }


def _spell(row: Mapping[str, Any]) -> dict[str, Any]:
    return {k: _iso(v) for k, v in row.items()}


# --------------------------------------------------------------------------- #
# GET /api/analysts/{id}/metrics
# --------------------------------------------------------------------------- #


def analyst_metrics(
    ctx: Context, run_id: str, analyst_id: str, *, family: str | None = None
) -> list[dict[str, Any]]:
    """Metric drill-down rows; ``family`` filters on the catalog family."""
    rows = _metric_rows(ctx, run_id, analyst_id=analyst_id)
    details = [_metric_detail(r) for r in rows]
    if family is None:
        return details
    if family not in {f.value for f in MetricFamily}:
        raise QueryError(f"unknown metric family {family!r}")
    return [d for d in details if d.get("family") == family]


# --------------------------------------------------------------------------- #
# GET /api/analysts/{id}/ratings
# --------------------------------------------------------------------------- #


def _rating_sort_cols() -> dict[str, Any]:
    ri = RatingInterval.__table__
    return {
        "entry_session": ri.c.entry_session,
        "announced_ts": ri.c.announced_ts,
        "security_id": ri.c.security_id,
        "interval_id": ri.c.interval_id,
    }


def list_ratings(
    ctx: Context,
    run_id: str,
    analyst_id: str,
    *,
    date_from: date | None = None,
    date_to: date | None = None,
    action: str | None = None,
    security: str | None = None,
    sort: str | None = None,
    order: str = "desc",
    limit: int | None = None,
    offset: int | None = None,
) -> dict[str, Any]:
    """Rating events joined to their intervals + to-exit return summary."""
    if order not in ("asc", "desc"):
        raise QueryError("order must be 'asc' or 'desc'")
    lim = clamp_limit(limit)
    off = clamp_offset(offset)

    ri = RatingInterval.__table__
    re_ = RatingEvent.__table__
    ret = IntervalReturn.__table__

    stmt = (
        select(
            ri,
            re_.c.price_target,
            re_.c.pt_currency,
            re_.c.pt_horizon_months,
            ret.c.stock_return.label("ret_stock_return"),
            ret.c.benchmark_return.label("ret_benchmark_return"),
            ret.c.excess_return.label("ret_excess_return"),
            ret.c.terminated_by_delisting.label("ret_terminated_by_delisting"),
            ret.c.unavailable_reason.label("ret_unavailable_reason"),
        )
        .select_from(
            ri.join(re_, ri.c.event_id == re_.c.event_id, isouter=True).join(
                ret,
                (ret.c.run_id == ri.c.run_id)
                & (ret.c.interval_id == ri.c.interval_id)
                & (ret.c.horizon == "to_exit"),
                isouter=True,
            )
        )
        .where(ri.c.run_id == run_id, ri.c.analyst_id == analyst_id)
    )
    if date_from is not None:
        stmt = stmt.where(ri.c.entry_session >= date_from)
    if date_to is not None:
        stmt = stmt.where(ri.c.entry_session <= date_to)
    if action is not None:
        stmt = stmt.where(ri.c.action_type == action)
    if security is not None:
        stmt = stmt.where(ri.c.security_id == security)

    sort_cols = _rating_sort_cols()
    if sort is not None and sort not in sort_cols:
        raise QueryError(f"unsupported sort {sort!r}; allowed: {sorted(sort_cols)}")
    order_exprs = _order_by(
        sort or "entry_session",
        sort_cols,
        default="entry_session",
        order=order,
        tiebreak=ri.c.interval_id,
    )
    total = _count(ctx, stmt)
    rows = _all(ctx, stmt.order_by(*order_exprs).limit(lim).offset(off))

    interval_ids = [str(r["interval_id"]) for r in rows]
    exposures = _legs_by_interval(ctx, run_id, interval_ids)

    items = []
    for r in rows:
        item = _interval_row(r)
        item["price_target"] = r.get("price_target")
        item["pt_currency"] = r.get("pt_currency")
        item["pt_horizon_months"] = r.get("pt_horizon_months")
        item["exposures"] = exposures.get(str(r["interval_id"]), [])
        item["return_summary"] = {
            "stock_return": r.get("ret_stock_return"),
            "benchmark_return": r.get("ret_benchmark_return"),
            "excess_return": r.get("ret_excess_return"),
            "terminated_by_delisting": r.get("ret_terminated_by_delisting"),
            "unavailable_reason": r.get("ret_unavailable_reason"),
        }
        items.append(item)
    return {"items": items, "total": total, "limit": lim, "offset": off}


def _legs_by_interval(
    ctx: Context, run_id: str, interval_ids: Sequence[str]
) -> dict[str, list[dict[str, Any]]]:
    """Exposure legs grouped by interval (one query for the page)."""
    if not interval_ids:
        return {}
    tbl = IntervalExposure.__table__
    stmt = (
        select(tbl)
        .where(tbl.c.run_id == run_id, tbl.c.interval_id.in_(list(interval_ids)))
        .order_by(tbl.c.interval_id, tbl.c.segment_no)
    )
    out: dict[str, list[dict[str, Any]]] = {}
    for r in _all(ctx, stmt):
        iid = str(r.pop("interval_id"))
        r.pop("run_id", None)
        out.setdefault(iid, []).append(r)
    return out


def _interval_row(r: Mapping[str, Any]) -> dict[str, Any]:
    keys = (
        "interval_id",
        "event_id",
        "analyst_id",
        "firm_id",
        "security_id",
        "native_rating",
        "canonical_rating",
        "action_type",
        "announced_ts",
        "effective_ts",
        "knowledge_ts",
        "available_ts",
        "entry_session",
        "entry_point",
        "entry_ts",
        "exit_session",
        "exit_point",
        "exit_ts",
        "exit_reason",
        "expiry_date",
        "decay_start_session",
        "is_investable",
        "non_investable_reason",
        "flags",
    )
    out = {k: _iso(r.get(k)) for k in keys}
    # Dates/timestamps/datetimes serialize via _iso only for date; datetime has isoformat too.
    for k, v in out.items():
        if hasattr(v, "isoformat"):
            out[k] = v.isoformat()
    return out


# --------------------------------------------------------------------------- #
# GET /api/analysts/{id}/ratings/{interval_id}
# --------------------------------------------------------------------------- #


def rating_detail(
    ctx: Context, run_id: str, analyst_id: str, interval_id: str
) -> dict[str, Any] | None:
    """Single-rating detail incl. legs, windows, target outcome and explanation."""
    ri = RatingInterval.__table__
    interval = _one(
        ctx,
        select(ri).where(ri.c.run_id == run_id, ri.c.interval_id == interval_id),
    )
    if interval is None or str(interval["analyst_id"]) != analyst_id:
        return None

    event = _one(
        ctx, select(RatingEvent.__table__).where(RatingEvent.event_id == interval["event_id"])
    )
    exposures = _legs_by_interval(ctx, run_id, [interval_id]).get(interval_id, [])
    returns = _all(
        ctx,
        select(IntervalReturn.__table__)
        .where(
            IntervalReturn.run_id == run_id,
            IntervalReturn.interval_id == interval_id,
        )
        .order_by(IntervalReturn.horizon),
    )
    contributions = _all(
        ctx,
        select(IntervalContribution.__table__)
        .where(
            IntervalContribution.run_id == run_id,
            IntervalContribution.interval_id == interval_id,
        )
        .order_by(IntervalContribution.horizon),
    )
    window = _one(
        ctx,
        select(EventWindow.__table__).where(
            EventWindow.run_id == run_id, EventWindow.event_id == interval["event_id"]
        ),
    )
    target = _one(
        ctx,
        select(TargetOutcome.__table__).where(
            TargetOutcome.run_id == run_id, TargetOutcome.event_id == interval["event_id"]
        ),
    )

    detail = _interval_row(interval)
    detail["event"] = _jsonable_row(event) if event else None
    detail["exposures"] = exposures
    detail["returns"] = [_jsonable_row(r) for r in returns]
    detail["contributions"] = [_jsonable_row(r) for r in contributions]
    detail["event_window"] = _jsonable_row(window) if window else None
    detail["target_outcome"] = _jsonable_row(target) if target else None
    detail["explanation"] = _explain(interval, exposures, returns, contributions)
    return detail


def _jsonable_row(row: Mapping[str, Any] | None) -> dict[str, Any] | None:
    if row is None:
        return None
    out: dict[str, Any] = {}
    for k, v in row.items():
        if hasattr(v, "isoformat") or isinstance(v, date):
            out[k] = v.isoformat()
        else:
            out[k] = v
    return out


def _pct(x: float | None) -> str:
    return "n/a" if x is None else f"{x * 100:+.2f}%"


def _explain(
    interval: Mapping[str, Any],
    exposures: Sequence[Mapping[str, Any]],
    returns: Sequence[Mapping[str, Any]],
    contributions: Sequence[Mapping[str, Any]],
) -> str:
    """Plain-language arithmetic explanation built from stored rows only."""
    parts: list[str] = []
    action = str(interval.get("action_type") or "?")
    rating = str(interval.get("canonical_rating") or interval.get("native_rating") or "?")
    parts.append(
        f"{action.replace('_', ' ')} to {rating} on {interval.get('security_id')} "
        f"(announced {interval.get('announced_ts')}, effective {interval.get('effective_ts')})."
    )
    entry = interval.get("entry_session")
    exit_ = interval.get("exit_session")
    span = ""
    if isinstance(entry, date) and isinstance(exit_, date):
        span = f" over {(exit_ - entry).days} calendar days"
    parts.append(
        f"Position held {entry} {interval.get('entry_point')} -> "
        f"{exit_} {interval.get('exit_point')}{span}; "
        f"exit reason {interval.get('exit_reason')}."
    )
    if not interval.get("is_investable"):
        reason = interval.get("non_investable_reason") or "not investable"
        parts.append(f"Interval is not investable ({reason}); excluded from metrics.")
        return " ".join(parts)

    # Weighted legs: weight x excess = contribution (per horizon).
    for contrib in contributions:
        horizon = str(contrib.get("horizon"))
        weight = contrib.get("position_weight")
        excess = contrib.get("excess_return")
        value = contrib.get("contribution")
        leg = next(
            (r for r in returns if str(r.get("horizon")) == horizon),
            None,
        )
        if contrib.get("unavailable_reason"):
            parts.append(f"[{horizon}] contribution unavailable ({contrib['unavailable_reason']}).")
            continue
        w_txt = "n/a" if weight is None else f"{weight:.4f}"
        if leg is not None and leg.get("excess_return") is not None and value is not None:
            stock = leg.get("stock_return")
            bench = leg.get("benchmark_return")
            parts.append(
                f"[{horizon}] weight {w_txt} x excess {_pct(excess)} "
                f"(stock {_pct(stock)} vs benchmark {_pct(bench)}) "
                f"= {_pct(value)} contribution."
            )
        elif value is not None:
            parts.append(f"[{horizon}] contribution {_pct(value)} (weight {w_txt}).")
    if exposures:
        legs = "; ".join(
            f"seg {e.get('segment_no')}: weight {e.get('position_weight')} "
            f"({e.get('weight_source')}) {e.get('start_session')}->"
            f"{e.get('end_session')}"
            for e in exposures
        )
        parts.append(f"Exposure legs: {legs}.")
    if not contributions:
        parts.append("No contribution rows stored for this interval.")
    return " ".join(parts)


# --------------------------------------------------------------------------- #
# GET /api/analysts/{id}/portfolio
# --------------------------------------------------------------------------- #


def portfolio(
    ctx: Context,
    run_id: str,
    analyst_id: str,
    *,
    date_from: date | None = None,
    date_to: date | None = None,
) -> dict[str, Any]:
    """NAV series (downsampled >2000 pts, extrema kept) + latest positions."""
    nav_tbl = PortfolioNav.__table__
    stmt = (
        select(nav_tbl)
        .where(nav_tbl.c.run_id == run_id, nav_tbl.c.analyst_id == analyst_id)
        .order_by(nav_tbl.c.session_date)
    )
    if date_from is not None:
        stmt = stmt.where(nav_tbl.c.session_date >= date_from)
    if date_to is not None:
        stmt = stmt.where(nav_tbl.c.session_date <= date_to)
    rows = _all(ctx, stmt)

    total = len(rows)
    downsampled = total > MAX_PORTFOLIO_POINTS
    points = _downsample(rows) if downsampled else rows

    # Positions snapshot: latest session_date present in the (filtered) series.
    positions: list[dict[str, Any]] = []
    if rows:
        latest = rows[-1]["session_date"]
        pos_tbl = PortfolioPosition.__table__
        pos_stmt = (
            select(pos_tbl)
            .where(
                pos_tbl.c.run_id == run_id,
                pos_tbl.c.analyst_id == analyst_id,
                pos_tbl.c.session_date == latest,
            )
            .order_by(pos_tbl.c.security_id)
            .limit(MAX_POSITIONS)
        )
        positions = [_jsonable_row(r) or {} for r in _all(ctx, pos_stmt)]

    return {
        "analyst_id": analyst_id,
        "points": [
            {
                "session_date": _iso(p["session_date"]),
                "nav": p["nav"],
                "benchmark_nav": p["benchmark_nav"],
                "active_return": p["active_return"],
                "gross_exposure": p["gross_exposure"],
                "turnover": p["turnover"],
                "cost": p["cost"],
                "drawdown": p["drawdown"],
            }
            for p in points
        ],
        "total_points": total,
        "downsampled": downsampled,
        "positions": {
            "session_date": _iso(rows[-1]["session_date"]) if rows else None,
            "rows": positions,
        },
    }


def _downsample(rows: Sequence[Mapping[str, Any]], limit: int = MAX_PORTFOLIO_POINTS) -> list[Any]:
    """Uniform-stride downsample that always keeps first/last + extrema.

    Keeps the min-nav, max-nav and min-drawdown (worst) points so peaks,
    troughs and drawdown extremes survive; remaining slots are filled by
    even stride over the series. Deterministic.
    """
    n = len(rows)
    if n <= limit:
        return list(rows)
    keep: set[int] = {0, n - 1}
    # Extrema indices.
    navs = [float(r["nav"]) for r in rows]
    dds = [float(r["drawdown"]) for r in rows]
    keep.add(navs.index(min(navs)))
    keep.add(navs.index(max(navs)))
    keep.add(dds.index(min(dds)))
    keep.add(dds.index(max(dds)))
    # Fill the rest by even stride.
    stride = n / limit
    for i in range(limit):
        keep.add(min(n - 1, int(i * stride)))
    return [rows[i] for i in sorted(keep)][:limit]


# --------------------------------------------------------------------------- #
# GET /api/leaderboard
# --------------------------------------------------------------------------- #


def leaderboard_sorts() -> frozenset[str]:
    """Sort whitelist: base columns + every catalog metric code."""
    return LEADERBOARD_BASE_SORTS | frozenset(_CATALOG)


def leaderboard(
    ctx: Context,
    run_id: str,
    *,
    peer_group: str | None = None,
    metric: str | None = None,
    min_obs: int | None = None,
    sort: str | None = None,
    order: str = "desc",
    limit: int | None = None,
    offset: int | None = None,
) -> dict[str, Any]:
    """Ranked leaderboard for one run.

    ``metric`` restricts rows to analysts with a stored (non-null value or
    explicit unavailable reason is NOT enough — requires ``value`` non-null)
    result for that catalog metric; ``min_obs`` filters on the investable
    interval count ``n_investable``.
    """
    if order not in ("asc", "desc"):
        raise QueryError("order must be 'asc' or 'desc'")
    lim = clamp_limit(limit)
    off = clamp_offset(offset)
    if metric is not None and metric not in _CATALOG:
        raise QueryError(f"unknown metric {metric!r}")
    if min_obs is not None and min_obs < 0:
        raise QueryError("min_obs must be >= 0")

    ac = AnalystComposite.__table__
    an = Analyst.__table__
    am = AnalystMetric.__table__
    n_obs_sq = (
        select(RatingInterval.analyst_id, func.count().label("n_obs"))
        .where(RatingInterval.run_id == run_id, RatingInterval.is_investable.is_(True))
        .group_by(RatingInterval.analyst_id)
        .subquery()
    )

    stmt = (
        select(
            ac.c.analyst_id,
            an.c.name,
            an.c.firm_id,
            ac.c.peer_group,
            ac.c.composite_score,
            ac.c.rank_overall,
            ac.c.rank_in_peer_group,
            ac.c.is_ranked,
            ac.c.unranked_reason,
            func.coalesce(n_obs_sq.c.n_obs, 0).label("n_investable"),
        )
        .select_from(
            ac.join(an, ac.c.analyst_id == an.c.analyst_id, isouter=True).join(
                n_obs_sq, ac.c.analyst_id == n_obs_sq.c.analyst_id, isouter=True
            )
        )
        .where(ac.c.run_id == run_id)
    )
    if peer_group is not None:
        stmt = stmt.where(ac.c.peer_group == peer_group)
    if min_obs is not None:
        stmt = stmt.where(func.coalesce(n_obs_sq.c.n_obs, 0) >= min_obs)

    sort_key = sort or "score"
    am = AnalystMetric.__table__
    if sort_key in _CATALOG:
        stmt = stmt.outerjoin(
            am,
            (am.c.run_id == ac.c.run_id)
            & (am.c.analyst_id == ac.c.analyst_id)
            & (am.c.metric_code == sort_key),
        )
    elif sort_key not in LEADERBOARD_BASE_SORTS:
        raise QueryError(
            f"unsupported sort {sort_key!r}; allowed: score, rank columns, analyst_id, "
            "name, n_obs, or a metric code"
        )

    # ``metric`` filter via EXISTS keeps the base query join-free.
    if metric is not None:
        exists_sq = (
            select(AnalystMetric.analyst_id)
            .where(
                AnalystMetric.run_id == run_id,
                AnalystMetric.analyst_id == ac.c.analyst_id,
                AnalystMetric.metric_code == metric,
                AnalystMetric.value.isnot(None),
            )
            .exists()
        )
        stmt = stmt.where(exists_sq)

    sort_cols: dict[str, Any] = {
        "score": ac.c.composite_score,
        "rank_overall": ac.c.rank_overall,
        "rank_in_peer_group": ac.c.rank_in_peer_group,
        "analyst_id": ac.c.analyst_id,
        "name": an.c.name,
        "n_obs": n_obs_sq.c.n_obs,
    }
    if sort_key in _CATALOG:
        sort_cols[sort_key] = am.c.value
    order_exprs = _order_by(
        sort_key,
        sort_cols,
        default="score",
        order=order,
        tiebreak=ac.c.analyst_id,
    )
    # For score/rank sorts the natural default is ascending rank; `order`
    # still applies to the chosen column.
    total = _count(ctx, stmt)
    rows = _all(ctx, stmt.order_by(*order_exprs).limit(lim).offset(off))

    key_rows = _metric_rows(ctx, run_id, codes=KEY_METRICS)
    key_by_analyst: dict[str, list[dict[str, Any]]] = {}
    for r in key_rows:
        key_by_analyst.setdefault(str(r["analyst_id"]), []).append(r)

    # Family breakdown needs the full run's metric rows + composites.
    all_metrics = _metric_rows(ctx, run_id)
    composites = _composite_rows(ctx, run_id)
    families = family_breakdown(all_metrics, composites)

    items = []
    for r in rows:
        aid = str(r["analyst_id"])
        items.append(
            {
                "rank": r.get("rank_overall"),
                "analyst_id": aid,
                "name": r.get("name"),
                "firm_id": r.get("firm_id"),
                "peer_group": r.get("peer_group"),
                "composite_score": r.get("composite_score"),
                "rank_in_peer_group": r.get("rank_in_peer_group"),
                "is_ranked": bool(r.get("is_ranked")),
                "unranked_reason": r.get("unranked_reason"),
                "n_investable": int(r.get("n_investable") or 0),
                "family_scores": families.get(aid, {}),
                "key_metrics": _inline_metrics(key_by_analyst.get(aid, [])),
            }
        )
    return {"items": items, "total": total, "limit": lim, "offset": off}
