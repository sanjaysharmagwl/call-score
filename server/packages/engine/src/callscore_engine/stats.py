"""Statistics stage: bootstrap inference, empirical-Bayes shrinkage, peer percentiles.

Consumes the metrics stage outputs (``values``: one row per analyst and catalog
metric; ``samples``: ``metric_samples`` contract rows) and produces
``metric_results`` (``frames.METRIC_RESULT_FRAME``). Pure function of data +
methodology: no I/O, no clock, deterministic RNG only.

Conventions implemented here:

* ``quarter_block_bootstrap`` metrics resample the calendar quarters of
  ``observation_date`` with replacement (same number of blocks per draw) and
  apply the definition's ``aggregate`` to the pooled observations:
  ``mean`` -> mean, ``annualized_mean`` -> 252 * mean,
  ``annualized_sharpe`` -> sqrt(252) * mean / sample_std(ddof=1). Block-level
  sums/counts/sums of squares make the pooled statistics exact and vectorized.
* ``std_err`` is the sample std (ddof=1) of the finite draws; the CI is the
  two-sided percentile interval at ``methodology.confidence_level``. Fewer
  than two finite draws, or a single quarter block, yields null statistics.
* ``t_stat`` = ``(value - null_value) / std_err`` and ``is_significant`` is
  whether the CI excludes ``null_value``; both need a non-null ``null_value``.
* Shrinkage (``empirical_bayes`` and ``definition.shrink``) pools analysts
  within a peer group that have a value and ``std_err > 0``: with
  ``sigma2 = std_err**2``, ``tau2 = max(0, var(values, ddof=1) - mean(sigma2))``
  and ``lambda = tau2 / (tau2 + sigma2)``, ``shrunk = lambda*value +
  (1-lambda)*group_mean``. ``tau2 = 0`` collapses every member to the group
  mean. Groups smaller than ``methodology.min_peer_size`` (counted over the
  shrinkage candidates) and analysts without a peer group get null.
* ``peer_percentile`` is ``100 * (rank - 0.5) / n`` with average ranks for
  ties, computed on the ascending value within the peer group over analysts
  with an available value (group size >= ``min_peer_size``). Higher-is-better
  and descriptive metrics use the ascending percentile; lower-is-better
  metrics are inverted so the best value scores highest.
* RNG streams are ``numpy.random.default_rng`` seeded per
  (analyst_id, metric_code) from ``methodology.bootstrap_seed`` via SHA-256 —
  never Python's ``hash()``.
"""

import hashlib
from collections.abc import Sequence
from datetime import date

import numpy as np
import numpy.typing as npt
import polars as pl

from callscore_core.contracts import MetricValue
from callscore_core.methodology import Methodology
from callscore_core.metrics import METRIC_DEFINITIONS, MetricDefinition

FloatArray = npt.NDArray[np.float64]

_RESULT_SCHEMA: dict[str, pl.DataType] = {
    "analyst_id": pl.String(),
    "metric_code": pl.String(),
    "value": pl.Float64(),
    "n_obs": pl.Int64(),
    "std_err": pl.Float64(),
    "t_stat": pl.Float64(),
    "ci_low": pl.Float64(),
    "ci_high": pl.Float64(),
    "shrunk_value": pl.Float64(),
    "peer_percentile": pl.Float64(),
    "is_significant": pl.Boolean(),
    "unavailable_reason": pl.String(),
}


def _seed(bootstrap_seed: int, analyst_id: str, metric_code: str) -> int:
    digest = hashlib.sha256(f"{bootstrap_seed}|{analyst_id}|{metric_code}".encode()).digest()
    return int.from_bytes(digest[:8], "little")


def _quarter_index(day: date) -> int:
    return day.year * 4 + (day.month - 1) // 3


def _bootstrap_draws(
    dates: list[date],
    obs: list[float],
    aggregate: str,
    n_draws: int,
    seed: int,
) -> FloatArray:
    """``n_draws`` quarter-block bootstrap replicates of the aggregate statistic."""
    quarters = np.array([_quarter_index(d) for d in dates], dtype=np.int64)
    values = np.asarray(obs, dtype=np.float64)
    _, inverse = np.unique(quarters, return_inverse=True)
    n_blocks = int(inverse.max()) + 1 if inverse.size else 0
    if n_blocks < 2:
        return np.empty(0, dtype=np.float64)
    block_n = np.bincount(inverse).astype(np.float64)
    block_sum = np.bincount(inverse, weights=values)
    block_sumsq = np.bincount(inverse, weights=values * values)
    rng = np.random.default_rng(seed)
    picks = rng.integers(0, n_blocks, size=(n_draws, n_blocks))
    total_n = block_n[picks].sum(axis=1)
    total_sum = block_sum[picks].sum(axis=1)
    mean: FloatArray = total_sum / total_n
    if aggregate == "annualized_mean":
        return 252.0 * mean
    if aggregate == "annualized_sharpe":
        total_sumsq = block_sumsq[picks].sum(axis=1)
        variance = (total_sumsq - total_sum * total_sum / total_n) / (total_n - 1.0)
        with np.errstate(divide="ignore", invalid="ignore"):
            sharpe: FloatArray = np.sqrt(252.0) * mean / np.sqrt(np.maximum(variance, 0.0))
            return sharpe
    return mean


