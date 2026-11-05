import { useQueries } from "@tanstack/react-query";
import { type FormEvent, useMemo, useState } from "react";
import { api } from "../../api/client";
import { shortRunId } from "../../api/query";
import { DataTable, type DataTableColumnDef } from "../../components/DataTable";
import { ErrorState } from "../../components/states";
import { SEED_LEAF_FIELDS } from "../methodologies/fields";
import { useRuns } from "../shared/queries";
import {
  runDetailQueryOptions,
  type ScenarioCompareResponse,
  type SweepJob,
  useSensitivitySweep,
} from "./api";

/** Server-side hard cap on sweep combinations. */
export const MAX_SWEEP_COMBOS = 25;

const SUGGESTED_PARAMS = SEED_LEAF_FIELDS.map((f) => f.path);

/** Parse a comma-separated values list into JSON scalars. */
export function parseSweepValues(raw: string): { values: unknown[]; error?: string } {
  const tokens = raw
    .split(",")
    .map((t) => t.trim())
    .filter((t) => t.length > 0);
  const values: unknown[] = [];
  for (const token of tokens) {
    if (token === "true") values.push(true);
    else if (token === "false") values.push(false);
    else if (token === "null") values.push(null);
    else if (/^-?\d+(\.\d+)?$/.test(token)) values.push(Number(token));
    else if (
      (token.startsWith('"') && token.endsWith('"')) ||
      (token.startsWith("'") && token.endsWith("'"))
    ) {
      values.push(token.slice(1, -1));
    } else values.push(token);
  }
  if (values.length === 0) return { values, error: "at least one value is required" };
  if (values.length > MAX_SWEEP_COMBOS) {
    return {
      values,
      error: `sweep is capped at ${MAX_SWEEP_COMBOS} combinations (got ${values.length})`,
    };
  }
  return { values };
}

/** Human label for one sweep job's spec (``{"param_path","value"}`` or overrides). */
export function specLabel(spec: Record<string, unknown>): string {
  if (typeof spec.param_path === "string") {
    return `${spec.param_path} = ${JSON.stringify(spec.value)}`;
  }
  if (spec.overrides && typeof spec.overrides === "object") {
    return `overrides ${JSON.stringify(spec.overrides)}`;
  }
  return JSON.stringify(spec);
}

interface SweepRow {
  job: SweepJob;
  runStatus?: string;
  runProgress?: number;
  meanScoreDelta?: number | null;
  meanAbsRankDelta?: number | null;
  spearman?: number | null;
}

function mean(nums: (number | null | undefined)[]): number | null {
  const xs = nums.filter((n): n is number => typeof n === "number" && Number.isFinite(n));
  if (xs.length === 0) return null;
  return xs.reduce((a, b) => a + b, 0) / xs.length;
}

function compareSummary(compare: ScenarioCompareResponse | undefined) {
  if (!compare) return { meanScoreDelta: null, meanAbsRankDelta: null, spearman: null };
  const analysts = compare.analysts ?? [];
  return {
    meanScoreDelta: mean(analysts.map((a) => a.score_delta)),
    meanAbsRankDelta: mean(
      analysts.map((a) => (a.rank_delta == null ? null : Math.abs(a.rank_delta))),
    ),
    spearman: compare.rank_correlation.spearman ?? null,
  };
}

/**
 * Sensitivity sweep panel: submit a bounded (<=25) parameter sweep on top of
 * the scenario's effective methodology, poll the spawned runs, and tabulate
 * per-value score/rank deltas vs a chosen reference run.
 */
