import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { createMemoryHistory, createRouter, RouterProvider } from "@tanstack/react-router";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { vi } from "vitest";
import { routeTree } from "../../routeTree.gen";
import type { JsonObject } from "./fields";

export { cleanup, fireEvent, screen, waitFor };

export const RUN_BASE = "aaaaaaaa-0000-0000-0000-00000000000a";
export const RUN_WHATIF = "bbbbbbbb-0000-0000-0000-00000000000b";
export const FP_BASE = "a".repeat(64);
export const FP_ALT = "b".repeat(64);
export const SCENARIO_ID = "scn-test0001";

export function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json" },
  });
}

export function requestUrl(input: RequestInfo | URL): string {
  if (typeof input === "string") return input;
  if (input instanceof URL) return input.toString();
  if (input instanceof Request) return input.url;
  return String(input);
}

export interface FetchCall {
  url: string;
  method: string;
  body?: unknown;
}

/**
 * Stub `fetch` with a path-prefix -> response handler. Returns recorded
 * calls (url + parsed JSON body for non-GET) for assertions.
 */
export function stubApi(
  routes: Record<string, unknown | ((url: string, init?: RequestInit) => Response)>,
): FetchCall[] {
  const calls: FetchCall[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = requestUrl(input);
      // openapi-fetch passes a constructed Request; init may be absent.
      let method = init?.method ?? "GET";
      let rawBody: unknown = init?.body;
      if (input instanceof Request) {
        method = input.method;
        if (rawBody === undefined || rawBody === null) {
          const text = await input.clone().text();
          rawBody = text.length > 0 ? text : undefined;
        }
      }
      let body: unknown = rawBody;
      if (typeof rawBody === "string") {
        try {
          body = JSON.parse(rawBody);
        } catch {
          body = rawBody;
        }
      }
      calls.push({ url, method, body });
      const effInit = { method, body: typeof rawBody === "string" ? rawBody : undefined };
      if (url.includes("/api/health")) {
        return jsonResponse({ status: "ok", version: "0.1.0" });
      }
      for (const [prefix, handler] of Object.entries(routes)) {
        if (url.includes(prefix)) {
          if (typeof handler === "function") return handler(url, effInit);
          return jsonResponse(handler);
        }
      }
      return jsonResponse({ detail: "not found" }, 404);
    }),
  );
  return calls;
}

export function renderRoute(initialEntry: string) {
  const router = createRouter({
    routeTree,
    history: createMemoryHistory({ initialEntries: [initialEntry] }),
  });
  render(
    <QueryClientProvider client={new QueryClient()}>
      <RouterProvider router={router} />
    </QueryClientProvider>,
  );
  return router;
}

export function runSummary(runId: string, overrides: Record<string, unknown> = {}) {
  return {
    run_id: runId,
    status: "succeeded",
    progress: 1,
    as_of_date: "2024-06-28",
    knowledge_cutoff: "2024-06-28T00:00:00Z",
    started_at: "2024-06-28T01:00:00Z",
    finished_at: "2024-06-28T01:05:00Z",
    methodology_fingerprint: FP_BASE,
    ...overrides,
  };
}

export function runsResponse(runs: unknown[] = [runSummary(RUN_BASE)]) {
  return {
    data: runs,
    pagination: { limit: 100, offset: 0, count: runs.length, total: runs.length },
  };
}

/** A complete methodology payload matching the API contract. */
export function methodologyFixture(overrides: JsonObject = {}): JsonObject {
  return {
    contract_version: 1,
    entry_convention: "next_open",
    expiry_policy: "hard_close",
    decay_days: 90,
    benchmark_mode: "sector",
    benchmark_id: "IDX_BROAD",
    scoring_mode: "absolute",
    position_weights: {
      strong_buy: 1,
      buy: 0.5,
      hold: 0,
      sell: -0.5,
      strong_sell: -1,
    },
    fixed_horizons_days: [63, 126, 252],
    primary_fixed_horizon_days: 126,
    min_consensus_ratings: 3,
    min_ratings_for_rank: 20,
    shrinkage_method: "empirical_bayes",
    peer_group_dims: ["cap_bucket", "region", "sector"],
    min_peer_size: 5,
    peer_fallback: "unranked",
    transaction_cost_bps: 0,
    include_announcement_window: true,
    return_basis: "total",
    family_weights: {
      return_skill: 0.4,
      timing: 0.15,
      price_target: 0.15,
      behavior: 0.1,
      consistency: 0.2,
    },
    bootstrap_draws: 1000,
    bootstrap_seed: 20261006,
    confidence_level: 0.95,
    reporting_currency: "USD",
    missing_fx_policy: "fail",
    ...overrides,
  };
}

