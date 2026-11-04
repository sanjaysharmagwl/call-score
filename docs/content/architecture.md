# Architecture

The repo has four top-level folders:

- `server/` — uv workspace of Python packages (FastAPI backend).
- `web/` — Vite + React + TypeScript SPA.
- `data/` — profiles, fixtures and generated datasets.
- `docs/` — this site.

## Server package graph

```
callscore-core  <-  callscore-store  <-  callscore-synth
callscore-core  <-  callscore-engine  <-  callscore-calc
callscore-api   ->  core + store + engine
```

`callscore-engine` is a pure function of data + methodology: it must not
depend on `callscore-store`. `callscore-calc` and `callscore-calc-whatif` are
vendored from py-libs (see
[decision 0002](decisions/0002-vendor-pylibs-calc.md)).

## Web

The SPA talks to the API under `/api` only. Its TypeScript types are generated
from the API's OpenAPI schema (`just api-types`) and must stay in sync —
CI fails on drift. In production the FastAPI app serves `web/dist` itself.
