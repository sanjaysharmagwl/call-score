# 0003 — Scoring semantics: timing, decay, horizons, metrics, composite

## Context

The build plan names the metric families and methodology toggles but leaves
execution timing, decay accounting, horizon selection and the composite
formula open. Engine, synth and API work run in parallel, so these must be
fixed once, as code, before any of them start. The executable source of
truth is `callscore_core` (`methodology`, `frames`, `metrics`, `contracts`).

## Decision

### Signal availability and entry

- A rating is **available** at `available_ts = max(effective_ts, knowledge_ts)`.
  `knowledge_ts` governs, not `announced_ts`, so a correction is never
  actionable before the system learned it.
- Entry is an actual exchange execution point, found on the trading
  calendar, never by weekday arithmetic:
    - `next_open` — the first session open strictly after `available_ts`
      (pre-market news enters the same day's open; intraday or post-close
      news enters the next open).
    - `same_close` — the first session close strictly after `available_ts`.
    - `t_plus_2_open` — the open one session after the `next_open` session.
- An interval exits at the earliest of: the next interval's entry point for
  the same analyst and security, expiry (`hard_close`: close of the last
  session on or before `expiry_date`), coverage or employment end (close of
  the last session on or before the end date), delisting, or the run's
  as-of close. Exposure is `[entry, exit)`, so consecutive intervals never
  overlap or leave gaps.
- Non-investable ratings (`not_rated`, `under_review`, `restricted`) create
  intervals with a null weight; they are never treated as holds.

### Returns at execution points

- Stock returns are measured between the entry and exit execution points,
  not close-to-close. The total-return value at a close is `tr_index`; at an
  open it is the previous close's `tr_index` times the split-adjusted
  overnight price ratio, with an ex-date dividend reinvested at that open.
  `price` basis uses split-adjusted prices only.
- Benchmarks carry both `open_level` and `close_level`, so the benchmark
  leg spans exactly the same points as the stock. Missing benchmark data
  fails the run; it is never replaced by zero.
- `beta_adjusted` uses `beta × market return` over the same points, with
  beta as of the session before entry.
- Excess return is arithmetic: `stock_return − benchmark_return`.
  Contribution is `weight × excess_return`.
- Delisting ends the interval: the final value is the last close times
  `1 + final_return` (−1.0 for bankruptcy), flagged `terminated_by_delisting`.
  It is never dropped and never censored.

### Weights and decay

- Absolute scoring uses `position_weights`.
  `relative_to_consensus` uses `clip(w_own − mean(w_others), −1, 1)` over
  other analysts' ratings visible at `available_ts`, and requires
  `min_consensus_ratings` of them. Otherwise the interval is non-investable
  with the reason `insufficient_consensus`. This deliberately departs from a
  z-score, which is unstable when analysts agree (σ ≈ 0).
- `decay`: full weight until expiry, then a linear taper to zero over
  `decay_days` trading sessions. The taper is stored as daily
  `exposures` segments with weight `w × (1 − k / decay_days)`, and the
  to-exit contribution is `Σ segment_weight × segment_excess_return`.
  `hard_close` and `carry_forward` produce a single segment.
- Interval boundaries and raw return legs are independent of weights, so a
  weight-only change reuses them and recomputes contributions onward.

### Horizons

- `to_exit` (the interval's own holding period) drives call-performance
  metrics: mean contribution, hit rate and consistency.
- Fixed horizons (`d63`, `d126`, `d252`) run from entry using the entry
  weight, regardless of later rating changes. They are unavailable if they
  extend past the as-of date, and end early on delisting.
  `primary_fixed_horizon_days` (default 126, matching the horizon on which
  synthetic skill is planted) selects the horizon used for the information
  coefficient. All fixed horizons are still computed and shown.

### Metrics and composite

- The catalog (`callscore_core.metrics.METRIC_DEFINITIONS`) defines every
  metric's formula, estimand, inputs (declared frames), horizon, direction,
  inference method, shrinkage and whether it counts toward the composite.
- Scored metrics, by family:

    | Family | Scored metrics |
    | --- | --- |
    | Return/skill | mean contribution, hit rate, information coefficient, information ratio |
    | Timing | 20-session revision drift |
    | Price target | target hit rate, target absolute error |
    | Behavior | stale coverage |
    | Consistency | positive-quarter fraction |

- Style traits — independence, optimism, activity, announcement reaction
  and momentum chasing — are shown but never scored, so the composite
  rewards skill rather than unusual behaviour. Annualized alpha is shown with
  its own bootstrap CI but is not scored, to avoid double-counting it with
  the information ratio.
- Composite: z-score each scored metric within the peer group (shrunk value
  where shrinkage applies), clip at ±3, flip metrics where lower is better,
  average within each family, then combine with `family_weights`. Weights
  are renormalized over the families that have data, and at least 3 families
  are required. The score is `100 × Φ(c / sd(c))` over the cohort. Ties
  break by score, then analyst ID.
- Default family weights: return/skill 0.40, consistency 0.20, timing 0.15,
  price target 0.15, behavior 0.10.

### Cache identity

- Every stage key includes the run context (as-of date, knowledge cutoff,
  source and universe fingerprints, algorithm version), its slice of the
  methodology and its upstream fingerprints. The metrics and statistics
  stages also include `METRIC_CATALOG_FINGERPRINT`, so editing any metric
  definition or composite rule invalidates them. A test fails if a
  methodology field is not assigned to a stage.

## Consequences

- Synthetic and real data must supply benchmark open levels, split
  factors, dividends, intraday high/low (for target hits) and point-in-time
  betas.
- Decaying intervals produce one exposure row per tail session. That costs
  roughly 10–15% of intervals × `decay_days` rows, which is acceptable.
- Changing any of these rules is a contract change: update the code, this
  record and the golden fixtures together.
