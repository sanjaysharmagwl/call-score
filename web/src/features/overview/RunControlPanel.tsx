import { type FormEvent, useEffect, useState } from "react";
import type { RunDetail } from "../../api/query";
import { shortFingerprint, shortRunId } from "../../api/query";
import { MetricCard } from "../../components/MetricCard";
import { ErrorState, LoadingState } from "../../components/states";
import { useMethodologies } from "../shared/queries";
import { useRetryRun, useRunLive, useSubmitRun } from "./queries";

const TERMINAL_FAILURE = new Set(["failed", "interrupted"]);
const ACTIVE = new Set(["queued", "running"]);

function todayIso(): string {
  return new Date().toISOString().slice(0, 10);
}

function statusTagClass(status: string): string {
  if (status === "succeeded") return "tag tag-accent";
  if (TERMINAL_FAILURE.has(status)) return "tag tag-neutral";
  return "tag tag-accent-2";
}

function ProgressBar({ progress }: { progress: number }) {
  const pct = Math.round(Math.min(1, Math.max(0, progress)) * 100);
  return (
    <div className="flex items-center gap-2">
      <div
        role="progressbar"
        aria-valuenow={pct}
        aria-valuemin={0}
        aria-valuemax={100}
        aria-label="Run progress"
        className="h-2 w-48 overflow-hidden bg-(--color-neutral-200)"
      >
        <div className="h-full bg-(--color-accent)" style={{ width: `${pct}%` }} />
      </div>
      <span className="text-xs text-(--color-neutral-600)">{pct}%</span>
    </div>
  );
}

function RunFacts({ run }: { run: RunDetail }) {
  const rowCounts = run.row_counts ?? {};
  const rowTotal = Object.values(rowCounts).reduce((a, b) => a + b, 0);
  return (
    <dl className="grid grid-cols-[auto_1fr] gap-x-4 gap-y-1 text-sm">
      <dt className="text-(--color-neutral-500)">Run</dt>
      <dd className="font-mono text-xs" title={run.run_id}>
        {run.run_id ? shortRunId(run.run_id) : "—"}
      </dd>
      <dt className="text-(--color-neutral-500)">As of</dt>
      <dd>{run.as_of_date}</dd>
      <dt className="text-(--color-neutral-500)">Cutoff</dt>
      <dd>{run.knowledge_cutoff ? run.knowledge_cutoff.slice(0, 16).replace("T", " ") : "—"}</dd>
      <dt className="text-(--color-neutral-500)">Methodology</dt>
      <dd className="font-mono text-xs" title={run.methodology_fingerprint ?? undefined}>
        {run.methodology_fingerprint ? shortFingerprint(run.methodology_fingerprint) : "default"}
      </dd>
      <dt className="text-(--color-neutral-500)">Source</dt>
      <dd className="font-mono text-xs" title={run.source_fingerprint ?? undefined}>
        {run.source_fingerprint ? shortFingerprint(run.source_fingerprint) : "—"}
      </dd>
      <dt className="text-(--color-neutral-500)">Algorithm</dt>
      <dd>{run.algorithm_version || "—"}</dd>
      <dt className="text-(--color-neutral-500)">Rows</dt>
      <dd>
        {rowTotal > 0
          ? `${rowTotal.toLocaleString()} across ${Object.keys(rowCounts).length} tables`
          : "—"}
      </dd>
    </dl>
  );
}

function RetryButton({ runId }: { runId: string }) {
  const retry = useRetryRun();
  return (
    <button
      type="button"
      className="btn btn-secondary"
      disabled={retry.isPending}
      onClick={() => retry.mutate(runId)}
    >
      {retry.isPending ? "Retrying…" : "Retry run"}
    </button>
  );
}

function FailurePanel({ run }: { run: RunDetail }) {
  return (
    <div className="flex flex-col gap-2 border border-(--color-divider) bg-(--color-neutral-100) p-3">
      <p className="text-sm font-medium text-(--color-neutral-800)">
        Run {shortRunId(run.run_id)} {run.status}
      </p>
      {run.error && (
        <p className="text-sm text-(--color-neutral-700)" data-testid="run-error">
          <span className="font-mono text-xs">{run.error.type}:</span> {run.error.message}
        </p>
      )}
      <div>
        <RetryButton runId={run.run_id} />
      </div>
    </div>
  );
}

/** Status block for the currently selected run (from the header selector). */
function ActiveRunStatus({ runId }: { runId: string }) {
  const query = useRunLive(runId);

  if (query.isPending) return <LoadingState label="Loading run" />;
  if (query.isError || !query.data) {
    return <ErrorState error={query.error} onRetry={() => void query.refetch()} title="Run" />;
  }
  const run = query.data;
  return (
    <div className="flex flex-col gap-2">
      <div className="flex items-center gap-3">
        <span className={statusTagClass(run.status)} data-testid="run-status">
          {run.status}
        </span>
        {ACTIVE.has(run.status) && <ProgressBar progress={run.progress} />}
      </div>
      {TERMINAL_FAILURE.has(run.status) ? <FailurePanel run={run} /> : <RunFacts run={run} />}
    </div>
  );
}

