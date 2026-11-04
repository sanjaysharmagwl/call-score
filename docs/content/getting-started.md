# Getting started

Prerequisites: `uv`, `pnpm`, `just`, Node 24.

```bash
just setup      # sync the uv workspace, pnpm install, git hooks
just dev        # API on :8000 + Vite dev server together
just check      # lint, typecheck, tests and docs build for all components
```

Useful slices: `just dev-api`, `just dev-web`, `just server-check`,
`just web-check`, `just seed small`, `just api-types`, `just docs-serve`.
