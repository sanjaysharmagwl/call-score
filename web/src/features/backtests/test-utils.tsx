import { vi } from "vitest";
import { jsonResponse, RUN_A, requestUrl } from "../analysts/test-utils";

export {
  analystListItem,
  analystListResponse,
  cleanup,
  fireEvent,
  renderRoute,
  runsResponse,
  screen,
  waitFor,
} from "../analysts/test-utils";
export { jsonResponse, RUN_A, requestUrl };

export const BT_ID = "bt-0123456789abcdef";
export const BENCH_ID = "CSTM_0123456789abcdef";

export interface FetchCall {
  url: string;
  method: string;
  body?: unknown;
}

type Handler = (call: FetchCall) => Response;

/**
 * Stub `fetch` with method+path-prefix routing. Route keys look like
 * `"GET /api/backtests"` or `"POST /api/backtests"`; a bare path matches any
 * method. More specific (longer) paths win over prefixes. Request bodies are
 * captured for assertions. Returns recorded calls.
 */
export function stubApi(routes: Record<string, unknown | Handler>): FetchCall[] {
  const calls: FetchCall[] = [];
  const entries = Object.entries(routes)
    .map(([key, handler]) => {
      const m = /^(GET|POST|PUT|PATCH|DELETE)\s+(.*)$/.exec(key);
      return {
        method: m?.[1],
        path: m?.[2] ?? key,
        handler,
      };
    })
    .sort((a, b) => b.path.length - a.path.length);

  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = requestUrl(input);
      const method = input instanceof Request ? input.method : (init?.method ?? "GET");
      let body: unknown;
      try {
        if (input instanceof Request && input.body !== null) {
          body = await input.clone().json();
        } else if (typeof init?.body === "string") {
          body = JSON.parse(init.body);
        }
      } catch {
        body = undefined;
      }
      const call: FetchCall = { url, method, body };
      calls.push(call);
      if (url.includes("/api/health")) {
        return jsonResponse({ status: "ok", version: "0.1.0" });
      }
      for (const entry of entries) {
        if (!url.includes(entry.path)) continue;
        if (entry.method && entry.method !== method) continue;
        if (typeof entry.handler === "function") return entry.handler(call);
        return jsonResponse(entry.handler);
      }
      return jsonResponse({ detail: "not found" }, 404);
    }),
  );
  return calls;
}

// ---------------------------------------------------------------------------
// Fixtures (hand-built, contract-shaped)
// ---------------------------------------------------------------------------

export function backtestSummary(overrides: Record<string, unknown> = {}) {
  return {
    backtest_id: BT_ID,
    name: "Quality momentum",
    status: "succeeded",
    created_at: "2024-03-01T10:00:00Z",
    started_at: "2024-03-01T10:00:05Z",
    finished_at: "2024-03-01T10:02:00Z",
    benchmark_id: "IDX_BROAD",
    error: null,
    ...overrides,
  };
}

export function backtestListResponse(items: unknown[] = [backtestSummary()]) {
  return {
    data: items,
    pagination: { limit: 200, offset: 0, count: items.length, total: items.length },
  };
}

export function backtestDetail(overrides: Record<string, unknown> = {}) {
  return {
    ...backtestSummary(),
    spec: {
      selection: "all",
      analyst_ids: null,
      score_field: "position_weight",
      sizing: "equal",
      mode: "long_only",
      gross_exposure_cap: 1.0,
      max_position_weight: 1.0,
      allow_shorting: true,
      vol_lookback_sessions: 60,
      rebalance: "monthly",
      horizon: "until_change",
      transaction_cost_bps: 5.0,
      benchmark_id: "IDX_BROAD",
    },
    monthly_returns: [
      { year: 2024, month: 1, month_return: 0.021, nav_end: 1.021 },
      { year: 2024, month: 2, month_return: -0.01, nav_end: 1.011 },
    ],
    attribution: [
      { dimension: "sector", group_key: "tech", contribution: 0.012 },
      { dimension: "analyst", group_key: "AN_001", contribution: 0.008 },
      { dimension: "rating_action", group_key: "upgrade", contribution: 0.005 },
    ],
    ...overrides,
  };
}

export function navResponse(overrides: Record<string, unknown> = {}) {
  return {
    data: [
      {
        session_date: "2024-01-02",
        nav: 1.0,
        benchmark_nav: 1.0,
        active_return: 0,
        gross_exposure: 0.5,
        turnover: 0,
        cost: 0,
        drawdown: 0,
      },
      {
        session_date: "2024-01-03",
        nav: 1.01,
        benchmark_nav: 1.005,
        active_return: 0.005,
        gross_exposure: 0.5,
        turnover: 0.1,
        cost: 0.0005,
        drawdown: -0.005,
      },
    ],
    interval: "1d",
    downsampled: false,
    source_rows: 2,
    total: 2,
    ...overrides,
  };
}

