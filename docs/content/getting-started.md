# Getting started

Prerequisites: `uv`, `pnpm`, `just`, Node 24.

```bash
just setup      # sync the uv workspace, pnpm install, git hooks
just seed small # write a small synthetic DB to data/db/callscore.db
just dev        # API on :8000 + Vite dev server together -> http://localhost:5173
just check      # lint, typecheck, tests and docs build for all components
```

## The local product flow

1. `just setup` — one-time install (`uv sync --all-packages`, `pnpm install`,
   pre-commit hooks).
2. `just seed small` — generate a synthetic dataset with `callscore-synth`
   (`small`, `medium` or `full`; `just seed medium` etc.). The DB lands at
   `data/db/callscore.db`; re-running requires adding `force` as a second
   argument (`just seed small force`) or deleting the file.
3. `just dev` — starts the FastAPI backend on :8000 and the Vite dev server
   on :5173. The SPA talks only to `/api`; there is no calc logic in the web
   bundle.
4. Open <http://localhost:5173> — the overview page shows first-use guidance
   until a scoring run exists.

## Scoring runs from the CLI

The API also ships a `callscore` CLI (in `callscore-api`) for headless runs:

```bash
just run data/db/callscore.db 2025-12-31          # methodology defaults to 'default'
just leaderboard data/db/callscore.db RUN_ID      # composite leaderboard, top 20
```

`callscore run` flags: `--db`, `--as-of`, `--methodology`
(`default`, a methodology JSON path, or a stored fingerprint),
`--knowledge-cutoff`, `--run-id`, `--scale`. `just run` forwards the first
two positionally and accepts `methodology`/`scale` as optional args — see
`just help run`.

Inside the SPA, runs are submitted through `POST /api/runs` and executed by
the background job coordinator (single-writer lease, per-stage Parquet cache
under `data/db/cache/`). `just dev-api` alone serves the API **and** the
built SPA from `web/dist` when it exists — one process, one origin.

## Other useful recipes

| Recipe | What it does |
| --- | --- |
| `just dev-api` / `just dev-web` | API or Vite dev server only |
| `just api-types` | regenerate `web/src/api/schema.gen.ts` from the OpenAPI schema |
| `just server-check` / `just web-check` / `just docs-build` | per-component checks |
| `just e2e` | Playwright suite (builds SPA, seeds throwaway DBs, serves :4173/:4174); first run needs `cd web && pnpm exec playwright install chromium` |
| `just benchmark` | timed `callscore-synth` + `callscore run` on a profile (see [Performance](performance.md)) |
| `just build` | SPA bundle + all server wheels into `dist/` |
| `just docs-serve` | live-reload docs preview |

## Configuration

The API reads `CALLSCORE_*` environment variables — `CALLSCORE_DB_PATH`,
`CALLSCORE_CACHE_DIR`, `CALLSCORE_HOST`/`CALLSCORE_PORT`,
`CALLSCORE_JOB_LEASE_TTL_SECONDS`, `CALLSCORE_JOB_POLL_INTERVAL_SECONDS`,
`CALLSCORE_WEB_DIST`, `CALLSCORE_LOG_LEVEL` (see
`callscore_api.settings`). Defaults resolve relative to the repo root, so a
fresh clone works without configuration.
