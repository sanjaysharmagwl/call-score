import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "../../api/client";
import type { Schemas } from "../../api/query";

export type BacktestSummary = Schemas["BacktestSummary"];
export type BacktestDetail = Schemas["BacktestDetail"];
export type BacktestListResponse = Schemas["BacktestListResponse"];
export type BacktestCreateRequest = Schemas["BacktestCreateRequest"];
export type BacktestSubmitResponse = Schemas["BacktestSubmitResponse"];
export type BacktestNavResponse = Schemas["BacktestNavResponse"];
export type BacktestNavRow = Schemas["BacktestNavRow"];
export type BacktestStatsResponse = Schemas["BacktestStatsResponse"];
export type BacktestStatRow = Schemas["BacktestStatRow"];
export type BacktestTradesResponse = Schemas["BacktestTradesResponse"];
export type BacktestTradeRow = Schemas["BacktestTradeRow"];
export type BacktestAttributionResponse = Schemas["BacktestAttributionResponse"];
export type BacktestAttributionRow = Schemas["BacktestAttributionRow"];
export type BacktestMonthlyResponse = Schemas["BacktestMonthlyResponse"];
export type BacktestMonthlyRow = Schemas["BacktestMonthlyRow"];
export type BenchmarkSummary = Schemas["BenchmarkSummary"];
export type BenchmarkListResponse = Schemas["BenchmarkListResponse"];
export type BenchmarkDetail = Schemas["BenchmarkDetail"];
export type BenchmarkCreateRequest = Schemas["BenchmarkCreateRequest"];
export type BenchmarkSubmitResponse = Schemas["BenchmarkSubmitResponse"];
export type BenchmarkMaterializeResponse = Schemas["BenchmarkMaterializeResponse"];
export type BenchmarkSeriesResponse = Schemas["BenchmarkSeriesResponse"];

/** Backtest lifecycle mirrors run statuses (queued -> running -> terminal). */
const ACTIVE_BACKTEST_STATUSES = new Set(["queued", "running"]);
export const BACKTEST_POLL_INTERVAL_MS = 1500;

const keys = {
  list: (params?: Record<string, unknown>) => ["backtests", params ?? {}] as const,
  detail: (backtestId: string) => ["backtest", { backtest_id: backtestId }] as const,
  nav: (backtestId: string, params?: Record<string, unknown>) =>
    ["backtest-nav", { backtest_id: backtestId, ...(params ?? {}) }] as const,
  stats: (backtestId: string) => ["backtest-stats", { backtest_id: backtestId }] as const,
  trades: (backtestId: string, params?: Record<string, unknown>) =>
    ["backtest-trades", { backtest_id: backtestId, ...(params ?? {}) }] as const,
  attribution: (backtestId: string) =>
    ["backtest-attribution", { backtest_id: backtestId }] as const,
  monthly: (backtestId: string) => ["backtest-monthly", { backtest_id: backtestId }] as const,
  benchmarks: (params?: Record<string, unknown>) => ["benchmarks", params ?? {}] as const,
  benchmark: (benchmarkId: string) => ["benchmark", { benchmark_id: benchmarkId }] as const,
  benchmarkSeries: (benchmarkId: string, params?: Record<string, unknown>) =>
    ["benchmark-series", { benchmark_id: benchmarkId, ...(params ?? {}) }] as const,
};

async function unwrap<T>(promise: Promise<{ data?: T; error?: unknown }>): Promise<T> {
  const { data, error } = await promise;
  if (error) throw error;
  if (data === undefined) throw new Error("Empty API response");
  return data;
}

/** Best-effort parse of an error response body (already-consumed safe). */
async function readErrorBody(response: Response | undefined): Promise<unknown> {
  if (!response) return null;
  try {
    return await response.clone().json();
  } catch {
    return null;
  }
}

/** `GET /api/backtests` — list backtests newest first, optional status filter. */
export function useBacktests(params?: {
  status?: "queued" | "running" | "succeeded" | "failed" | "interrupted";
  limit?: number;
  offset?: number;
}) {
  return useQuery({
    queryKey: keys.list(params as Record<string, unknown> | undefined),
    queryFn: () =>
      unwrap<BacktestListResponse>(
        api.GET("/api/backtests", {
          params: {
            query: {
              status: params?.status,
              limit: params?.limit,
              offset: params?.offset,
            },
          },
        }),
      ),
    retry: 1,
  });
}

