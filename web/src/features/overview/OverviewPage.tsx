import { Link } from "@tanstack/react-router";
import type { ReactNode } from "react";
import { EmptyState, ErrorState, LoadingState } from "../../components/states";
import { useOverview, useRuns } from "../shared/queries";
import { useActiveRun } from "../shared/run-state";
import { OverviewDashboard } from "./OverviewDashboard";
import { useRunLive } from "./queries";
import { RunControlPanel } from "./RunControlPanel";

// Shortcut targets verified against routeTree.gen.ts.
// Titles deliberately differ from the sidebar nav labels (shell.test asserts
// on the exact nav text).
const PAGES: {
  to: "/leaderboard" | "/analysts" | "/securities" | "/sectors" | "/changes";
  title: string;
  body: string;
}[] = [
  { to: "/leaderboard", title: "Rankings", body: "Rank analysts within peer groups." },
  { to: "/analysts", title: "Analyst directory", body: "Browse analyst profiles and metrics." },
  { to: "/securities", title: "Security coverage", body: "Coverage and ratings per security." },
  { to: "/sectors", title: "Sector performance", body: "Sector-level aggregates and performance." },
  { to: "/changes", title: "Audit log", body: "Rating events and run lifecycle history." },
];

function ShortcutCard({ to, title, body }: { to: string; title: string; body: string }) {
  return (
    <Link
      to={to}
      search={(prev) => ({ run_id: prev.run_id })}
      className="card elev-sm flex flex-col gap-1 hover:border-(--color-accent)"
    >
      <span className="card-title">{title}</span>
      <span className="text-xs text-(--color-neutral-600)">{body}</span>
    </Link>
  );
}

function todayIso(): string {
  return new Date().toISOString().slice(0, 10);
}

function isEmptyDb(rowCounts: Record<string, number> | null | undefined): boolean {
  if (!rowCounts) return false;
  const values = Object.values(rowCounts);
  return values.length === 0 || values.every((v) => v === 0);
}

/** `/` — product home: run control, overview dashboard, and page shortcuts. */
export function OverviewPage() {
  const { runId, setRunId, isDefaulting } = useActiveRun();
  const runsProbe = useRuns({ limit: 1, order: "desc" });
  const runDetail = useRunLive(runId);
  // Overview without a run_id resolves the latest succeeded run server-side;
  // used only to pre-fill the new-run form's as_of default.
  const overviewProbe = useOverview(runId);
  const defaultAsOf = overviewProbe.data?.data_coverage?.last_session ?? todayIso();

  const hasRuns = (runsProbe.data?.data?.length ?? 0) > 0;
  const runStatus = runDetail.data?.status;
  const dbEmpty = isEmptyDb(runDetail.data?.row_counts);

  let body: ReactNode;
  if (runsProbe.isPending || (isDefaulting && !runId)) {
    body = <LoadingState label="Loading overview" />;
  } else if (!hasRuns) {
    body = (
      <EmptyState
        title="No scoring runs yet"
        description={
          <span>
            There are no calc runs in the database. Seed demo data with{" "}
            <code className="text-xs">just seed small</code> (from the repo root), then submit a run
            with <code className="text-xs">POST /api/runs</code> or the form below.
          </span>
        }
      />
    );
  } else if (runId && runDetail.isPending) {
    body = <LoadingState label="Loading run" />;
  } else if (runId && (runStatus === "failed" || runStatus === "interrupted")) {
    body = (
      <ErrorState
        title="Active run failed"
        error={runDetail.data?.error?.message}
        onRetry={() => void runDetail.refetch()}
      />
    );
  } else if (runId && runDetail.isError) {
    body = (
      <ErrorState
        title="Failed to load run"
        error={runDetail.error}
        onRetry={() => void runDetail.refetch()}
      />
    );
  } else if (runId && runStatus !== "succeeded") {
    body = (
      <EmptyState
        title="Run in progress"
        description="This run has not finished yet. The dashboard will populate once it succeeds."
      />
    );
  } else if (runId && dbEmpty) {
    body = (
      <EmptyState
        title="Database is empty"
        description={
          <span>
            Run {runId.slice(0, 8)} completed but produced no rows — the source database is empty.
            Seed it with <code className="text-xs">just seed small</code> and submit a new run.
          </span>
        }
      />
    );
  } else if (runId) {
    body = <OverviewDashboard runId={runId} />;
  } else {
    body = (
      <EmptyState
        title="No succeeded run"
        description="No succeeded calc run exists yet. Submit one below or seed and run the engine."
      />
    );
  }

  return (
    <div className="flex flex-col gap-6">
      <div className="flex items-start justify-between gap-4">
        <div>
          <h1 className="font-heading text-2xl font-semibold">Overview</h1>
          <p className="text-sm text-(--color-neutral-600)">
            Run control and cross-run aggregates for analyst rating performance.
          </p>
        </div>
        <nav aria-label="Pages" className="grid grid-cols-5 gap-3">
          {PAGES.map((p) => (
            <ShortcutCard key={p.to} {...p} />
          ))}
        </nav>
      </div>

      <RunControlPanel runId={runId} setRunId={setRunId} defaultAsOf={defaultAsOf} />

      {body}
    </div>
  );
}
