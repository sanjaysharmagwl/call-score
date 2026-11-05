import { shortRunId } from "../api/query";
import { useRuns } from "../features/shared/queries";
import { useActiveRun, useRunSelection } from "../features/shared/run-state";
import { EmptyState, LoadingState } from "./states";

/**
 * Header run selector: lists calc runs (newest first) and writes the choice
 * to the `run_id` URL search param. When the URL has no `run_id`, the newest
 * succeeded run is selected automatically by `useActiveRun`.
 */
export function RunSelector() {
  const { runId, setRunId } = useRunSelection();
  const { runId: activeRunId } = useActiveRun();
  const runsQuery = useRuns({ limit: 100, order: "desc" });
  const runs = runsQuery.data?.data ?? [];
  const value = runId ?? activeRunId ?? "";

  if (runsQuery.isPending) {
    return <LoadingState label="Loading runs" />;
  }

  if (runsQuery.isError) {
    return (
      <span className="text-xs text-(--color-neutral-600)" title="Runs API error">
        runs unavailable
      </span>
    );
  }

  if (runs.length === 0) {
    return (
      <EmptyState
        title="No calc runs"
        description={
          <span>
            No scoring runs yet. Submit one via <code className="text-xs">POST /api/runs</code> or
            seed the database and run the engine (<code className="text-xs">just seed</code>).
          </span>
        }
      />
    );
  }

  return (
    <label className="flex items-center gap-2 text-xs text-(--color-neutral-600)">
      <span>Run</span>
      <select
        aria-label="Active calc run"
        className="input max-w-[260px] py-1 text-xs"
        value={value}
        onChange={(e) => setRunId(e.target.value || undefined)}
      >
        {value === "" && <option value="">— select run —</option>}
        {runs.map((run) => (
          <option key={run.run_id} value={run.run_id}>
            {shortRunId(run.run_id)} · {run.as_of_date} · {run.status}
            {run.methodology_fingerprint ? ` · ${run.methodology_fingerprint.slice(0, 8)}` : ""}
          </option>
        ))}
        {/* Keep the current run selectable even if it fell off the first page. */}
        {value !== "" && !runs.some((r) => r.run_id === value) && (
          <option value={value}>{shortRunId(value)} · (current)</option>
        )}
      </select>
    </label>
  );
}
