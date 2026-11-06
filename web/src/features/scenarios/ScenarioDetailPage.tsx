import { Link } from "@tanstack/react-router";
import { type FormEvent, useMemo, useState } from "react";
import { shortFingerprint, shortRunId } from "../../api/query";
import { ErrorState, LoadingState } from "../../components/states";
import { type JsonObject, summarizeOverrides } from "../methodologies/fields";
import { MethodologyParamsView } from "../methodologies/MethodologyParamsView";
import { useRuns } from "../shared/queries";
import { useExecuteScenario, useRunLive, useScenario, useScenarioCompare } from "./api";
import { ComparePanel } from "./ComparePanel";
import { SweepPanel } from "./SweepPanel";

export type ScenarioTab = "config" | "compare" | "sweep";

const TABS: { id: ScenarioTab; label: string }[] = [
  { id: "config", label: "Config & runs" },
  { id: "compare", label: "Compare" },
  { id: "sweep", label: "Sensitivity sweep" },
];

const ACTIVE = new Set(["queued", "running"]);
const TERMINAL_FAILURE = new Set(["failed", "interrupted"]);

function todayIso(): string {
  return new Date().toISOString().slice(0, 10);
}

function statusTag(status: string) {
  const cls =
    status === "succeeded"
      ? "tag tag-accent"
      : TERMINAL_FAILURE.has(status)
        ? "tag tag-neutral"
        : "tag tag-accent-2";
  return <span className={cls}>{status}</span>;
}

/** Polls a just-executed what-if run until it reaches a terminal state. */
function TrackedRun({ runId }: { runId: string }) {
  const query = useRunLive(runId);
  const run = query.data;
  const rowCounts = run?.row_counts ?? {};
  const rowTotal = Object.values(rowCounts).reduce((a, b) => a + b, 0);
  return (
    <div
      className="flex flex-col gap-2 border border-(--color-divider) bg-(--color-neutral-100) p-3"
      data-testid="tracked-run"
    >
      <p className="text-sm">
        What-if run <span className="font-mono text-xs">{shortRunId(runId)}</span>:{" "}
        {run ? statusTag(run.status) : "loading…"}
        {run && ACTIVE.has(run.status) && (
          <span className="ml-2 text-xs text-(--color-neutral-600)">
            {Math.round(Math.min(1, Math.max(0, run.progress)) * 100)}%
          </span>
        )}
      </p>
      {run?.error && (
        <p className="text-sm text-(--color-neutral-700)">
          <span className="font-mono text-xs">{run.error.type}:</span> {run.error.message}
        </p>
      )}
      {run?.status === "succeeded" && (
        <p className="text-xs text-(--color-neutral-600)" data-testid="tracked-run-rows">
          Finished — {rowTotal.toLocaleString()} rows across {Object.keys(rowCounts).length} tables.
          Results are labelled what-if; the baseline run is unchanged.
        </p>
      )}
    </div>
  );
}