export function statsResponse() {
  return {
    data: [
      {
        stat_code: "annualized_return",
        value: 0.1234,
        std_err: null,
        ci_low: null,
        ci_high: null,
        detail_json: null,
      },
      {
        stat_code: "annualized_volatility",
        value: 0.2,
        std_err: null,
        ci_low: null,
        ci_high: null,
        detail_json: null,
      },
      {
        stat_code: "sharpe_ratio",
        value: 0.62,
        std_err: null,
        ci_low: null,
        ci_high: null,
        detail_json: null,
      },
      {
        stat_code: "information_ratio",
        value: 0.4,
        std_err: null,
        ci_low: null,
        ci_high: null,
        detail_json: null,
      },
      {
        stat_code: "max_drawdown",
        value: -0.08,
        std_err: null,
        ci_low: null,
        ci_high: null,
        detail_json: null,
      },
      {
        stat_code: "turnover",
        value: 0.15,
        std_err: null,
        ci_low: null,
        ci_high: null,
        detail_json: null,
      },
      {
        stat_code: "hit_rate",
        value: 0.55,
        std_err: null,
        ci_low: null,
        ci_high: null,
        detail_json: null,
      },
      {
        stat_code: "n_sessions",
        value: 250,
        std_err: null,
        ci_low: null,
        ci_high: null,
        detail_json: null,
      },
    ],
  };
}

export function tradesResponse() {
  return {
    data: [
      {
        trade_seq: 1,
        session_date: "2024-01-02",
        security_id: "SEC_1",
        side: "buy",
        quantity: 10.5,
        price: 95.25,
        notional: 1000,
        fee: 0.5,
        realized_pnl: null,
      },
      {
        trade_seq: 2,
        session_date: "2024-01-03",
        security_id: "SEC_2",
        side: "sell",
        quantity: 4,
        price: 50.1,
        notional: 200.4,
        fee: 0.1,
        realized_pnl: null,
      },
    ],
    pagination: { limit: 50, offset: 0, count: 2, total: 2 },
    truncated: false,
  };
}

export function monthlyResponse() {
  return {
    data: [
      { year: 2024, month: 1, month_return: 0.021, nav_end: 1.021 },
      { year: 2024, month: 2, month_return: -0.01, nav_end: 1.011 },
    ],
  };
}

export function attributionResponse() {
  return {
    data: [
      { dimension: "sector", group_key: "tech", contribution: 0.012 },
      { dimension: "analyst", group_key: "AN_001", contribution: 0.008 },
    ],
  };
}

export function benchmarkSummary(overrides: Record<string, unknown> = {}) {
  return {
    benchmark_id: BENCH_ID,
    kind: "custom",
    name: "Tech large-cap",
    status: "ready",
    preset: "custom",
    weighting: "cap_weighted",
    rebalance: "monthly",
    row_count: 250,
    ...overrides,
  };
}

export function benchmarkListResponse(items: unknown[] = [benchmarkSummary()]) {
  return {
    data: items,
    pagination: { limit: 200, offset: 0, count: items.length, total: items.length },
  };
}

export function benchmarkSeriesResponse() {
  return {
    data: [
      { session_date: "2024-01-02", open_level: 100, close_level: 100.5 },
      { session_date: "2024-01-03", open_level: 100.5, close_level: 101.2 },
    ],
    from_date: null,
    to_date: null,
    total: 2,
  };
}

export function sectorsResponse() {
  return {
    data: [
      {
        sector_id: "tech",
        name: "Technology",
        parent_sector_id: null,
        security_count: 10,
        analyst_count: 3,
        delisted_count: 0,
      },
    ],
    pagination: { limit: 500, offset: 0, count: 1, total: 1 },
  };
}

/** Routes needed by the backtests list page + builder. */
export function baseBacktestsRoutes(overrides: Record<string, unknown> = {}) {
  return {
    "GET /api/runs": {
      data: [
        {
          run_id: RUN_A,
          status: "succeeded",
          progress: 1,
          as_of_date: "2024-06-28",
          knowledge_cutoff: "2024-06-28T00:00:00Z",
          started_at: "2024-06-28T01:00:00Z",
          finished_at: "2024-06-28T01:05:00Z",
          methodology_fingerprint: "f".repeat(64),
        },
      ],
      pagination: { limit: 1, offset: 0, count: 1, total: 1 },
    },
    "GET /api/analysts": {
      items: [
        {
          analyst_id: "AN_001",
          name: "Alice Analyst",
          firm_id: "F1",
          peer_group: "tech-large",
          is_ranked: true,
          n_obs: 12,
          key_metrics: {},
        },
      ],
      total: 1,
      limit: 500,
      offset: 0,
      provenance: {},
    },
    "GET /api/sectors": sectorsResponse(),
    "GET /api/benchmarks": benchmarkListResponse([]),
    "GET /api/backtests": backtestListResponse([]),
    ...overrides,
  };
}
