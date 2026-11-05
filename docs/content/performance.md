# Performance

Measured on the `full` synthetic profile (`data/profiles/full.toml`:
500 securities, 2,520 sessions, ~19.8k rating events, 187 analysts) on an
Apple M1 (8 GB RAM), macOS, Python 3.12, Polars 1.44, SQLite (WAL).

Reproduce with `just benchmark` (seed + run, `/usr/bin/time -l`) or the
commands below. Benchmarks used seed `20261006`, `as_of=2025-12-31`,
`--methodology default`.

## Dataset generation (`callscore-synth --scale full`)

| Metric | Value |
| --- | --- |
| Wall time | ~1,073 s (~18 min) |
| Peak RSS (`/usr/bin/time -l`) | ~1.82 GB |
| Output DB size | ~368 MB |
| Largest inputs | `security_daily`/`liquidity_daily` ~1.16M rows each, `consensus_daily` ~1.03M |

## Scoring run (`callscore run` / `execute_run`)

`callscore run --db bench-full.db --scale full --as-of 2025-12-31
--methodology default` (includes snapshot load, pipeline, and atomic
publish of 582,793 derived rows):

| Metric | Value |
| --- | --- |
| Wall time | ~333 s (~5.5 min) |
| Peak RSS | ~2.74 GB |
| Derived rows written | 582,793 across 9 tables |

Pipeline-only timings (snapshot already loaded, no DB writes), measured via
`run_pipeline` / `run_pipeline_cached`:

| Phase | Cold | Warm stage cache |
| --- | --- | --- |
| Snapshot load (`load_input_snapshot`) | ~20 s | — |
| Stage compute (all 5 stages) | ~23 s total | ~0.4 s (all cache hits) |
| Artifact content fingerprints | ~82 s | ~82 s (unchanged) |
| **Pipeline total** | **~95 s** | **~97 s** |

Per-stage compute (cold): INTERVALS ~9.3 s, RETURNS ~0.4 s, EXPOSURES
~0.8 s, METRICS ~8.6 s, STATISTICS ~4.4 s.

### Finding: warm cache does not make runs faster end-to-end

The per-stage Parquet cache (`data/db/cache/`, used by the API job
coordinator) eliminates recompute — every stage is a cache hit in ~0.4 s —
but `run_pipeline_cached` still spends ~82 s computing `StageArtifact`
`content_fingerprint`s: it canonicalizes every output row of every frame to
JSON and SHA-256s it. Two frames dominate: `metric_samples` (~7.1M rows,
~46 s) and `coverage_daily` (~6.2M rows, ~31 s); `portfolio_nav` (~438k
rows) adds ~3.6 s. A cheaper fingerprint (e.g. over Parquet bytes or a
streaming hash) would make warm runs meaningfully faster; noted as a
follow-up, not changed here.

## API latency spot-check

20 sequential requests each against a single uvicorn worker on the
full-profile DB (Apple M1):

| Endpoint | min | p50 | p95 | max |
| --- | --- | --- | --- | --- |
| `GET /api/leaderboard?limit=20` | 35.6 ms | 35.9 ms | 41.9 ms | 58.6 ms |
| `GET /api/analysts/{id}` | 29.9 ms | 30.1 ms | 30.5 ms | 30.5 ms |
| `GET /api/analysts/{id}/metrics` | 1.2 ms | 1.3 ms | 1.4 ms | 1.4 ms |

All read queries are index-backed —
`EXPLAIN QUERY PLAN` shows `SEARCH ... USING INDEX` on
`ix_analyst_composite_run_rank`, `ix_rating_interval_run_analyst`,
`ix_analyst_metric_run_metric`, and the `portfolio_nav`/`portfolio_position`
primary keys. No schema v3 was justified; existing indexes cover the hot
paths.

## Response-size caps (verified enforced)

- `limit` clamped to `MAX_LIMIT = 500` at routers (`le=`) and again in
  services (`clamp_limit`).
- Analyst portfolio NAV series downsampled above `MAX_PORTFOLIO_POINTS =
  2000` points (uniform stride, extrema kept).
- Positions snapshot capped at `MAX_POSITIONS = 500` rows.
- Exports/series endpoints paginate with the same cap.

## Vendored calc packages

`callscore-calc` / `callscore-calc-whatif` (ADR
[0002](decisions/0002-vendor-pylibs-calc.md)) are workspace members kept for
their own test suites. **No product code imports `callscore_calc`** — the
engine is self-contained (Polars/NumPy) and the API never loads the calc
plugin registry. `callscore-engine` previously declared an unused
`callscore-calc` dependency; it was removed in this pass (lockfile
regenerated). The packages remain installed via `uv sync
--all-packages` and still ship as wheels in `dist/`.

## Known gaps (documented, not fixed)

- **`portfolio_position` never populated**: the engine has
  `portfolio.portfolio_positions()` but `runner.run_pipeline` does not emit
  a positions frame, `callscore_core.frames` has no positions contract, and
  the store frame columns (`interval_id`, `notional`) don't line up with the
  engine frame. Wiring it needs contract + runner + `STAGE_TABLES` changes —
  deferred.
- **`calc_run.created_at` missing**: `calc_run` has `started_at`/
  `finished_at` but no creation timestamp; adding it is a schema v3
  migration (`_UPGRADES[2]`, `ALTER TABLE calc_run ADD COLUMN created_at`)
  — deferred.
- **Duplicated Provenance pydantic models** across API schema modules —
  cosmetic, skipped.
- **`callscore-synth --scale small --seed 20261006` can emit a post-delist
  rating**: observed `EV_000613` on `SEC_0050` effective 2024-09-14, one day
  after that security's only classification spell ends (delisting_date
  2024-09-13). `callscore run` then fails with `MissingBenchmarkData: No
  sector classification at entry`. Seed `7` (used by the E2E suite) and the
  `full` profile are unaffected. Synth should suppress ratings effective
  after `delisting_date` — reported as a follow-up.

`just benchmark` was verified end-to-end on `small` (recipe mechanics) and
`full` (numbers above); the `small` profile-seed run failure above is a
data issue, not a recipe issue.
