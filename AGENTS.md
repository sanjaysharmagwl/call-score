# AGENTS.md

Guidance for AI coding agents working in this repository.

## What this is

A full-stack monorepo for **callscore**, an analyst rating performance
platform. Four top-level folders:

- `server/` — uv virtual-workspace monorepo of `callscore-<pkg>` packages
  (import `callscore_<pkg>`): `core` (domain types), `store` (SQLite via
  SQLAlchemy, WAL + foreign keys on), `synth` (`callscore-synth` CLI data
  generator), `engine` (pure scoring on Polars), `api` (FastAPI, all routes
  under `/api`, serves `web/dist` in prod), and the vendored `calc` /
  `calc-whatif`.
- `web/` — pnpm Vite + React + TS SPA (`@callscore/web`): TanStack
  Router/Query, openapi-fetch client, Tailwind v4, Biome for lint/format.
- `data/` — `profiles/` + `fixtures/` committed; `raw/` `db/` `exports/`
  gitignored (`.gitkeep` retained).
- `docs/` — MkDocs Material site (`docs/mkdocs.yml`, `docs_dir: content`);
  deps live in the server `docs` dependency group.

## Commands

Everything runs through `just` from the repo root (`just` lists recipes):

- `just setup` — `uv sync --all-packages --all-extras` in `server/`,
  `pnpm install` in `web/`, pre-commit install.
- `just dev` — API (uvicorn `--reload`, :8000) + Vite dev server; Ctrl-C
  stops both. Slices: `just dev-api`, `just dev-web`.
- `just check` — `server-check` (ruff + format check + mypy strict + pytest)
  and `web-check` (biome + tsc + vitest) and `docs-build` (mkdocs --strict).
- `just server-test core` — one package's tests.
- `just seed small|medium|full` — write `data/db/callscore.db`.
- `just api-types` — regenerate `web/src/api/schema.gen.ts` from the API's
  OpenAPI schema.
- `just build` — web bundle + all server wheels into `dist/`.

## Conventions

- **Vendored calc**: `server/packages/calc{,-whatif}` are vendored copies of
  py-libs `pylibs-calc{,_whatif}` at `f2acb4a`, renamed to `callscore-calc{,-whatif}`.
  They keep their own versions and `requires-python >=3.10`; the py312-only
  ruff upgrades are per-file-ignored there. Fixes from upstream must be
  ported manually — see `docs/content/decisions/0002`.
- **Engine purity**: `callscore-engine` is a pure function of data +
  methodology — it must never depend on `callscore-store` or do I/O.
- **No calc logic in web**: the SPA only calls `/api`; calculations live in
  `callscore-engine`/`callscore-calc`.
- **Generated files are never edited**: `web/src/api/schema.gen.ts`
  (`just api-types`), `web/src/routeTree.gen.ts` (TanStack router plugin).
  CI fails on OpenAPI type drift.
- **FastAPI route modules**: do not add `from __future__ import annotations`
  — FastAPI needs real annotation objects.
- **Python**: new packages require `>=3.12`; ruff `py312`, line length 100,
  rules `E,F,I,UP,B,SIM`, pinned `ruff==0.16.9` (pre-commit rev `v0.16.9` —
  keep them in sync); mypy `--strict` with the `pydantic.mypy` plugin;
  pytest `--import-mode=importlib`, no `__init__.py` in tests.
- **Supply chain**: `exclude-newer = "7 days"` under `[tool.uv]` in
  `server/pyproject.toml`; `minimumReleaseAge: 10080` in
  `web/pnpm-workspace.yaml`.
- **Inter-package deps**: normal `dependencies` entry plus
  `[tool.uv.sources] X = { workspace = true }`.