function ExecutePanel({
  scenarioId,
  defaultAsOf,
  onExecuted,
}: {
  scenarioId: string;
  defaultAsOf: string;
  onExecuted: () => void;
}) {
  const execute = useExecuteScenario();
  const [asOf, setAsOf] = useState("");
  const [cutoff, setCutoff] = useState("");
  const [trackingId, setTrackingId] = useState<string | null>(null);
  const effectiveAsOf = asOf || defaultAsOf || todayIso();

  const onSubmit = (event: FormEvent) => {
    event.preventDefault();
    execute.mutate(
      {
        scenario_id: scenarioId,
        as_of_date: effectiveAsOf,
        knowledge_cutoff: cutoff ? new Date(cutoff).toISOString() : null,
      },
      {
        onSuccess: (res) => {
          setTrackingId(res.run_id);
          onExecuted();
        },
      },
    );
  };

  const invalidation = execute.data?.invalidation as
    | { stages?: { stage?: string; fingerprint_changes?: boolean }[] }
    | undefined;

  return (
    <section className="card elev-sm" data-testid="execute-panel">
      <h3 className="card-title">Execute what-if run</h3>
      <p className="card-body">
        Runs the scenario&apos;s effective methodology (base + overrides) as a separate calc run. It
        never mutates the baseline.
      </p>
      {trackingId ? (
        <TrackedRun runId={trackingId} />
      ) : (
        <form onSubmit={onSubmit} className="flex flex-col gap-3">
          <div className="grid grid-cols-2 gap-3">
            <label className="flex flex-col gap-1 text-xs text-(--color-neutral-600)">
              As of date
              <input
                type="date"
                className="input"
                value={effectiveAsOf}
                onChange={(e) => setAsOf(e.target.value)}
                required
                aria-label="Execute as of date"
              />
            </label>
            <label className="flex flex-col gap-1 text-xs text-(--color-neutral-600)">
              Knowledge cutoff (optional)
              <input
                type="datetime-local"
                className="input"
                value={cutoff}
                onChange={(e) => setCutoff(e.target.value)}
              />
            </label>
          </div>
          {execute.isError && <ErrorState error={execute.error} title="Failed to execute" />}
          <div>
            <button type="submit" className="btn btn-primary" disabled={execute.isPending}>
              {execute.isPending ? "Submitting…" : "Execute scenario"}
            </button>
          </div>
        </form>
      )}
      {execute.data && !trackingId && invalidation?.stages && (
        <p className="text-xs text-(--color-neutral-600)" data-testid="invalidation-report">
          Invalidation: {invalidation.stages.length} stage
          {invalidation.stages.length === 1 ? "" : "s"} checked
          {invalidation.stages.some((s) => s.fingerprint_changes)
            ? " — downstream fingerprints change"
            : " — no downstream fingerprint changes"}
          .
        </p>
      )}
    </section>
  );
}

function RunHistory({ runIds }: { runIds: string[] }) {
  const runs = useRuns({ limit: 500, order: "desc" });
  const byId = new Map((runs.data?.data ?? []).map((r) => [r.run_id, r]));
  const ordered = [...runIds].reverse().slice(0, 25);
  if (ordered.length === 0) {
    return <p className="text-sm text-(--color-neutral-600)">No executions yet.</p>;
  }
  return (
    <ul className="flex flex-col gap-1 text-sm" data-testid="run-history">
      {ordered.map((id) => {
        const run = byId.get(id);
        return (
          <li key={id} className="flex items-center gap-3">
            <span className="font-mono text-xs" title={id}>
              {shortRunId(id)}
            </span>
            {run ? (
              <>
                {statusTag(run.status)}
                <span className="text-xs text-(--color-neutral-600)">{run.as_of_date}</span>
                <span className="text-xs text-(--color-neutral-500)">
                  {run.methodology_fingerprint ? shortFingerprint(run.methodology_fingerprint) : ""}
                </span>
              </>
            ) : (
              <span className="text-xs text-(--color-neutral-500)">run not in recent list</span>
            )}
          </li>
        );
      })}
    </ul>
  );
}

