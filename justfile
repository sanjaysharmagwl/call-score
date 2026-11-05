# Usage: just <recipe> [args]   (run `just` or `just help` to list recipes)

# List every recipe
help:
    @just --list --unsorted

# Sync server workspace + web deps + git hooks
setup:
    cd server && uv sync --all-packages --all-extras
    cd web && pnpm install
    cd server && uv run pre-commit install

# Run the API (uvicorn --reload :8000) and the Vite dev server together; Ctrl-C stops both
dev:
    #!/usr/bin/env bash
    set -e
    (cd server && uv run uvicorn callscore_api.main:app --reload --port 8000) & API_PID=$!
    trap 'kill $API_PID 2>/dev/null' EXIT INT TERM
    cd web && pnpm dev

# API only, on :8000
dev-api:
    cd server && uv run uvicorn callscore_api.main:app --reload --port 8000

# Vite dev server only
dev-web:
    cd web && pnpm dev

# Full check: server + web + docs
check: server-check web-check docs-build

# Server: ruff lint + format check
server-lint:
    cd server && uv run ruff check .
    cd server && uv run ruff format --check .

# Server: ruff autofix + format
server-fmt:
    cd server && uv run ruff check --fix .
    cd server && uv run ruff format .

# Server: mypy strict over all package sources
server-typecheck:
    cd server && uv run mypy packages/*/src

# Server: all tests, or one package's (just server-test core)
server-test pkg="":
    cd server && uv run pytest {{ if pkg != "" { "packages/" + pkg } else { "" } }}

# Server: lint + typecheck + test
server-check: server-lint server-typecheck server-test

# Web: biome check
web-lint:
    cd web && pnpm lint

# Web: tsc -b
web-typecheck:
    cd web && pnpm typecheck

# Web: vitest
web-test:
    cd web && pnpm test

# Web: lint + typecheck + test
web-check: web-lint web-typecheck web-test

# Regenerate web/src/api/schema.gen.ts from the API's OpenAPI schema
api-types:
    cd server && uv run python -m callscore_api.openapi > ../web/openapi.json
    cd web && pnpm api-types
    rm web/openapi.json

# Generate a synthetic dataset (just seed small|medium|full)
seed scale="small":
    cd server && uv run callscore-synth --scale {{scale}} --out ../data/db/callscore.db

# Execute a scoring run against a DB:
#   just run ../data/db/callscore.db 2025-12-31 [methodology] [scale]
# methodology: 'default', a methodology JSON path, or a stored fingerprint.
# scale is optional ('', small|medium|full) — a data-scale hint, not a filter.
run db as_of methodology="default" scale="":
    #!/usr/bin/env bash
    set -euo pipefail
    root="$PWD"
    db="{{db}}"
    [[ "$db" = /* ]] || db="$root/$db"
    args=(run --db "$db" --as-of "{{as_of}}" --methodology "{{methodology}}")
    if [[ -n "{{scale}}" ]]; then args+=(--scale "{{scale}}"); fi
    cd server && uv run callscore "${args[@]}"

# Print a run's composite leaderboard:
#   just leaderboard ../data/db/callscore.db RUN_ID [limit]
leaderboard db run limit="20":
    #!/usr/bin/env bash
    set -euo pipefail
    root="$PWD"
    db="{{db}}"
    [[ "$db" = /* ]] || db="$root/$db"
    cd server && uv run callscore leaderboard --db "$db" --run {{run}} --limit {{limit}}

# Run the Playwright E2E suite (builds the SPA, seeds throwaway DBs, serves
# the API+SPA on :4173/:4174). First run: `cd web && pnpm exec playwright install chromium`
e2e:
    cd web && pnpm test:e2e

# Benchmark seed + scoring run end-to-end on a profile (uses /usr/bin/time -l,
# macOS only): writes data/db/bench-<profile>.db, never touches callscore.db.
#   just benchmark [profile=full] [seed=20261006] [as_of=2025-12-31]
benchmark profile="full" seed="20261006" as_of="2025-12-31":
    #!/usr/bin/env bash
    set -euo pipefail
    db="../data/db/bench-{{profile}}.db"
    cd server
    echo "== seed: callscore-synth --scale {{profile}} --seed {{seed}} =="
    /usr/bin/time -l uv run callscore-synth --scale {{profile}} --seed {{seed}} --out "$db" --force
    echo "== run: callscore run --scale {{profile}} --as-of {{as_of}} --methodology default =="
    /usr/bin/time -l uv run callscore run --db "$db" --scale {{profile}} --as-of {{as_of}} --methodology default

# Preview the docs site (reloads on edit)
docs-serve:
    cd server && uv run --group docs mkdocs serve -f ../docs/mkdocs.yml

# Build the docs site (strict: warnings are errors)
docs-build:
    cd server && uv run --group docs mkdocs build --strict -f ../docs/mkdocs.yml

# Build the SPA and all server wheels/sdists into dist/
build:
    cd web && pnpm build
    cd server && uv build --all-packages -o ../dist

# Remove build artifacts and caches
clean:
    rm -rf dist site web/dist server/.pytest_cache server/.mypy_cache server/.ruff_cache
    find . -name __pycache__ -type d -prune -exec rm -rf {} +
