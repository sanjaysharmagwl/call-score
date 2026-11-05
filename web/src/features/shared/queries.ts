import { useQuery } from "@tanstack/react-query";
import { api } from "../../api/client";
import type {
  AnalystListResponse,
  AnalystProfileResponse,
  ChangesResponse,
  LeaderboardResponse,
  MethodologyListResponse,
  MetricDrilldownResponse,
  OverviewResponse,
  RunDetail,
  RunListResponse,
  SectorListResponse,
  SectorPerformanceResponse,
  SecurityDetailResponse,
  SecurityListResponse,
} from "../../api/query";
import { queryKeys } from "../../api/query";

type Order = "asc" | "desc";

export interface RunsParams {
  status?: "queued" | "running" | "succeeded" | "failed" | "interrupted";
  as_of?: string;
  sort?: string;
  order?: Order;
  limit?: number;
  offset?: number;
}

export interface PageParams {
  sort?: string;
  order?: Order;
  limit?: number;
  offset?: number;
}

export interface LeaderboardParams extends PageParams {
  peer_group?: string;
  metric?: string;
  min_obs?: number;
}

export interface AnalystsParams extends PageParams {
  q?: string;
  peer_group?: string;
  eligible?: "ranked" | "unranked" | "all";
}

export interface SecuritiesParams extends PageParams {
  q?: string;
  sector?: string;
  cap_bucket?: string;
  as_of?: string;
}

export interface ChangesParams {
  run_id?: string;
  from?: string;
  to?: string;
  type?: string;
  limit?: number;
  offset?: number;
}

function useApiQuery<T>(key: readonly unknown[], queryFn: () => Promise<T>, enabled = true) {
  return useQuery({
    queryKey: key,
    queryFn,
    enabled,
    retry: 1,
  });
}

async function unwrap<T>(promise: Promise<{ data?: T; error?: unknown }>): Promise<T> {
  const { data, error } = await promise;
  if (error) throw error;
  if (data === undefined) throw new Error("Empty API response");
  return data;
}

export function useHealth() {
  return useApiQuery(queryKeys.health(), () =>
    unwrap<Record<string, string>>(api.GET("/api/health")),
  );
}

export function useRuns(params?: RunsParams, enabled = true) {
  return useApiQuery(
    queryKeys.runs.list(params as Record<string, unknown> | undefined),
    () =>
      unwrap<RunListResponse>(
        api.GET("/api/runs", {
          params: { query: { ...params } },
        }),
      ),
    enabled,
  );
}

export function useRun(runId: string | undefined) {
  return useApiQuery(
    queryKeys.runs.detail(runId ?? ""),
    () =>
      unwrap<RunDetail>(
        api.GET("/api/runs/{run_id}", {
          params: { path: { run_id: runId ?? "" } },
        }),
      ),
    Boolean(runId),
  );
}

export function useMethodologies() {
  return useApiQuery(queryKeys.methodologies.list(), () =>
    unwrap<MethodologyListResponse>(api.GET("/api/methodologies")),
  );
}

export function useAnalysts(params: AnalystsParams & { run_id: string }, enabled = true) {
  return useApiQuery(
    queryKeys.analysts.list(params as unknown as Record<string, unknown>),
    () =>
      unwrap<AnalystListResponse>(
        api.GET("/api/analysts", {
          params: {
            query: {
              run_id: params.run_id,
              q: params.q,
              peer_group: params.peer_group,
              eligible: params.eligible,
              sort: params.sort,
              order: params.order,
              limit: params.limit,
              offset: params.offset,
            },
          },
        }),
      ),
    enabled,
  );
}

export function useAnalyst(analystId: string, runId: string | undefined) {
  return useApiQuery(
    queryKeys.analysts.profile(analystId, runId),
    () =>
      unwrap<AnalystProfileResponse>(
        api.GET("/api/analysts/{analyst_id}", {
          params: {
            path: { analyst_id: analystId },
            query: { run_id: runId ?? "" },
          },
        }),
      ),
    Boolean(runId),
  );
}

export function useAnalystMetrics(analystId: string, params: { run_id: string; family?: string }) {
  return useApiQuery(
    queryKeys.analysts.metrics(analystId, params as Record<string, unknown>),
    () =>
      unwrap<MetricDrilldownResponse>(
        api.GET("/api/analysts/{analyst_id}/metrics", {
          params: {
            path: { analyst_id: analystId },
            query: { run_id: params.run_id, family: params.family },
          },
        }),
      ),
    Boolean(params.run_id),
  );
}

export function useLeaderboard(params: LeaderboardParams & { run_id: string }, enabled = true) {
  return useApiQuery(
    queryKeys.leaderboard.list(params as unknown as Record<string, unknown>),
    () =>
      unwrap<LeaderboardResponse>(
        api.GET("/api/leaderboard", {
          params: {
            query: {
              run_id: params.run_id,
              peer_group: params.peer_group,
              metric: params.metric,
              min_obs: params.min_obs,
              sort: params.sort,
              order: params.order,
              limit: params.limit,
              offset: params.offset,
            },
          },
        }),
      ),
    enabled,
  );
}

export function useSecurities(params?: SecuritiesParams) {
  return useApiQuery(queryKeys.securities.list(params as Record<string, unknown> | undefined), () =>
    unwrap<SecurityListResponse>(
      api.GET("/api/securities", {
        params: {
          query: {
            q: params?.q,
            sector: params?.sector,
            cap_bucket: params?.cap_bucket,
            as_of: params?.as_of,
            limit: params?.limit,
            offset: params?.offset,
          },
        },
      }),
    ),
  );
}

export function useSecurity(securityId: string) {
  return useApiQuery(queryKeys.securities.detail(securityId), () =>
    unwrap<SecurityDetailResponse>(
      api.GET("/api/securities/{security_id}", {
        params: { path: { security_id: securityId } },
      }),
    ),
  );
}

export function useSectors(params?: { as_of?: string; limit?: number; offset?: number }) {
  return useApiQuery(queryKeys.sectors.list(params as Record<string, unknown> | undefined), () =>
    unwrap<SectorListResponse>(
      api.GET("/api/sectors", {
        params: {
          query: { as_of: params?.as_of, limit: params?.limit, offset: params?.offset },
        },
      }),
    ),
  );
}

export function useSectorPerformance(sectorId: string, runId: string | undefined) {
  return useApiQuery(
    queryKeys.sectors.performance(sectorId, runId ?? ""),
    () =>
      unwrap<SectorPerformanceResponse>(
        api.GET("/api/sectors/{sector_id}/performance", {
          params: { path: { sector_id: sectorId }, query: { run_id: runId ?? "" } },
        }),
      ),
    Boolean(runId),
  );
}

export function useChanges(params?: ChangesParams) {
  return useApiQuery(queryKeys.changes.list(params as Record<string, unknown> | undefined), () =>
    unwrap<ChangesResponse>(
      api.GET("/api/changes", {
        params: {
          query: {
            run_id: params?.run_id,
            from: params?.from,
            to: params?.to,
            type: params?.type,
            limit: params?.limit,
            offset: params?.offset,
          },
        },
      }),
    ),
  );
}

export function useOverview(runId: string | undefined) {
  return useApiQuery(
    queryKeys.overview.get(runId),
    () =>
      unwrap<OverviewResponse>(
        api.GET("/api/overview", {
          params: { query: { run_id: runId } },
        }),
      ),
    true,
  );
}