function CompareTab({ runIds, lastRunId }: { runIds: string[]; lastRunId?: string | null }) {
  const runs = useRuns({ status: "succeeded", limit: 100, order: "desc" });
  const succeeded = runs.data?.data ?? [];
  const scenarioRuns = succeeded.filter((r) => runIds.includes(r.run_id));

  const [baselineRun, setBaselineRun] = useState("");
  const [whatIfRun, setWhatIfRun] = useState("");

  const effectiveBaseline = baselineRun || succeeded[0]?.run_id || "";
  const effectiveWhatIf =
    whatIfRun ||
    (lastRunId && runIds.includes(lastRunId) ? lastRunId : scenarioRuns[0]?.run_id) ||
    "";

  const compare = useScenarioCompare(effectiveBaseline || undefined, effectiveWhatIf || undefined);

  if (runIds.length === 0) {
    return (
      <p className="text-sm text-(--color-neutral-600)" data-testid="compare-empty">
        No what-if runs yet — execute the scenario first, then compare it against a baseline run.
      </p>
    );
  }

  return (
    <div className="flex flex-col gap-3" data-testid="compare-tab">
      <div className="grid grid-cols-2 gap-3">
        <label className="flex flex-col gap-1 text-xs text-(--color-neutral-600)">
          Baseline / reference run (A)
          <select
            className="input"
            value={effectiveBaseline}
            onChange={(e) => setBaselineRun(e.target.value)}
            aria-label="Baseline run"
          >
            {succeeded.map((r) => (
              <option key={r.run_id} value={r.run_id}>
                {shortRunId(r.run_id)} · {r.as_of_date}
              </option>
            ))}
          </select>
        </label>
        <label className="flex flex-col gap-1 text-xs text-(--color-neutral-600)">
          What-if run (B)
          <select
            className="input"
            value={effectiveWhatIf}
            onChange={(e) => setWhatIfRun(e.target.value)}
            aria-label="What-if run"
          >
            {scenarioRuns.map((r) => (
              <option key={r.run_id} value={r.run_id}>
                {shortRunId(r.run_id)} · {r.as_of_date} · what-if
              </option>
            ))}
            {/* Allow comparing against any succeeded run as the what-if side. */}
            {succeeded
              .filter((r) => !runIds.includes(r.run_id))
              .map((r) => (
                <option key={r.run_id} value={r.run_id}>
                  {shortRunId(r.run_id)} · {r.as_of_date}
                </option>
              ))}
          </select>
        </label>
      </div>
      <ComparePanel
        data={compare.data}
        isPending={compare.isPending}
        error={compare.error}
        onRetry={() => void compare.refetch()}
        aLabel="Baseline / reference run (A)"
        bLabel="What-if run (B)"
      />
    </div>
  );
}

function ConfigTab({
  scenarioId,
  baseParams,
  overrides,
  universeFilter,
  runIds,
  lastExecution,
  sweeps,
  baseRef,
  baseFingerprint,
  onExecuted,
  defaultAsOf,
}: {
  scenarioId: string;
  baseParams: JsonObject | null | undefined;
  overrides: JsonObject | undefined;
  universeFilter: string[] | null | undefined;
  runIds: string[];
  lastExecution: Record<string, unknown> | null | undefined;
  sweeps: Record<string, unknown>[] | undefined;
  baseRef: string | null | undefined;
  baseFingerprint: string | null | undefined;
  onExecuted: () => void;
  defaultAsOf: string;
}) {
  return (
    <div className="grid grid-cols-2 gap-4">
      <div className="flex flex-col gap-4">
        <ExecutePanel scenarioId={scenarioId} defaultAsOf={defaultAsOf} onExecuted={onExecuted} />
        <section className="card elev-sm">
          <h3 className="card-title">Configuration</h3>
          <dl className="grid grid-cols-[140px_1fr] gap-x-3 gap-y-1 text-sm">
            <dt className="text-(--color-neutral-500)">Base</dt>
            <dd>
              {baseRef ?? "default"}
              {baseFingerprint && (
                <span className="ml-2 font-mono text-xs" title={baseFingerprint}>
                  {shortFingerprint(baseFingerprint)}
                </span>
              )}
            </dd>
            <dt className="text-(--color-neutral-500)">Overrides</dt>
            <dd className="font-mono text-xs">{summarizeOverrides(overrides)}</dd>
            <dt className="text-(--color-neutral-500)">Universe</dt>
            <dd className="font-mono text-xs">
              {universeFilter && universeFilter.length > 0 ? universeFilter.join(", ") : "all"}
            </dd>
            {lastExecution && (
              <>
                <dt className="text-(--color-neutral-500)">Last execution</dt>
                <dd className="font-mono text-xs">
                  {String(lastExecution.as_of_date ?? "?")} · run{" "}
                  {shortRunId(String(lastExecution.run_id ?? ""))}
                </dd>
              </>
            )}
          </dl>
        </section>
        <section className="card elev-sm">
          <h3 className="card-title">Run history</h3>
          <RunHistory runIds={runIds} />
        </section>
        {sweeps && sweeps.length > 0 && (
          <section className="card elev-sm">
            <h3 className="card-title">Sweeps</h3>
            <ul className="flex flex-col gap-1 text-xs text-(--color-neutral-600)">
              {sweeps.slice(-5).map((s) => (
                <li key={JSON.stringify(s)} className="font-mono">
                  {JSON.stringify(s).slice(0, 160)}
                </li>
              ))}
            </ul>
          </section>
        )}
      </div>
      <section className="card elev-sm">
        <h3 className="card-title">Base methodology parameters</h3>
        <p className="text-xs text-(--color-neutral-600)">
          Resolved base the overrides apply to (read-only).
        </p>
        <MethodologyParamsView params={baseParams ?? null} />
      </section>
    </div>
  );
}