export function SweepPanel({
  scenarioId,
  defaultAsOf,
}: {
  scenarioId: string;
  defaultAsOf?: string;
}) {
  const sweep = useSensitivitySweep();
  const runs = useRuns({ status: "succeeded", limit: 100, order: "desc" });
  const succeededRuns = runs.data?.data ?? [];

  const [paramPath, setParamPath] = useState("");
  const [valuesRaw, setValuesRaw] = useState("");
  const [asOf, setAsOf] = useState(defaultAsOf ?? "");
  const [referenceRun, setReferenceRun] = useState<string>("");
  const [formError, setFormError] = useState<string | null>(null);

  const jobs = useMemo(() => sweep.data?.jobs ?? [], [sweep.data]);

  // Poll every spawned run until terminal.
  const runQueries = useQueries({
    queries: jobs.map((job) => runDetailQueryOptions(job.run_id)),
  });
  const runById = new Map(runQueries.map((q, i) => [jobs[i]?.run_id, q.data]));

  // For finished runs, fetch the delta vs the reference run.
  const finishedJobs = jobs.filter((j) => runById.get(j.run_id)?.status === "succeeded");
  const compareQueries = useQueries({
    queries: finishedJobs.map((job) => ({
      queryKey: ["scenario-compare", { a: referenceRun, b: job.run_id }] as const,
      queryFn: async () => {
        const { data, error } = await api.GET("/api/scenarios/compare", {
          params: { query: { a: referenceRun, b: job.run_id } },
        });
        if (error) throw error;
        if (data === undefined) throw new Error("Empty API response");
        return data;
      },
      enabled: Boolean(referenceRun),
      retry: 1,
    })),
  });
  const compareByRun = new Map(
    finishedJobs.map((job, i) => [job.run_id, compareQueries[i]?.data] as const),
  );

  const effectiveAsOf = asOf || defaultAsOf || new Date().toISOString().slice(0, 10);
  const effectiveReference = referenceRun || succeededRuns[0]?.run_id || "";

  const onSubmit = (event: FormEvent) => {
    event.preventDefault();
    const { values, error } = parseSweepValues(valuesRaw);
    if (error) {
      setFormError(error);
      return;
    }
    setFormError(null);
    sweep.mutate({
      as_of_date: effectiveAsOf,
      scenario_id: scenarioId,
      param_path: paramPath.trim() || null,
      values,
    });
  };

  const rows: SweepRow[] = jobs.map((job) => {
    const run = runById.get(job.run_id);
    const cmp = compareByRun.get(job.run_id);
    const summary =
      run?.status === "succeeded"
        ? compareSummary(cmp)
        : { meanScoreDelta: null, meanAbsRankDelta: null, spearman: null };
    return {
      job,
      runStatus: run?.status ?? job.status,
      runProgress: run?.progress,
      ...summary,
    };
  });

  const columns: DataTableColumnDef<SweepRow>[] = [
    { accessorKey: "job.index", header: "#", cell: ({ row }) => row.original.job.index },
    {
      id: "spec",
      header: "Parameter value",
      cell: ({ row }) => <code className="text-xs">{specLabel(row.original.job.spec)}</code>,
    },
    {
      id: "run",
      header: "Run",
      cell: ({ row }) => (
        <span className="font-mono text-xs" title={row.original.job.run_id}>
          {shortRunId(row.original.job.run_id)} · {row.original.runStatus ?? "?"}
          {row.original.runProgress !== undefined && row.original.runStatus !== "succeeded"
            ? ` ${Math.round(row.original.runProgress * 100)}%`
            : ""}
        </span>
      ),
    },
    {
      id: "mean_score_delta",
      header: "Mean score Δ",
      cell: ({ row }) =>
        row.original.meanScoreDelta === null || row.original.meanScoreDelta === undefined
          ? "…"
          : row.original.meanScoreDelta.toFixed(4),
    },
    {
      id: "mean_abs_rank_delta",
      header: "Mean |rank Δ|",
      cell: ({ row }) =>
        row.original.meanAbsRankDelta === null || row.original.meanAbsRankDelta === undefined
          ? "…"
          : row.original.meanAbsRankDelta.toFixed(2),
    },
    {
      id: "spearman",
      header: "Spearman",
      cell: ({ row }) =>
        row.original.spearman === null || row.original.spearman === undefined
          ? "…"
          : row.original.spearman.toFixed(3),
    },
  ];

  return (
    <div className="flex flex-col gap-4" data-testid="sweep-panel">
      <form onSubmit={onSubmit} className="flex flex-col gap-3" data-testid="sweep-form">
        <div className="grid grid-cols-3 gap-3">
          <label className="flex flex-col gap-1 text-xs text-(--color-neutral-600)">
            Parameter path
            <input
              className="input font-mono text-xs"
              value={paramPath}
              onChange={(e) => setParamPath(e.target.value)}
              placeholder="family_weights.return_skill"
              list="sweep-param-paths"
              required
              aria-label="Sweep parameter path"
            />
            <datalist id="sweep-param-paths">
              {SUGGESTED_PARAMS.map((p) => (
                <option key={p} value={p} />
              ))}
            </datalist>
          </label>
          <label className="flex flex-col gap-1 text-xs text-(--color-neutral-600)">
            Values (comma-separated, max {MAX_SWEEP_COMBOS})
            <input
              className="input font-mono text-xs"
              value={valuesRaw}
              onChange={(e) => setValuesRaw(e.target.value)}
              placeholder="0.2, 0.4, 0.6"
              required
              aria-label="Sweep values"
            />
          </label>
          <label className="flex flex-col gap-1 text-xs text-(--color-neutral-600)">
            As of date
            <input
              type="date"
              className="input"
              value={effectiveAsOf}
              onChange={(e) => setAsOf(e.target.value)}
              required
              aria-label="Sweep as of date"
            />
          </label>
        </div>
        <p className="text-xs text-(--color-neutral-600)">
          Sweeps the scenario&apos;s effective methodology — a what-if only; the baseline run is
          untouched. Deltas below are vs the reference run (B − A).
        </p>
        {formError && (
          <p
            className="text-sm text-(--color-neutral-700)"
            role="alert"
            data-testid="sweep-form-error"
          >
            {formError}
          </p>
        )}
        {sweep.isError && <ErrorState error={sweep.error} title="Failed to submit sweep" />}
        <div className="flex items-center gap-3">
          <button type="submit" className="btn btn-primary" disabled={sweep.isPending}>
            {sweep.isPending ? "Submitting…" : "Run sweep"}
          </button>
          {sweep.data && (
            <span className="text-sm text-(--color-neutral-600)" data-testid="sweep-submitted">
              Submitted {sweep.data.count} combination{sweep.data.count === 1 ? "" : "s"} (sweep{" "}
              {sweep.data.sweep_id.slice(0, 20)}…)
            </span>
          )}
        </div>
      </form>

      {jobs.length > 0 && (
        <div className="flex flex-col gap-2">
          <label className="flex items-center gap-2 text-xs text-(--color-neutral-600)">
            Reference run for deltas
            <select
              className="input max-w-[320px]"
              value={effectiveReference}
              onChange={(e) => setReferenceRun(e.target.value)}
              aria-label="Sweep reference run"
            >
              {succeededRuns.map((r) => (
                <option key={r.run_id} value={r.run_id}>
                  {shortRunId(r.run_id)} · {r.as_of_date} · {r.status}
                </option>
              ))}
            </select>
          </label>
          <DataTable columns={columns} data={rows} caption="Sensitivity sweep results" />
        </div>
      )}
    </div>
  );
}