/** Raw `GET /api/backtests/{id}` fetch (shared by the hook and list cells). */
export async function fetchBacktestDetail(backtestId: string): Promise<BacktestDetail> {
  return unwrap<BacktestDetail>(
    api.GET("/api/backtests/{backtest_id}", {
      params: { path: { backtest_id: backtestId } },
    }),
  );
}

/**
 * `GET /api/backtests/{id}` — detail, polling while queued/running (the
 * T17/T22 run-state pattern: status transitions drive `refetchInterval`).
 */
export function useBacktest(backtestId: string | undefined) {
  return useQuery({
    queryKey: keys.detail(backtestId ?? ""),
    queryFn: () => fetchBacktestDetail(backtestId ?? ""),
    enabled: Boolean(backtestId),
    retry: 1,
    refetchInterval: (query) => {
      const status = query.state.data?.status;
      return status !== undefined && ACTIVE_BACKTEST_STATUSES.has(status)
        ? BACKTEST_POLL_INTERVAL_MS
        : false;
    },
  });
}

export type NavInterval = "1d" | "1w" | "1M";

export interface NavParams {
  from?: string;
  to?: string;
  interval?: NavInterval;
  limit?: number;
  offset?: number;
}

/** `GET /api/backtests/{id}/nav` — downsampled NAV series. */
export function useBacktestNav(backtestId: string | undefined, params?: NavParams) {
  return useQuery({
    queryKey: keys.nav(backtestId ?? "", params as Record<string, unknown> | undefined),
    queryFn: () =>
      unwrap<BacktestNavResponse>(
        api.GET("/api/backtests/{backtest_id}/nav", {
          params: {
            path: { backtest_id: backtestId ?? "" },
            query: {
              from: params?.from,
              to: params?.to,
              interval: params?.interval,
              limit: params?.limit,
              offset: params?.offset,
            },
          },
        }),
      ),
    enabled: Boolean(backtestId),
    retry: 1,
  });
}

/** `GET /api/backtests/{id}/stats` — scalar stats (empty until succeeded). */
export function useBacktestStats(backtestId: string | undefined, enabled = true) {
  return useQuery({
    queryKey: keys.stats(backtestId ?? ""),
    queryFn: () =>
      unwrap<BacktestStatsResponse>(
        api.GET("/api/backtests/{backtest_id}/stats", {
          params: { path: { backtest_id: backtestId ?? "" } },
        }),
      ),
    enabled: Boolean(backtestId) && enabled,
    retry: 1,
  });
}

export interface TradesParams {
  limit?: number;
  offset?: number;
}

/** `GET /api/backtests/{id}/trades` — paginated trade rows (JSON). */
export function useBacktestTrades(backtestId: string | undefined, params?: TradesParams) {
  return useQuery({
    queryKey: keys.trades(backtestId ?? "", params as Record<string, unknown> | undefined),
    queryFn: () =>
      unwrap<BacktestTradesResponse>(
        api.GET("/api/backtests/{backtest_id}/trades", {
          params: {
            path: { backtest_id: backtestId ?? "" },
            query: { limit: params?.limit, offset: params?.offset },
          },
        }),
      ),
    enabled: Boolean(backtestId),
    retry: 1,
  });
}

/** `GET /api/backtests/{id}/attribution` — contributions by dimension. */
export function useBacktestAttribution(backtestId: string | undefined, enabled = true) {
  return useQuery({
    queryKey: keys.attribution(backtestId ?? ""),
    queryFn: () =>
      unwrap<BacktestAttributionResponse>(
        api.GET("/api/backtests/{backtest_id}/attribution", {
          params: { path: { backtest_id: backtestId ?? "" } },
        }),
      ),
    enabled: Boolean(backtestId) && enabled,
    retry: 1,
  });
}

