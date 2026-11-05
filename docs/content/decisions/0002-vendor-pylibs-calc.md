# 0002 — Vendor pylibs-calc

## Context

`callscore-engine` builds on the `pylibs-calc` calculation engine, but
`pylibs-calc` is not installable from PyPI and a path reference to
`../py-libs` would break anyone who doesn't have that repo checked out next
to this one.

## Decision

Copy `packages/calc` and `packages/calc_whatif` from py-libs at commit
`f2acb4a` into this workspace as `callscore-calc` and `callscore-calc-whatif`
(`server/packages/calc`, `server/packages/calc-whatif`) and own them here.

## Consequences

- Fixes in either repo must be ported manually; there is no automatic sync.
- The vendored packages keep their own version lines and `requires-python`.
- **Update (T26):** as shipped, no product code imports `callscore_calc` —
  `callscore-engine` is self-contained and `callscore-api` never loads the
  calc plugin registry. The packages remain workspace members (own test
  suites, wheels in `dist/`); the unused `callscore-engine -> callscore-calc`
  dependency edge was removed. See [Performance](../performance.md#vendored-calc-packages).