export function methodologyDefaults() {
  return {
    methodology: methodologyFixture(),
    fingerprint: FP_BASE,
    presets: ["default"],
  };
}

export function methodologyVersion(overrides: Record<string, unknown> = {}) {
  return {
    id: 1,
    fingerprint: FP_BASE,
    is_baseline: true,
    created_at: "2024-01-01T00:00:00Z",
    params: methodologyFixture(),
    ...overrides,
  };
}

export function methodologiesListResponse() {
  return {
    data: [
      methodologyVersion(),
      methodologyVersion({
        id: 2,
        fingerprint: FP_ALT,
        is_baseline: false,
        created_at: "2024-02-01T00:00:00Z",
        params: methodologyFixture({ decay_days: 60 }),
      }),
    ],
    pagination: { limit: 100, offset: 0, count: 2, total: 2 },
  };
}

export function methodologyDiffResponse() {
  return {
    a_fingerprint: FP_BASE,
    b_fingerprint: FP_ALT,
    identical: false,
    changes: [
      { path: "decay_days", a: 90, b: 60, a_missing: false, b_missing: false },
      { path: "transaction_cost_bps", a: 0, b: 25, a_missing: false, b_missing: false },
    ],
  };
}

export function scenarioSummary(overrides: Record<string, unknown> = {}) {
  return {
    scenario_id: SCENARIO_ID,
    name: "Cost stress",
    description: "Higher transaction costs",
    created_at: "2024-03-01T00:00:00Z",
    base_fingerprint: FP_BASE,
    base_ref: "default",
    overrides: { transaction_cost_bps: 25 },
    universe_filter: null,
    run_count: 1,
    last_run_id: RUN_WHATIF,
    ...overrides,
  };
}

export function scenarioDetail(overrides: Record<string, unknown> = {}) {
  return {
    ...scenarioSummary(),
    run_ids: [RUN_WHATIF],
    base_methodology: methodologyFixture(),
    last_execution: {
      run_id: RUN_WHATIF,
      as_of_date: "2024-06-28",
      methodology_fingerprint: FP_ALT,
      created: true,
      at: "2024-06-28T02:00:00Z",
    },
    sweeps: [],
    ...overrides,
  };
}

export function compareResponse(overrides: Record<string, unknown> = {}) {
  return {
    run_a_id: RUN_BASE,
    run_b_id: RUN_WHATIF,
    run_a_status: "succeeded",
    run_b_status: "succeeded",
    as_of_date_a: "2024-06-28",
    as_of_date_b: "2024-06-28",
    methodology_a_fingerprint: FP_BASE,
    methodology_b_fingerprint: FP_ALT,
    universe: { a: null, b: null, compatible: true, intersection: null },
    summary: {
      n_analysts_a: 2,
      n_analysts_b: 2,
      n_common: 2,
      only_in_a: 0,
      only_in_b: 0,
      ranked_to_ranked: 2,
      ranked_to_unranked: 0,
      unranked_to_ranked: 0,
      unranked_to_unranked: 0,
    },
    rank_correlation: { method: "spearman_overall_rank", n: 2, spearman: 0.5 },
    analysts: [
      {
        analyst_id: "AN_1",
        in_a: true,
        in_b: true,
        composite_score_a: 0.5,
        composite_score_b: 0.55,
        score_delta: 0.05,
        rank_overall_a: 1,
        rank_overall_b: 2,
        rank_delta: 1,
        rank_in_peer_group_a: 1,
        rank_in_peer_group_b: 2,
        peer_rank_delta: 1,
        is_ranked_a: true,
        is_ranked_b: true,
        transition: "ranked",
      },
    ],
    metric_deltas: [
      {
        metric_code: "hit_rate",
        family: "return_skill",
        n_pairs: 2,
        mean_delta: 0.01,
        mean_abs_delta: 0.01,
        max_abs_delta: 0.02,
      },
    ],
    family_deltas: [{ family: "return_skill", n_metrics: 3, mean_delta: 0.01 }],
    ...overrides,
  };
}