/** `GET /api/backtests/{id}/monthly` — monthly compounded returns. */
export function useBacktestMonthly(backtestId: string | undefined, enabled = true) {
  return useQuery({
    queryKey: keys.monthly(backtestId ?? ""),
    queryFn: () =>
      unwrap<BacktestMonthlyResponse>(
        api.GET("/api/backtests/{backtest_id}/monthly", {
          params: { path: { backtest_id: backtestId ?? "" } },
        }),
      ),
    enabled: Boolean(backtestId) && enabled,
    retry: 1,
  });
}

/** `POST /api/backtests` — submit a backtest (202, queued). */
export function useSubmitBacktest() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (input: BacktestCreateRequest): Promise<BacktestSubmitResponse> => {
      const { data, error, response } = await api.POST("/api/backtests", { body: input });
      if (error || data === undefined) {
        // openapi-fetch already parses JSON error bodies into `error`; try the
        // raw response as a fallback for non-JSON error payloads.
        const body = await readErrorBody(response);
        throw body ?? error ?? new Error("Empty API response");
      }
      return data;
    },
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["backtests"] });
    },
  });
}

/** `GET /api/benchmarks` — custom benchmark definitions. */
export function useBenchmarks(params?: { status?: string; limit?: number; offset?: number }) {
  return useQuery({
    queryKey: keys.benchmarks(params as Record<string, unknown> | undefined),
    queryFn: () =>
      unwrap<BenchmarkListResponse>(
        api.GET("/api/benchmarks", {
          params: {
            query: {
              status: params?.status,
              limit: params?.limit,
              offset: params?.offset,
            },
          },
        }),
      ),
    retry: 1,
  });
}

/** `GET /api/benchmarks/{id}` — detail incl. spec + materialization info. */
export function useBenchmark(benchmarkId: string | undefined) {
  return useQuery({
    queryKey: keys.benchmark(benchmarkId ?? ""),
    queryFn: () =>
      unwrap<BenchmarkDetail>(
        api.GET("/api/benchmarks/{benchmark_id}", {
          params: { path: { benchmark_id: benchmarkId ?? "" } },
        }),
      ),
    enabled: Boolean(benchmarkId),
    retry: 1,
  });
}

/** `POST /api/benchmarks` — create a custom benchmark def (201, status draft). */
export function useCreateBenchmark() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (input: BenchmarkCreateRequest): Promise<BenchmarkSubmitResponse> => {
      const { data, error, response } = await api.POST("/api/benchmarks", { body: input });
      if (error || data === undefined) {
        const body = await readErrorBody(response);
        throw body ?? error ?? new Error("Empty API response");
      }
      return data;
    },
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["benchmarks"] });
    },
  });
}

/** `POST /api/benchmarks/{id}/materialize` — synchronous rebuild (200). */
export function useMaterializeBenchmark() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (benchmarkId: string): Promise<BenchmarkMaterializeResponse> => {
      const { data, error, response } = await api.POST(
        "/api/benchmarks/{benchmark_id}/materialize",
        { params: { path: { benchmark_id: benchmarkId } } },
      );
      if (error || data === undefined) {
        const body = await readErrorBody(response);
        throw body ?? error ?? new Error("Empty API response");
      }
      return data;
    },
    onSuccess: (_data, benchmarkId) => {
      void queryClient.invalidateQueries({ queryKey: ["benchmarks"] });
      void queryClient.invalidateQueries({ queryKey: keys.benchmark(benchmarkId) });
      void queryClient.invalidateQueries({ queryKey: ["benchmark-series"] });
    },
  });
}

export interface BenchmarkSeriesParams {
  from?: string;
  to?: string;
}

/** `GET /api/benchmarks/{id}/series` — materialized levels for preview charts. */
export function useBenchmarkSeries(
  benchmarkId: string | undefined,
  params?: BenchmarkSeriesParams,
) {
  return useQuery({
    queryKey: keys.benchmarkSeries(
      benchmarkId ?? "",
      params as Record<string, unknown> | undefined,
    ),
    queryFn: () =>
      unwrap<BenchmarkSeriesResponse>(
        api.GET("/api/benchmarks/{benchmark_id}/series", {
          params: {
            path: { benchmark_id: benchmarkId ?? "" },
            query: { from: params?.from, to: params?.to },
          },
        }),
      ),
    enabled: Boolean(benchmarkId),
    retry: 1,
  });
}
