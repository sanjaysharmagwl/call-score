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
just seed small   # write a small synthetic DB to data/db/callscore.db (add `force` to overwrite)
just dev          # API (:8000) and Vite dev server together -> http://localhost:5173
just check        # lint + typecheck + tests + docs build, all components
```

Headless scoring runs use the `callscore` CLI shipped by `callscore-api`:

```bash
just run data/db/callscore.db 2025-12-31            # execute a scoring run
just leaderboard data/db/callscore.db RUN_ID        # print the composite leaderboard
just e2e                                           # Playwright suite (needs `pnpm exec playwright install chromium` once)
just build                                         # web/dist + server wheels into dist/
```

See `just` for every recipe, `docs/content/getting-started.md` for the full
local-product flow, and `AGENTS.md` for conventions.
