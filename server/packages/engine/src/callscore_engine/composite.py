"""Composite rankings: peer-standardized scored metrics -> 0-100 score + ranks.

Consumes ``metric_results`` (``frames.METRIC_RESULT_FRAME``), the peer-group
assignment and the investable-interval counts, and produces ``composites``
(``frames.COMPOSITE_FRAME``). Pure function of data + methodology.

Conventions implemented here (decision 0003, ``metrics.CompositeSpec``):

* Only catalog metrics with ``in_composite`` count, grouped by ``family``.
  The analyst's input ``x`` is ``shrunk_value`` when present, else ``value``.
* ``is_ranked`` requires, in order: ``n_obs >= min_ratings_for_rank``
  (``insufficient_ratings``), a non-null peer group (``no_peer_group``), a
  peer group with at least ``min_peer_size`` base-eligible members — analysts
  passing the first two checks (``peer_group_too_small``) — and at least
  ``spec.min_scored_families`` families with an available scored metric
  (``insufficient_families``).
* Each scored metric is z-scored within its peer group as
  ``(x - mean) / std(ddof=1)`` over the *ranked* analysts' ``x`` values
  (groups with fewer than two values or zero variance produce z = 0),
  clipped to ``+-spec.z_clip`` and multiplied by the metric's orientation
  (``higher`` +1, ``lower`` -1). A family's score is the mean of its
  available oriented z values.
* ``c`` is the weighted mean of family scores with
  ``methodology.family_weights``, renormalized over the families present.
  Analysts with no scored family get a null ``composite_score``; the
  degenerate case of only zero-weight families present also yields null.
* ``composite_score = 100 * Phi(c / s)`` where ``s`` is the sample std
  (ddof=1) of ``c`` over ranked analysts; ``s = 0`` or fewer than two ranked
  analysts maps every computable ``c`` to 50.0. Unranked analysts still get
  a ``composite_score`` when they have at least one scored family, but all
  rank columns stay null for them.
* ``rank_overall`` orders ranked analysts by composite score descending then
  ``analyst_id`` ascending (1-based ordinal); ``rank_in_peer_group`` applies
  the same rule inside each peer group.
"""

import math
from collections.abc import Sequence

import polars as pl

from callscore_core.enums import MetricFamily
from callscore_core.frames import COMPOSITE_FRAME
from callscore_core.methodology import Methodology
from callscore_core.metrics import (
    COMPOSITE_SPEC,
    METRIC_DEFINITIONS,
    CompositeSpec,
    MetricDefinition,
)

_DTYPES: dict[str, pl.DataType] = {
    "String": pl.String(),
    "Date": pl.Date(),
    "Datetime[us, UTC]": pl.Datetime("us", "UTC"),
    "Float64": pl.Float64(),
    "Int64": pl.Int64(),
    "Boolean": pl.Boolean(),
}

_COMPOSITE_SCHEMA: dict[str, pl.DataType] = {
    column.name: _DTYPES[column.dtype] for column in COMPOSITE_FRAME.columns
}


def _family_weight(family: MetricFamily, methodology: Methodology) -> float:
    return float(getattr(methodology.family_weights, family.value))