/** Polls a freshly submitted run until it reaches a terminal state. */
function TrackedRun({
  runId,
  onSucceeded,
}: {
  runId: string;
  onSucceeded: (runId: string) => void;
}) {
  const query = useRunLive(runId);
  const run = query.data;

  useEffect(() => {
    if (run?.status === "succeeded") onSucceeded(runId);
  }, [run?.status, runId, onSucceeded]);

  if (query.isPending || !run) {
    return <LoadingState label="Loading submitted run" />;
  }

  return (
    <div className="flex flex-col gap-2">
      <p className="text-sm" data-testid="tracked-run">
        Submitted run <span className="font-mono text-xs">{shortRunId(runId)}</span>:{" "}
        <span className={statusTagClass(run.status)}>{run.status}</span>
      </p>
      {ACTIVE.has(run.status) && <ProgressBar progress={run.progress} />}
      {TERMINAL_FAILURE.has(run.status) && <FailurePanel run={run} />}
      {run.status === "succeeded" && (
        <p className="text-sm text-(--color-neutral-700)">Run succeeded — switching to it…</p>
      )}
    </div>
  );
}

function NewRunForm({
  defaultAsOf,
  onSubmitted,
}: {
  defaultAsOf: string;
  onSubmitted: (runId: string) => void;
}) {
  const methodologies = useMethodologies();
  const submit = useSubmitRun();
  const [asOf, setAsOf] = useState("");
  const [cutoff, setCutoff] = useState("");
  const [methodology, setMethodology] = useState("default");

  const effectiveAsOf = asOf || defaultAsOf || todayIso();
  const versions = methodologies.data?.data ?? [];

  const onSubmit = (event: FormEvent) => {
    event.preventDefault();
    submit.mutate(
      {
        as_of_date: effectiveAsOf,
        knowledge_cutoff: cutoff ? new Date(cutoff).toISOString() : null,
        methodology,
      },
      { onSuccess: (res) => onSubmitted(res.run_id) },
    );
  };

  return (
    <form onSubmit={onSubmit} className="flex flex-col gap-3" data-testid="new-run-form">
      <div className="grid grid-cols-3 gap-3">
        <label className="flex flex-col gap-1 text-xs text-(--color-neutral-600)">
          As of date
          <input
            type="date"
            className="input"
            value={effectiveAsOf}
            onChange={(e) => setAsOf(e.target.value)}
            required
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
        <label className="flex flex-col gap-1 text-xs text-(--color-neutral-600)">
          Methodology
          <select
            className="input"
            value={methodology}
            onChange={(e) => setMethodology(e.target.value)}
            aria-label="Methodology"
          >
            <option value="default">Default (built-in preset)</option>
            {versions.map((v) => (
              <option key={v.fingerprint} value={v.fingerprint}>
                {shortFingerprint(v.fingerprint)}
                {v.is_baseline ? " · baseline" : ""} · {v.created_at.slice(0, 10)}
              </option>
            ))}
          </select>
        </label>
      </div>
      <p className="text-xs text-(--color-neutral-600)">
        &quot;Default&quot; uses the built-in methodology preset; a fingerprint selects a stored
        methodology version. Knowledge cutoff limits which rating events are visible to the run.
      </p>
      {submit.isError && <ErrorState error={submit.error} title="Failed to submit run" />}
      <div>
        <button type="submit" className="btn btn-primary" disabled={submit.isPending}>
          {submit.isPending ? "Submitting…" : "Submit run"}
        </button>
      </div>
    </form>
  );
}

/**
 * Run-control panel for the overview page: status of the active run, live
 * progress for a just-submitted run, failure/retry handling, and the
 * new-run submission form.
 */
export function RunControlPanel({
  runId,
  setRunId,
  defaultAsOf,
}: {
  runId: string | undefined;
  setRunId: (id: string | undefined) => void;
  defaultAsOf: string;
}) {
  const [trackingId, setTrackingId] = useState<string | null>(null);
  const [showForm, setShowForm] = useState(false);

  const handleSucceeded = (id: string) => {
    setRunId(id);
    setTrackingId(null);
  };

  return (
    <MetricCard kicker="Run control" title={trackingId ? "Submitted run" : "Scoring runs"}>
      <div className="flex flex-col gap-4">
        {trackingId ? (
          <TrackedRun runId={trackingId} onSucceeded={handleSucceeded} />
        ) : runId ? (
          <ActiveRunStatus runId={runId} />
        ) : (
          <p className="text-sm text-(--color-neutral-600)">No active run selected.</p>
        )}

        <div>
          <button
            type="button"
            className="btn btn-secondary"
            aria-expanded={showForm}
            onClick={() => setShowForm((v) => !v)}
          >
            {showForm ? "Hide new-run form" : "New run"}
          </button>
        </div>
        {showForm && (
          <NewRunForm
            defaultAsOf={defaultAsOf}
            onSubmitted={(id) => {
              setTrackingId(id);
              setShowForm(false);
            }}
          />
        )}
      </div>
    </MetricCard>
  );
}
