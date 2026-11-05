import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "../../api/client";
import { queryKeys, type RunDetail, type Schemas } from "../../api/query";

export type RunSubmitResponse = Schemas["RunSubmitResponse"];
export type RunSummary = Schemas["RunSummary"];

/** Statuses during which the run detail endpoint should be polled. */
const ACTIVE_RUN_STATUSES = new Set(["queued", "running"]);

/** Poll interval (ms) while a run is queued/running. */
export const RUN_POLL_INTERVAL_MS = 1500;

export interface SubmitRunInput {
  as_of_date: string;
  knowledge_cutoff?: string | null;
  /** `"default"`, a stored 64-hex methodology fingerprint, or an inline object. */
  methodology: string;
}

/**
 * Run detail with live polling: refetches every {@link RUN_POLL_INTERVAL_MS}
 * while the run is `queued`/`running`, otherwise behaves like a plain query.
 * Shares the `queryKeys.runs.detail` cache entry with `useRun`.
 */
export function useRunLive(runId: string | undefined) {
  return useQuery({
    queryKey: queryKeys.runs.detail(runId ?? ""),
    queryFn: async (): Promise<RunDetail> => {
      const { data, error } = await api.GET("/api/runs/{run_id}", {
        params: { path: { run_id: runId ?? "" } },
      });
      if (error) throw error;
      if (data === undefined) throw new Error("Empty API response");
      return data;
    },
    enabled: Boolean(runId),
    refetchInterval: (query) => {
      const status = query.state.data?.status;
      return status !== undefined && ACTIVE_RUN_STATUSES.has(status) ? RUN_POLL_INTERVAL_MS : false;
    },
  });
}

/** `POST /api/runs` — submit a scoring run (202, idempotent server-side). */
export function useSubmitRun() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (input: SubmitRunInput): Promise<RunSubmitResponse> => {
      const { data, error } = await api.POST("/api/runs", {
        body: {
          as_of_date: input.as_of_date,
          knowledge_cutoff: input.knowledge_cutoff ?? null,
          methodology: input.methodology,
        },
      });
      if (error) throw error;
      if (data === undefined) throw new Error("Empty API response");
      return data;
    },
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["runs"] });
    },
  });
}

/** `POST /api/runs/{run_id}/retry` — requeue a failed/interrupted run. */
export function useRetryRun() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (runId: string): Promise<RunSummary> => {
      const { data, error } = await api.POST("/api/runs/{run_id}/retry", {
        params: { path: { run_id: runId } },
      });
      if (error) throw error;
      if (data === undefined) throw new Error("Empty API response");
      return data;
    },
    onSuccess: (_data, runId) => {
      void queryClient.invalidateQueries({ queryKey: queryKeys.runs.detail(runId) });
      void queryClient.invalidateQueries({ queryKey: ["runs"] });
      void queryClient.invalidateQueries({ queryKey: queryKeys.overview.get() });
    },
  });
}
