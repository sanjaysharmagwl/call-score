# callscore

Analyst rating performance platform: it ingests analyst rating calls on
securities, tracks what each security did afterwards, and scores every
analyst's track record against it.

## Layout

| Dir | What |
| --- | --- |
| `server/` | uv workspace of Python packages: `callscore-core`, `-store`, `-synth`, `-engine`, `-api`, plus vendored `-calc` / `-calc-whatif`. |
| `web/` | Vite + React + TypeScript SPA (`@callscore/web`), Tailwind v4 on the mockup design tokens. |
| `data/` | Scale profiles and fixtures (committed); raw dumps, DBs, exports (gitignored). |
| `docs/` | MkDocs Material site: guides, design artifacts, ADRs, API reference. |

## Prerequisites

`uv`, `pnpm`, `just`, Node 24.

## Quickstart

```bash
just setup        # install everything + git hooks
just dev          # API (:8000) and Vite dev server together
just seed small   # write a small synthetic DB to data/db/callscore.db
just check        # lint + typecheck + tests + docs build, all components
```

See `just` for every recipe and `AGENTS.md` for conventions.