def _inference(
    definition: MetricDefinition,
    value: float | None,
    dates: list[date],
    obs: list[float],
    methodology: Methodology,
    analyst_id: str,
) -> tuple[float | None, float | None, float | None, float | None, bool | None]:
    """``(std_err, t_stat, ci_low, ci_high, is_significant)`` for one result row."""
    if definition.inference != "quarter_block_bootstrap" or value is None:
        return None, None, None, None, None
    draws = _bootstrap_draws(
        dates,
        obs,
        definition.aggregate,
        methodology.bootstrap_draws,
        _seed(methodology.bootstrap_seed, analyst_id, definition.code),
    )
    finite = draws[np.isfinite(draws)]
    if finite.size < 2:
        return None, None, None, None, None
    std_err = float(np.std(finite, ddof=1))
    alpha = 1.0 - methodology.confidence_level
    ci_low, ci_high = (
        float(q) for q in np.percentile(finite, [100.0 * alpha / 2.0, 100.0 * (1 - alpha / 2)])
    )
    null_value = definition.null_value
    t_stat = (value - null_value) / std_err if null_value is not None and std_err > 0 else None
    is_significant = (
        None if null_value is None else bool(null_value < ci_low or null_value > ci_high)
    )
    return std_err, t_stat, ci_low, ci_high, is_significant


def _average_ranks(values: FloatArray) -> FloatArray:
    """1-based average ranks (ascending) with ties sharing the mean rank."""
    n = values.size
    order = np.argsort(values, kind="stable")
    sorted_values = values[order]
    sorted_ranks = np.empty(n, dtype=np.float64)
    i = 0
    while i < n:
        j = i + 1
        while j < n and sorted_values[j] == sorted_values[i]:
            j += 1
        sorted_ranks[i:j] = (i + 1 + j) / 2.0
        i = j
    ranks = np.empty(n, dtype=np.float64)
    ranks[order] = sorted_ranks
    return ranks


def _percentile_map(
    rows: list[dict[str, object]],
    definition: MetricDefinition,
    min_peer_size: int,
) -> dict[int, float]:
    """peer_percentile per row index within each peer group of available values."""
    result: dict[int, float] = {}
    groups: dict[str, list[int]] = {}
    for idx, row in enumerate(rows):
        value = row["value"]
        group = row["_peer_group"]
        if value is None or group is None:
            continue
        groups.setdefault(str(group), []).append(idx)
    for indices in groups.values():
        if len(indices) < min_peer_size:
            continue
        raw = np.array([float(rows[i]["value"]) for i in indices], dtype=np.float64)  # type: ignore[arg-type]
        ranks = _average_ranks(raw)
        oriented = ranks if definition.direction != "lower" else len(indices) - ranks + 1.0
        for idx, rank in zip(indices, oriented, strict=True):
            result[idx] = float(100.0 * (rank - 0.5) / len(indices))
    return result


