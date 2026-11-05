#!/usr/bin/env bash
# Playwright webServer launcher for the callscore E2E suite.
#
# Usage: serve.sh seeded|empty   (E2E_PORT required)
#
# - seeded: fresh tmp DB -> callscore-synth --scale small --seed 7 ->
#   `callscore run --as-of $E2E_AS_OF` -> uvicorn serving API + SPA.
# - empty:  fresh tmp DB path (bootstrapped empty by the app) -> uvicorn.
#
# The tmp workdir is unique per launch and removed on exit; the developer
# database at data/db/callscore.db is never touched.
set -euo pipefail

MODE="${1:?usage: serve.sh seeded|empty}"
PORT="${E2E_PORT:?E2E_PORT is required}"
AS_OF="${E2E_AS_OF:-2025-06-30}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
SERVER_DIR="$REPO_ROOT/server"
WEB_DIR="$REPO_ROOT/web"

WORK="$(mktemp -d "${TMPDIR:-/tmp}/callscore-e2e-${MODE}.XXXXXX")"
DB_PATH="$WORK/callscore.db"
trap 'rm -rf "$WORK"' EXIT

echo "[e2e serve] mode=$MODE port=$PORT db=$DB_PATH" >&2

cd "$SERVER_DIR"

if [ "$MODE" = "seeded" ]; then
  uv run callscore-synth --scale small --seed "${E2E_SEED:-7}" --out "$DB_PATH" --force >/dev/null
  uv run callscore run --db "$DB_PATH" --methodology default --as-of "$AS_OF" >/dev/null
fi

CALLSCORE_DB_PATH="$DB_PATH" \
CALLSCORE_WEB_DIST="$WEB_DIR/dist" \
CALLSCORE_PORT="$PORT" \
  exec uv run uvicorn "callscore_api.app:create_app" --factory \
    --host 127.0.0.1 --port "$PORT"
