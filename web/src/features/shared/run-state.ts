import { useQueryClient } from "@tanstack/react-query";
import { useNavigate, useSearch } from "@tanstack/react-router";
import { useCallback, useEffect, useRef } from "react";
import { runIdOfQueryKey } from "../../api/query";
import { useRuns } from "./queries";

interface RunSearch {
  run_id?: string;
}

/**
 * Read/write the active `run_id` search param. The param lives on the root
 * route's `validateSearch`, so it survives navigation, refresh, and deep
 * links. `setRunId` preserves any other search params.
 */
export function useRunSelection() {
  const search = useSearch({ strict: false }) as RunSearch;
  const navigate = useNavigate();
  const runId = typeof search.run_id === "string" && search.run_id ? search.run_id : undefined;

  const setRunId = useCallback(
    (id: string | undefined, opts?: { replace?: boolean }) => {
      void navigate({
        to: ".",
        search: (prev: Record<string, unknown>) => ({
          ...prev,
          run_id: id && id.length > 0 ? id : undefined,
        }),
        replace: opts?.replace ?? false,
      });
    },
    [navigate],
  );

  return { runId, setRunId };
}

/**
 * Active-run state for the shell: the URL `run_id` when present, otherwise
 * the newest `succeeded` run (which is written back into the URL so deep
 * links and refresh stay stable). Returns `undefined` while runs are still
 * loading or when no succeeded run exists.
 */
export function useActiveRun() {
  const { runId, setRunId } = useRunSelection();
  const latestRuns = useRuns({ status: "succeeded", limit: 1, order: "desc" }, !runId);
  const latest = latestRuns.data?.data?.[0];

  useEffect(() => {
    if (!runId && latest) {
      setRunId(latest.run_id, { replace: true });
    }
  }, [runId, latest, setRunId]);

  return {
    runId: runId ?? latest?.run_id,
    isDefaulting: !runId && latestRuns.isPending,
    runs: latestRuns,
    setRunId,
  };
}

/**
 * Evict cached run-scoped queries that belong to a different run. Called by
 * the shell whenever the active run changes; keys without a `run_id` (meta,
 * runs list, securities list, ...) are left untouched.
 */
export function useClearStaleRunQueries(runId: string | undefined) {
  const queryClient = useQueryClient();
  const prevRef = useRef<string | undefined>(undefined);

  useEffect(() => {
    const prev = prevRef.current;
    prevRef.current = runId;
    if (prev === runId) return;
    queryClient.removeQueries({
      predicate: (query) => {
        const scopedRunId = runIdOfQueryKey(query.queryKey);
        return scopedRunId !== undefined && scopedRunId !== runId;
      },
    });
  }, [runId, queryClient]);
}
