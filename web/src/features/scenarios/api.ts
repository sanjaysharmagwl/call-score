import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "../../api/client";
import type { Schemas } from "../../api/query";
import type { JsonObject, Methodology } from "../methodologies/fields";

export type ScenarioSummary = Schemas["ScenarioSummary"];
export type ScenarioDetail = Schemas["ScenarioDetail"];
export type ScenarioListResponse = Schemas["ScenarioListResponse"];
export type ScenarioCreateRequest = Schemas["ScenarioCreateRequest"];
export type ScenarioExecuteRequest = Schemas["ScenarioExecuteRequest"];
export type ScenarioExecuteResponse = Schemas["ScenarioExecuteResponse"];
export type ScenarioCompareResponse = Schemas["ScenarioCompareResponse"];
export type SensitivitySweepRequest = Schemas["SensitivitySweepRequest"];
export type SensitivitySweepResponse = Schemas["SensitivitySweepResponse"];
export type SweepJob = Schemas["SweepJob"];
export type RunDetail = Schemas["RunDetail"];
export type AnalystDelta = Schemas["AnalystDelta"];
export type FamilyDelta = Schemas["FamilyDelta"];
export type MetricDelta = Schemas["MetricDelta"];
export type UniverseComparison = Schemas["UniverseComparison"];
export type CompareSummary = Schemas["CompareSummary"];
export type RankCorrelation = Schemas["RankCorrelation"];

const ACTIVE_RUN_STATUSES = new Set(["queued", "running"]);
export const RUN_POLL_INTERVAL_MS = 1500;

const keys = {
  list: (params?: Record<string, unknown>) => ["scenarios", params ?? {}] as const,
  detail: (scenarioId: string) => ["scenario", { scenario_id: scenarioId }] as const,
  compare: (a: string, b: string) => ["scenario-compare", { a, b }] as const,
};

async function unwrap<T>(promise: Promise<{ data?: T; error?: unknown }>): Promise<T> {
  const { data, error } = await promise;
  if (error) throw error;
  if (data === undefined) throw new Error("Empty API response");
  return data;
}

async function fetchRunDetail(runId: string): Promise<RunDetail> {
  const { data, error } = await api.GET("/api/runs/{run_id}", {
    params: { path: { run_id: runId } },
  });
  if (error) throw error;
  if (data === undefined) throw new Error("Empty API response");
  return data;
}

/** Query options for run detail, polling while queued/running. */
export function runDetailQueryOptions(runId: string) {
  return {
    queryKey: ["run-live", runId] as const,
    queryFn: () => fetchRunDetail(runId),
    refetchInterval: (query: { state: { data?: RunDetail } }) => {
      const status = query.state.data?.status;
      return status !== undefined && ACTIVE_RUN_STATUSES.has(status) ? RUN_POLL_INTERVAL_MS : false;
    },
    retry: 1,
  };
}

/** Run detail with live polling while queued/running (mirrors overview's hook). */
export function useRunLive(runId: string | undefined) {
  return useQuery({
    ...runDetailQueryOptions(runId ?? ""),
    enabled: Boolean(runId),
  });
}

/** `GET /api/scenarios` — saved scenarios, newest first. */
export function useScenarios(params?: { limit?: number; offset?: number }) {
  return useQuery({
    queryKey: keys.list(params as Record<string, unknown> | undefined),
    queryFn: () =>
      unwrap<ScenarioListResponse>(
        api.GET("/api/scenarios", {
          params: { query: { limit: params?.limit, offset: params?.offset } },
        }),
      ),
    retry: 1,
  });
}

/** `GET /api/scenarios/{scenario_id}` — config + run/sweep history. */
export function useScenario(scenarioId: string | undefined) {
  return useQuery({
    queryKey: keys.detail(scenarioId ?? ""),
    queryFn: () =>
      unwrap<ScenarioDetail>(
        api.GET("/api/scenarios/{scenario_id}", {
          params: { path: { scenario_id: scenarioId ?? "" } },
        }),
      ),
    enabled: Boolean(scenarioId),
    retry: 1,
  });
}

/** `POST /api/scenarios` — create a named scenario. */
export function useCreateScenario() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (input: ScenarioCreateRequest): Promise<ScenarioDetail> => {
      const { data, error } = await api.POST("/api/scenarios", { body: input });
      if (error) throw error;
      if (data === undefined) throw new Error("Empty API response");
      return data;
    },
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["scenarios"] });
    },
  });
}

export interface ExecuteScenarioInput {
  scenario_id: string;
  as_of_date: string;
  knowledge_cutoff?: string | null;
  universe_filter?: string[] | null;
}

/** `POST /api/scenarios/{id}/execute` — queue a what-if run (202). */
export function useExecuteScenario() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (input: ExecuteScenarioInput): Promise<ScenarioExecuteResponse> => {
      const { data, error } = await api.POST("/api/scenarios/{scenario_id}/execute", {
        params: { path: { scenario_id: input.scenario_id } },
        body: {
          as_of_date: input.as_of_date,
          knowledge_cutoff: input.knowledge_cutoff ?? null,
          universe_filter: input.universe_filter ?? null,
        },
      });
      if (error) throw error;
      if (data === undefined) throw new Error("Empty API response");
      return data;
    },
    onSuccess: (_data, input) => {
      void queryClient.invalidateQueries({ queryKey: keys.detail(input.scenario_id) });
      void queryClient.invalidateQueries({ queryKey: ["scenarios"] });
    },
  });
}

/** `GET /api/scenarios/compare?a=&b=` — delta report between two runs. */
export function useScenarioCompare(a: string | undefined, b: string | undefined) {
  return useQuery({
    queryKey: keys.compare(a ?? "", b ?? ""),
    queryFn: () =>
      unwrap<ScenarioCompareResponse>(
        api.GET("/api/scenarios/compare", {
          params: { query: { a: a ?? "", b: b ?? "" } },
        }),
      ),
    enabled: Boolean(a && b),
    retry: 1,
  });
}

/** `GET /api/scenarios/{id}/compare?other_run_id=` — scenario run vs another run. */
export function useScenarioRunCompare(
  scenarioId: string | undefined,
  otherRunId: string | null | undefined,
) {
  return useQuery({
    queryKey: [
      "scenario-run-compare",
      { scenario_id: scenarioId ?? "", other_run_id: otherRunId ?? "" },
    ],
    queryFn: () =>
      unwrap<ScenarioCompareResponse>(
        api.GET("/api/scenarios/{scenario_id}/compare", {
          params: {
            path: { scenario_id: scenarioId ?? "" },
            query: { other_run_id: otherRunId ?? undefined },
          },
        }),
      ),
    enabled: Boolean(scenarioId),
    retry: 1,
  });
}

/** `POST /api/scenarios/sensitivity` — bounded (<=25) parameter sweep (202). */
export function useSensitivitySweep() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (input: SensitivitySweepRequest): Promise<SensitivitySweepResponse> => {
      const { data, error } = await api.POST("/api/scenarios/sensitivity", { body: input });
      if (error) throw error;
      if (data === undefined) throw new Error("Empty API response");
      return data;
    },
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["scenario"] });
    },
  });
}

export type { JsonObject, Methodology };