def infer_metrics(
    values: pl.DataFrame,
    samples: pl.DataFrame,
    peer_groups: pl.DataFrame,
    methodology: Methodology,
    catalog: Sequence[MetricDefinition] = METRIC_DEFINITIONS,
) -> pl.DataFrame:
    """``metric_results``: inference, shrinkage and percentiles for every ``values`` row.

    ``values`` carries ``analyst_id, metric_code, value, n_obs,
    unavailable_reason``; ``samples`` is a ``metric_samples`` frame;
    ``peer_groups`` carries ``analyst_id, peer_group`` (null group allowed).
    Output follows ``METRIC_RESULT_FRAME`` column order, dtypes and sort.
    """
    definitions = {d.code: d for d in catalog}
    peer_map = {
        str(a): (None if g is None else str(g))
        for a, g in peer_groups.select("analyst_id", "peer_group").iter_rows()
    }
    sample_map: dict[tuple[str, str], tuple[list[date], list[float]]] = {}
    for aid, code, obs_date, obs_value in samples.select(
        "analyst_id", "metric_code", "observation_date", "value"
    ).iter_rows():
        entry = sample_map.setdefault((str(aid), str(code)), ([], []))
        entry[0].append(obs_date)
        entry[1].append(float(obs_value))

    rows: list[dict[str, object]] = []
    for aid, code, value, n_obs, reason in values.select(
        "analyst_id", "metric_code", "value", "n_obs", "unavailable_reason"
    ).iter_rows():
        analyst_id, metric_code = str(aid), str(code)
        if metric_code not in definitions:
            raise ValueError(f"values contains metric_code not in catalog: {metric_code!r}")
        definition = definitions[metric_code]
        value_f = None if value is None else float(value)
        n_obs_i = int(n_obs)
        reason_s = None if reason is None else str(reason)
        if value_f is None:
            rows.append(
                {
                    "analyst_id": analyst_id,
                    "metric_code": metric_code,
                    "value": None,
                    "n_obs": n_obs_i,
                    "std_err": None,
                    "t_stat": None,
                    "ci_low": None,
                    "ci_high": None,
                    "shrunk_value": None,
                    "peer_percentile": None,
                    "is_significant": None,
                    "unavailable_reason": reason_s,
                    "_definition": definition,
                    "_peer_group": peer_map.get(analyst_id),
                }
            )
            continue
        dates, obs = sample_map.get((analyst_id, metric_code), ([], []))
        std_err, t_stat, ci_low, ci_high, is_significant = _inference(
            definition, value_f, dates, obs, methodology, analyst_id
        )
        rows.append(
            {
                "analyst_id": analyst_id,
                "metric_code": metric_code,
                "value": value_f,
                "n_obs": n_obs_i,
                "std_err": std_err,
                "t_stat": t_stat,
                "ci_low": ci_low,
                "ci_high": ci_high,
                "shrunk_value": None,
                "peer_percentile": None,
                "is_significant": is_significant,
                "unavailable_reason": None,
                "_definition": definition,
                "_peer_group": peer_map.get(analyst_id),
            }
        )

    _apply_shrinkage(rows, methodology)
    _apply_percentiles(rows, methodology)

    output_rows: list[dict[str, object]] = []
    for row in rows:
        metric = MetricValue(
            value=row["value"],
            n_obs=row["n_obs"],
            std_err=row["std_err"],
            t_stat=row["t_stat"],
            ci_low=row["ci_low"],
            ci_high=row["ci_high"],
            shrunk_value=row["shrunk_value"],
            peer_percentile=row["peer_percentile"],
            is_significant=row["is_significant"],
            unavailable_reason=row["unavailable_reason"],
        )
        output_rows.append(
            {
                "analyst_id": row["analyst_id"],
                "metric_code": row["metric_code"],
                "value": metric.value,
                "n_obs": metric.n_obs,
                "std_err": metric.std_err,
                "t_stat": metric.t_stat,
                "ci_low": metric.ci_low,
                "ci_high": metric.ci_high,
                "shrunk_value": metric.shrunk_value,
                "peer_percentile": metric.peer_percentile,
                "is_significant": metric.is_significant,
                "unavailable_reason": metric.unavailable_reason,
            }
        )
    return pl.DataFrame(output_rows, schema=_RESULT_SCHEMA).sort("analyst_id", "metric_code")


def _apply_shrinkage(rows: list[dict[str, object]], methodology: Methodology) -> None:
    """Empirical-Bayes shrinkage toward the peer-group mean, per metric."""
    if methodology.shrinkage_method.value != "empirical_bayes":
        return
    by_metric: dict[str, list[int]] = {}
    for idx, row in enumerate(rows):
        definition = row["_definition"]
        assert isinstance(definition, MetricDefinition)
        if definition.shrink:
            by_metric.setdefault(str(row["metric_code"]), []).append(idx)
    for indices in by_metric.values():
        candidates: dict[str, list[int]] = {}
        for idx in indices:
            row = rows[idx]
            std_err = row["std_err"]
            if (
                row["value"] is not None
                and std_err is not None
                and float(std_err) > 0.0  # type: ignore[arg-type]
                and row["_peer_group"] is not None
            ):
                candidates.setdefault(str(row["_peer_group"]), []).append(idx)
        for group_indices in candidates.values():
            if len(group_indices) < methodology.min_peer_size:
                continue
            vals = np.array([float(rows[i]["value"]) for i in group_indices])  # type: ignore[arg-type]
            variances = np.array(
                [float(rows[i]["std_err"]) ** 2 for i in group_indices]  # type: ignore[arg-type]
            )
            group_mean = float(vals.mean())
            tau2 = max(0.0, float(vals.var(ddof=1)) - float(variances.mean()))
            for i, idx in enumerate(group_indices):
                if tau2 == 0.0:
                    shrunk = group_mean
                else:
                    lam = tau2 / (tau2 + float(variances[i]))
                    shrunk = lam * float(vals[i]) + (1.0 - lam) * group_mean
                lo, hi = min(float(vals[i]), group_mean), max(float(vals[i]), group_mean)
                rows[idx]["shrunk_value"] = min(max(shrunk, lo), hi)


def _apply_percentiles(rows: list[dict[str, object]], methodology: Methodology) -> None:
    """Peer percentiles per metric over analysts with an available value."""
    by_metric: dict[str, list[int]] = {}
    for idx, row in enumerate(rows):
        by_metric.setdefault(str(row["metric_code"]), []).append(idx)
    for indices in by_metric.values():
        definition = rows[indices[0]]["_definition"]
        assert isinstance(definition, MetricDefinition)
        subset = [rows[i] for i in indices]
        percentiles = _percentile_map(subset, definition, methodology.min_peer_size)
        for local_idx, pct in percentiles.items():
            rows[indices[local_idx]]["peer_percentile"] = pct