/**
 * Scenario detail (`/scenarios/$scenarioId`): config, what-if execution with
 * live run polling, run history, baseline-vs-what-if comparison, and a
 * bounded sensitivity sweep. Deep links restore scenario + tab via the URL.
 */
export function ScenarioDetailPage({
  scenarioId,
  tab,
  onTabChange,
}: {
  scenarioId: string;
  tab: ScenarioTab;
  onTabChange: (tab: ScenarioTab) => void;
}) {
  const scenario = useScenario(scenarioId);
  const latestRuns = useRuns({ status: "succeeded", limit: 1, order: "desc" });
  const defaultAsOf = latestRuns.data?.data?.[0]?.as_of_date ?? "";
  const data = scenario.data;

  const baseParams = useMemo(
    () => (data?.base_methodology ?? null) as JsonObject | null,
    [data?.base_methodology],
  );

  if (scenario.isPending) return <LoadingState label="Loading scenario" />;
  if (scenario.isError || !data) {
    return (
      <ErrorState error={scenario.error} onRetry={() => void scenario.refetch()} title="Scenario" />
    );
  }

  return (
    <div className="flex flex-col gap-4" data-testid="scenario-detail">
      <div className="flex items-start justify-between gap-4">
        <div>
          <h1 className="flex items-center gap-3 font-heading text-2xl font-semibold">
            {data.name}
            <span className="tag tag-accent-2" data-testid="whatif-tag">
              what-if
            </span>
          </h1>
          {data.description && (
            <p className="text-sm text-(--color-neutral-600)">{data.description}</p>
          )}
          <p className="mt-1 text-xs text-(--color-neutral-500)">
            Created {data.created_at.slice(0, 19).replace("T", " ")} · {data.run_count} run
            {data.run_count === 1 ? "" : "s"}
            {data.last_run_id && (
              <>
                {" "}
                · last run <span className="font-mono">{shortRunId(data.last_run_id)}</span>
              </>
            )}
          </p>
        </div>
        <Link
          to="/scenarios"
          search={(prev) => ({ run_id: prev.run_id })}
          className="btn btn-secondary text-sm"
        >
          ← All scenarios
        </Link>
      </div>

      <div className="flex gap-1 border-b border-(--color-divider)">
        {TABS.map((t) => (
          <button
            key={t.id}
            type="button"
            className={`px-3 py-2 text-sm ${
              tab === t.id
                ? "border-b-2 border-(--color-accent) font-medium text-(--color-accent)"
                : "text-(--color-neutral-600) hover:text-(--color-text)"
            }`}
            onClick={() => onTabChange(t.id)}
          >
            {t.label}
          </button>
        ))}
      </div>

      {tab === "config" && (
        <ConfigTab
          scenarioId={scenarioId}
          baseParams={baseParams}
          overrides={data.overrides as JsonObject | undefined}
          universeFilter={data.universe_filter}
          runIds={data.run_ids ?? []}
          lastExecution={data.last_execution as Record<string, unknown> | null | undefined}
          sweeps={data.sweeps}
          baseRef={data.base_ref}
          baseFingerprint={data.base_fingerprint}
          defaultAsOf={defaultAsOf}
          onExecuted={() => void scenario.refetch()}
        />
      )}
      {tab === "compare" && <CompareTab runIds={data.run_ids ?? []} lastRunId={data.last_run_id} />}
      {tab === "sweep" && <SweepPanel scenarioId={scenarioId} defaultAsOf={defaultAsOf} />}
    </div>
  );
}
