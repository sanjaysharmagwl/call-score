import { execSync } from "node:child_process";
import { defineConfig, devices } from "@playwright/test";

/**
 * Playwright E2E for the callscore SPA + FastAPI app.
 *
 * Two web servers are launched (single origin each — the FastAPI app serves
 * both `/api` and the built SPA from `web/dist`):
 *
 * - `main`   (port E2E_PORT, default 4173): a FRESH tmp SQLite DB seeded via
 *   `callscore-synth --scale small --seed 7` plus one succeeded `callscore
 *   run` (as_of 2025-06-30). Never touches `data/db/callscore.db`.
 * - `empty`  (port E2E_EMPTY_PORT, default 4174): a fresh bootstrapped DB
 *   with zero calc runs, for first-use guidance assertions.
 *
 * `e2e/serve.sh` does the seeding + run + uvicorn launch per mode. The SPA
 * bundle is built once here, before the servers start, so both can share it.
 */

const PORT = Number(process.env.E2E_PORT ?? 4173);
const EMPTY_PORT = Number(process.env.E2E_EMPTY_PORT ?? 4174);
const BASE_URL = `http://127.0.0.1:${PORT}`;
const EMPTY_URL = `http://127.0.0.1:${EMPTY_PORT}`;

// Build the SPA bundle once; both servers mount it via CALLSCORE_WEB_DIST.
execSync("pnpm build", { stdio: "inherit" });

export default defineConfig({
  testDir: "./e2e",
  // The suite mutates shared state (scenarios, backtests, runs) — serialize.
  workers: 1,
  retries: process.env.CI ? 1 : 0,
  timeout: 60_000,
  expect: { timeout: 15_000 },
  reporter: [["list"]],
  use: {
    baseURL: BASE_URL,
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
  },
  projects: [
    {
      name: "main",
      testIgnore: "empty.spec.ts",
      use: { ...devices["Desktop Chrome"], baseURL: BASE_URL },
    },
    {
      name: "empty",
      testMatch: /empty\.spec\.ts$/,
      use: { ...devices["Desktop Chrome"], baseURL: EMPTY_URL },
    },
  ],
  webServer: [
    {
      command: "bash e2e/serve.sh seeded",
      url: `${BASE_URL}/api/health`,
      timeout: 300_000,
      reuseExistingServer: false,
      env: { E2E_PORT: String(PORT), E2E_AS_OF: process.env.E2E_AS_OF ?? "2025-06-30" },
    },
    {
      command: "bash e2e/serve.sh empty",
      url: `${EMPTY_URL}/api/health`,
      timeout: 300_000,
      reuseExistingServer: false,
      env: { E2E_PORT: String(EMPTY_PORT) },
    },
  ],
});