def _normal_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def compute_composites(
    results: pl.DataFrame,
    peer_groups: pl.DataFrame,
    n_investable: pl.DataFrame,
    methodology: Methodology,
    spec: CompositeSpec = COMPOSITE_SPEC,
    catalog: Sequence[MetricDefinition] = METRIC_DEFINITIONS,
) -> pl.DataFrame:
    """``composites`` frame: one row per analyst, sorted by ``analyst_id``.

    ``results`` is a ``metric_results`` frame; ``peer_groups`` carries
    ``analyst_id, peer_group``; ``n_investable`` carries ``analyst_id, n_obs``
    (investable interval count).
    """
    scored = [d for d in catalog if d.in_composite]

    analysts = (
        set(results["analyst_id"].to_list())
        | set(peer_groups["analyst_id"].to_list())
        | set(n_investable["analyst_id"].to_list())
    )
    peer_map = {
        str(a): (None if g is None else str(g))
        for a, g in peer_groups.select("analyst_id", "peer_group").iter_rows()
    }
    investable_map = {
        str(a): int(n) for a, n in n_investable.select("analyst_id", "n_obs").iter_rows()
    }
    # (analyst, metric) -> (value, x) where x = shrunk_value if present else value.
    metric_values: dict[tuple[str, str], tuple[float | None, float | None]] = {}
    for aid, code, value, shrunk in results.select(
        "analyst_id", "metric_code", "value", "shrunk_value"
    ).iter_rows():
        metric_values[(str(aid), str(code))] = (
            None if value is None else float(value),
            None if shrunk is None else float(shrunk),
        )

    def n_obs_of(analyst: str) -> int:
        return investable_map.get(analyst, 0)

    def peer_of(analyst: str) -> str | None:
        return peer_map.get(analyst)

    # Pass 1: base eligibility and family availability (no z-scores needed).
    base_ok: dict[str, bool] = {}
    families_present: dict[str, set[MetricFamily]] = {a: set() for a in analysts}
    for analyst in analysts:
        group = peer_of(analyst)
        base_ok[analyst] = (
            n_obs_of(analyst) >= methodology.min_ratings_for_rank and group is not None
        )
        if not base_ok[analyst]:
            continue
        for definition in scored:
            entry = metric_values.get((analyst, definition.code))
            if entry is not None and entry[0] is not None:
                families_present[analyst].add(definition.family)

    group_sizes: dict[str, int] = {}
    for analyst in analysts:
        if base_ok[analyst]:
            group = peer_of(analyst)
            assert group is not None
            group_sizes[group] = group_sizes.get(group, 0) + 1

    unranked_reason: dict[str, str | None] = {}
    is_ranked: dict[str, bool] = {}
    for analyst in analysts:
        group = peer_of(analyst)
        if n_obs_of(analyst) < methodology.min_ratings_for_rank:
            reason = "insufficient_ratings"
        elif group is None:
            reason = "no_peer_group"
        elif group_sizes.get(group, 0) < methodology.min_peer_size:
            reason = "peer_group_too_small"
        elif len(families_present[analyst]) < spec.min_scored_families:
            reason = "insufficient_families"
        else:
            reason = None
        unranked_reason[analyst] = reason
        is_ranked[analyst] = reason is None

    # Pass 2: z-score each scored metric within its peer group over the ranked
    # analysts' x values; stats are (mean, std) or None meaning z = 0 for all.
    z_stats: dict[tuple[str, str], tuple[float, float] | None] = {}
    for definition in scored:
        by_group: dict[str, list[tuple[str, float]]] = {}
        for analyst in analysts:
            if not is_ranked[analyst]:
                continue
            entry = metric_values.get((analyst, definition.code))
            if entry is None:
                continue
            value, shrunk = entry
            if value is None:
                continue
            x = shrunk if shrunk is not None else value
            group = peer_of(analyst)
            assert group is not None
            by_group.setdefault(group, []).append((analyst, x))
        for group, members in by_group.items():
            xs = [x for _, x in members]
            if len(xs) < 2:
                z_stats[(definition.code, group)] = None
                continue
            mean = sum(xs) / len(xs)
            var = sum((x - mean) ** 2 for x in xs) / (len(xs) - 1)
            std = math.sqrt(var)
            z_stats[(definition.code, group)] = None if std == 0.0 else (mean, std)

    def oriented_z(analyst: str, definition: MetricDefinition) -> float | None:
        entry = metric_values.get((analyst, definition.code))
        if entry is None:
            return None
        value, shrunk = entry
        if value is None:
            return None
        group = peer_of(analyst)
        if group is None:
            return 0.0
        stats = z_stats.get((definition.code, group))
        x = shrunk if shrunk is not None else value
        if stats is None:
            z = 0.0
        else:
            mean, std = stats
            z = (x - mean) / std
        z = max(-spec.z_clip, min(spec.z_clip, z))
        return z * definition.orientation

    # Pass 3: family scores and composite c per analyst.
    composite_c: dict[str, float | None] = {}
    for analyst in analysts:
        family_scores: dict[MetricFamily, list[float]] = {}
        for definition in scored:
            z = oriented_z(analyst, definition)
            if z is None:
                continue
            family_scores.setdefault(definition.family, []).append(z)
        if not family_scores:
            composite_c[analyst] = None
            continue
        numerator = 0.0
        denominator = 0.0
        for family, zs in family_scores.items():
            weight = _family_weight(family, methodology)
            numerator += weight * (sum(zs) / len(zs))
            denominator += weight
        composite_c[analyst] = numerator / denominator if denominator > 0.0 else None

    ranked_cs = [c for a in analysts if is_ranked[a] and (c := composite_c[a]) is not None]
    cohort_mean = sum(ranked_cs) / len(ranked_cs) if ranked_cs else 0.0
    cohort_std = (
        math.sqrt(sum((c - cohort_mean) ** 2 for c in ranked_cs) / (len(ranked_cs) - 1))
        if len(ranked_cs) >= 2
        else 0.0
    )

    def composite_score(analyst: str) -> float | None:
        c = composite_c[analyst]
        if c is None:
            return None
        if len(ranked_cs) < 2 or cohort_std == 0.0:
            return 50.0
        return 100.0 * _normal_cdf(c / cohort_std)

    scores = {a: composite_score(a) for a in analysts}

    # Pass 4: ordinal ranks over ranked analysts (score desc, analyst_id asc).
    def rank_key(analyst: str) -> tuple[float, str]:
        score = scores[analyst]
        return (-(score if score is not None else float("-inf")), analyst)

    rank_overall: dict[str, int | None] = {a: None for a in analysts}
    rank_in_group: dict[str, int | None] = {a: None for a in analysts}
    ranked_sorted = sorted((a for a in analysts if is_ranked[a]), key=rank_key)
    for position, analyst in enumerate(ranked_sorted, start=1):
        rank_overall[analyst] = position
    ranked_by_group: dict[str, list[str]] = {}
    for analyst in ranked_sorted:
        group = peer_of(analyst)
        assert group is not None
        ranked_by_group.setdefault(group, []).append(analyst)
    for group_members in ranked_by_group.values():
        for position, analyst in enumerate(group_members, start=1):
            rank_in_group[analyst] = position

    output_rows = [
        {
            "analyst_id": analyst,
            "peer_group": peer_of(analyst),
            "composite_score": scores[analyst],
            "rank_overall": rank_overall[analyst],
            "rank_in_peer_group": rank_in_group[analyst],
            "is_ranked": is_ranked[analyst],
            "unranked_reason": unranked_reason[analyst],
        }
        for analyst in sorted(analysts)
    ]
    return pl.DataFrame(output_rows, schema=_COMPOSITE_SCHEMA).sort("analyst_id")
