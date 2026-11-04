# 0001 — React SPA instead of Streamlit

## Context

The build plan specified Streamlit. The provided UI mockup, however, is a
dense 1320px desktop dashboard with bespoke SVG charts, clickable rows and
URL-shareable filters — none of which Streamlit can express without fighting
the framework.

## Decision

Build the frontend as a React + TypeScript + Vite SPA in `web/` with TanStack
Router/Query/Table, visx charts and Tailwind v4 on the mockup design tokens.
An openapi-typescript-generated client keeps the frontend typed against the
API. In production FastAPI serves `web/dist` directly.

## Consequences

- Two toolchains (uv + pnpm) must be maintained.
- The UI holds no calculation logic; everything flows through `/api`.
- Generated types must stay in sync, enforced by CI.
