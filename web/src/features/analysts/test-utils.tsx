import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { createMemoryHistory, createRouter, RouterProvider } from "@tanstack/react-router";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { vi } from "vitest";
import { routeTree } from "../../routeTree.gen";

export const RUN_A = "aaaaaaaa-0000-0000-0000-00000000000a";
export const ANALYST_ID = "AN_001";
export const INTERVAL_ID = "INT_001";

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
}

/**
 * Stub `fetch` with a path-prefix -> response handler. Returns the recorded
 * request URLs for assertions.
 */
export function stubApi(
  routes: Record<string, unknown | ((url: string) => Response)>,
): FetchCall[] {
  const calls: FetchCall[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL) => {
      const url = requestUrl(input);
      calls.push({ url });
      if (url.includes("/api/health")) {
        return jsonResponse({ status: "ok", version: "0.1.0" });
      }
      for (const [prefix, handler] of Object.entries(routes)) {
        if (url.includes(prefix)) {
          if (typeof handler === "function") return handler(url);
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

export function provenance(runId: string = RUN_A) {
  return {
    run_id: runId,
    methodology_fingerprint: "f".repeat(64),
    as_of_date: "2024-06-28",
    run_status: "succeeded",
  };
}

export function runSummary(runId: string) {
  return {
    run_id: runId,
    status: "succeeded",
    progress: 1,
    as_of_date: "2024-06-28",
    knowledge_cutoff: "2024-06-28T00:00:00Z",
    started_at: "2024-06-28T01:00:00Z",
    finished_at: "2024-06-28T01:05:00Z",
    methodology_fingerprint: "f".repeat(64),
  };
}

export function runsResponse() {
  return {
    data: [runSummary(RUN_A)],
    pagination: { limit: 100, offset: 0, count: 1, total: 1 },
  };
}

export function metricCatalogResponse() {
  return {
    metrics: [
      {
        code: "hit_rate",
        family: "return_skill",
        unit: "fraction",
        direction: "higher",
        horizon: "to_exit",
        required_inputs: [],
        formula: "mean(sign(excess)>0)",
        estimand: "P(excess>0)",
        unavailable_when: "n<min",
        min_obs: 5,
        scored: true,
        contributes_to_composite: true,
        inference: "quarter_block_bootstrap",
        shrink: true,
        aggregate: "mean",
        null_value: null,
      },
      {
        code: "mean_contribution",
        family: "return_skill",
        unit: "return",
        direction: "higher",
        horizon: "to_exit",
        required_inputs: [],
        formula: "mean(contribution)",
        estimand: "E[contribution]",
        unavailable_when: "n<min",
        min_obs: 5,
        scored: true,
        contributes_to_composite: true,
        inference: "quarter_block_bootstrap",
        shrink: true,
        aggregate: "mean",
        null_value: null,
      },
    ],
    composite_spec: {},
    fingerprint: "cat1",
  };
}

export function leaderboardRow(overrides: Record<string, unknown> = {}) {
  return {
    rank: 1,
    analyst_id: ANALYST_ID,
    name: "Alice Analyst",
    firm_id: "F1",
    peer_group: "tech-large",
    composite_score: 0.421,
    rank_in_peer_group: 1,
    is_ranked: true,
    unranked_reason: null,
    n_investable: 12,
    family_scores: {
      return_skill: 0.5,
      timing: 0.2,
      price_target: 0.1,
      behavior: 0.3,
      consistency: 0.4,
    },
    key_metrics: {
      mean_contribution: { value: 0.012, n_obs: 12, unavailable_reason: null },
      hit_rate: { value: 0.6, n_obs: 10, unavailable_reason: null },
      information_ratio: { value: 0.31, n_obs: 12, unavailable_reason: null },
      target_hit_rate: { value: null, n_obs: 0, unavailable_reason: "no_target_outcomes" },
    },
    ...overrides,
  };
}

export function leaderboardResponse(items: unknown[] = [leaderboardRow()]) {
  return {
    items,
    total: items.length,
    limit: 50,
    offset: 0,
    provenance: provenance(),
  };
}

export function analystListItem(overrides: Record<string, unknown> = {}) {
  return {
    analyst_id: ANALYST_ID,
    name: "Alice Analyst",
    firm_id: "F1",
    firm_name: "Acme Research",
    peer_group: "tech-large",
    composite_score: 0.421,
    rank_overall: 1,
    rank_in_peer_group: 1,
    is_ranked: true,
    unranked_reason: null,
    n_obs: 12,
    key_metrics: {
      hit_rate: { value: 0.6, n_obs: 10, unavailable_reason: null },
    },
    ...overrides,
  };
}

export function analystListResponse(items: unknown[] = [analystListItem()]) {
  return {
    items,
    total: items.length,
    limit: 50,
    offset: 0,
    provenance: provenance(),
  };
}

export function metricDetail(overrides: Record<string, unknown> = {}) {
  return {
    metric_code: "hit_rate",
    family: "return_skill",
    unit: "fraction",
    direction: "higher",
    minimum_observations: 5,
    in_composite: true,
    formula: "mean(sign(excess)>0)",
    unavailable_when: "n<5",
    value: 0.61,
    n_obs: 20,
    std_err: 0.05,
    t_stat: 2.2,
    ci_low: 0.51,
    ci_high: 0.71,
    shrunk_value: 0.6,
    peer_percentile: 82,
    is_significant: true,
    unavailable_reason: null,
    ...overrides,
  };
}

export function analystProfile(overrides: Record<string, unknown> = {}) {
  return {
    analyst_id: ANALYST_ID,
    name: "Alice Analyst",
    firm_id: "F1",
    firm_name: "Acme Research",
    employment_spells: [
      {
        analyst_id: ANALYST_ID,
        firm_id: "F1",
        security_id: null,
        from_date: "2015-01-01",
        to_date: null,
        is_lead: true,
      },
    ],
    coverage_spells: [
      {
        analyst_id: ANALYST_ID,
        firm_id: null,
        security_id: "SEC_1",
        from_date: "2020-06-01",
        to_date: "2024-01-31",
        is_lead: false,
      },
    ],
    peer_group: "tech-large",
    composite: {
      peer_group: "tech-large",
      composite_score: 0.421,
      rank_overall: 3,
      rank_in_peer_group: 1,
      is_ranked: true,
      unranked_reason: null,
      n_obs: 12,
      family_scores: {
        return_skill: 0.5,
        timing: 0.2,
        price_target: 0.1,
        behavior: 0.3,
        consistency: 0.4,
      },
      shrinkage: { method: "empirical_bayes", shrunk_values_present: true },
    },
    metrics: [
      metricDetail(),
      metricDetail({
        metric_code: "max_drawdown",
        family: "return_skill",
        unit: "return",
        value: null,
        n_obs: 0,
        ci_low: null,
        ci_high: null,
        shrunk_value: null,
        peer_percentile: null,
        is_significant: null,
        unavailable_reason: "insufficient_obs",
      }),
    ],
    provenance: provenance(),
    ...overrides,
  };
}

export function ratingListItem(overrides: Record<string, unknown> = {}) {
  return {
    interval_id: INTERVAL_ID,
    event_id: "EV_001",
    security_id: "SEC_1",
    native_rating: "buy",
    canonical_rating: "buy",
    action_type: "initiation",
    announced_ts: "2024-01-10T14:30:00Z",
    effective_ts: "2024-01-11T00:00:00Z",
    entry_session: "2024-01-12",
    entry_point: "open",
    entry_ts: "2024-01-12T14:30:00Z",
    exit_session: "2024-02-12",
    exit_point: "close",
    exit_ts: "2024-02-12T21:00:00Z",
    exit_reason: "superseded",
    is_investable: true,
    non_investable_reason: null,
    flags: null,
    price_target: 150,
    pt_currency: "USD",
    pt_horizon_months: 12,
    exposures: [
      {
        segment_no: 0,
        start_session: "2024-01-12",
        start_point: "open",
        end_session: "2024-02-12",
        end_point: "close",
        position_weight: 0.5,
        weight_source: "canonical",
      },
    ],
    return_summary: {
      stock_return: 0.1,
      benchmark_return: 0.05,
      excess_return: 0.05,
      terminated_by_delisting: false,
      unavailable_reason: null,
    },
    ...overrides,
  };
}

export function ratingListResponse(items: unknown[] = [ratingListItem()]) {
  return {
    items,
    total: items.length,
    limit: 20,
    offset: 0,
    provenance: provenance(),
  };
}

export function portfolioResponse(overrides: Record<string, unknown> = {}) {
  return {
    analyst_id: ANALYST_ID,
    points: [
      {
        session_date: "2024-01-12",
        nav: 1.0,
        benchmark_nav: 1.0,
        active_return: 0,
        gross_exposure: 0.5,
        turnover: 0,
        cost: 0,
        drawdown: 0,
      },
      {
        session_date: "2024-02-12",
        nav: 1.025,
        benchmark_nav: 1.01,
        active_return: 0.015,
        gross_exposure: 0.5,
        turnover: 0.2,
        cost: 0.001,
        drawdown: -0.01,
      },
    ],
    total_points: 2,
    downsampled: false,
    positions: {
      session_date: "2024-02-12",
      rows: [{ security_id: "SEC_1", position_weight: 0.5, market_value: 1025 }],
    },
    provenance: provenance(),
    ...overrides,
  };
}

export function ratingDetail(overrides: Record<string, unknown> = {}) {
  return {
    interval_id: INTERVAL_ID,
    event_id: "EV_001",
    analyst_id: ANALYST_ID,
    firm_id: "F1",
    security_id: "SEC_1",
    native_rating: "buy",
    canonical_rating: "buy",
    action_type: "initiation",
    announced_ts: "2024-01-10T14:30:00Z",
    effective_ts: "2024-01-11T00:00:00Z",
    knowledge_ts: "2024-01-10T15:00:00Z",
    available_ts: "2024-01-10T16:00:01Z",
    entry_session: "2024-01-12",
    entry_point: "open",
    entry_ts: "2024-01-12T14:30:00Z",
    exit_session: "2024-02-12",
    exit_point: "close",
    exit_ts: "2024-02-12T21:00:00Z",
    exit_reason: "superseded",
    expiry_date: null,
    decay_start_session: null,
    is_investable: true,
    non_investable_reason: null,
    flags: null,
    event: {
      event_id: "EV_001",
      analyst_id: ANALYST_ID,
      firm_id: "F1",
      security_id: "SEC_1",
      action_type: "initiation",
      native_rating: "buy",
      canonical_rating: "buy",
      prior_canonical_rating: null,
      scale_id: "scale-5",
      price_target: 150,
      pt_currency: "USD",
      pt_horizon_months: 12,
      announced_ts: "2024-01-10T14:30:00Z",
      effective_ts: "2024-01-11T00:00:00Z",
      knowledge_ts: "2024-01-10T15:00:00Z",
      is_correction: true,
      supersedes_event_id: "EV_000",
      source: "manual",
    },
    exposures: [
      {
        segment_no: 0,
        start_session: "2024-01-12",
        start_point: "open",
        end_session: "2024-02-12",
        end_point: "close",
        position_weight: 0.5,
        weight_source: "canonical",
      },
    ],
    returns: [
      {
        horizon: "to_exit",
        start_session: "2024-01-12",
        start_point: "open",
        end_session: "2024-02-12",
        end_point: "close",
        entry_price: 100,
        exit_price: 110,
        stock_return: 0.1,
        benchmark_id: "MKT_US",
        benchmark_return: 0.05,
        beta: 1.1,
        excess_return: 0.05,
        terminated_by_delisting: false,
        unavailable_reason: null,
      },
    ],
    contributions: [
      {
        horizon: "to_exit",
        position_weight: 0.5,
        excess_return: 0.05,
        contribution: 0.025,
        unavailable_reason: null,
      },
    ],
    event_window: {
      event_session: "2024-01-10",
      weight_change: 0.5,
      consensus_deviation: 0.2,
      car_pm1: 0.02,
      pre_event_excess_20d: 0.01,
      post_event_excess_20d: 0.03,
      unavailable_reason: null,
    },
    target_outcome: {
      entry_session: "2024-01-12",
      horizon_end_session: "2025-01-12",
      entry_price: 100,
      target_price: 150,
      implied_return: 0.5,
      realized_return: 0.1,
      hit: false,
      is_matured: true,
    },
    explanation:
      "Initiation to buy on SEC_1 (announced 2024-01-10T14:30:00Z). " +
      "Position held 2024-01-12 open -> 2024-02-12 close over 31 calendar days; " +
      "exit reason superseded. [to_exit] weight 0.5000 x excess +5.00% " +
      "(stock +10.00% vs benchmark +5.00%) = +2.50% contribution.",
    provenance: provenance(),
    ...overrides,
  };
}

export { cleanup, fireEvent, screen, waitFor };
