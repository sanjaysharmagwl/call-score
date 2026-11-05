import { Link } from "@tanstack/react-router";
import { type ReactNode, useMemo } from "react";
import type { Schemas } from "../../api/query";
import { shortFingerprint } from "../../api/query";
import { EligibilityBadge, ScoreBadge } from "../../components/Badges";
import { BarChart } from "../../components/charts";
import type { DataTableColumnDef } from "../../components/DataTable";
import { DataTable } from "../../components/DataTable";
import { MetricCard } from "../../components/MetricCard";
import { ErrorState, LoadingState } from "../../components/states";
import { useAnalysts, useOverview } from "../shared/queries";
import { useRunLive } from "./queries";

type AnalystEntry = Schemas["AnalystLeaderboardEntry"];
type DataCoverage = Schemas["DataCoverage"];

function fmtTs(ts: string | null | undefined): string {
  return ts ? ts.slice(0, 16).replace("T", " ") : "—";
}

function Stat({ label, value }: { label: string; value: ReactNode }) {
  return (
    <div className="flex items-baseline justify-between gap-4 border-b border-(--color-divider) py-1 last:border-0">
      <dt className="text-xs text-(--color-neutral-600)">{label}</dt>
      <dd className="text-sm font-medium">{value}</dd>
    </div>
  );
}

function RunSummaryCard({ runId }: { runId: string }) {
  const detail = useRunLive(runId);
  const run = detail.data;
  const rowCounts = run?.row_counts ?? {};
  const rowEntries = Object.entries(rowCounts).sort(([a], [b]) => a.localeCompare(b));

  return (
    <MetricCard kicker="Run" title="Run summary">
      {detail.isPending ? (
        <LoadingState label="Loading run summary" />
      ) : detail.isError || !run ? (
        <ErrorState error={detail.error} onRetry={() => void detail.refetch()} title="Run" />
      ) : (
        <dl>
          <Stat label="Run id" value={<span className="font-mono text-xs">{run.run_id}</span>} />
          <Stat label="As of" value={run.as_of_date} />
          <Stat
            label="Methodology"
            value={
              <span className="font-mono text-xs" title={run.methodology_fingerprint ?? undefined}>
                {run.methodology_fingerprint
                  ? shortFingerprint(run.methodology_fingerprint)
                  : "default"}
              </span>
            }
          />
          <Stat
            label="Source"
            value={
              <span className="font-mono text-xs" title={run.source_fingerprint ?? undefined}>
                {run.source_fingerprint ? shortFingerprint(run.source_fingerprint) : "—"}
              </span>
            }
          />
          <Stat label="Algorithm" value={run.algorithm_version || "—"} />
          <Stat
            label="Row counts"
            value={
              rowEntries.length === 0 ? (
                "—"
              ) : (
                <span className="font-mono text-xs">
                  {rowEntries.map(([k, v]) => `${k}=${v}`).join("  ")}
                </span>
              )
            }
          />
        </dl>
      )}
    </MetricCard>
  );
}

function AnalystsCard({ runId }: { runId: string }) {
  const overview = useOverview(runId);
  const analysts = overview.data?.analysts;

  const chartData = useMemo(
    () =>
      (analysts?.score_distribution ?? []).map((b) => ({
        x: b.bucket,
        y: b.count,
      })),
    [analysts?.score_distribution],
  );

  return (
    <MetricCard kicker="Analysts" title="Analyst coverage">
      {overview.isPending ? (
        <LoadingState label="Loading analyst aggregates" />
      ) : overview.isError ? (
        <ErrorState error={overview.error} onRetry={() => void overview.refetch()} />
      ) : analysts ? (
        <div className="flex flex-col gap-3">
          <dl>
            <Stat label="Analysts" value={analysts.n_analysts} />
            <Stat label="Ranked" value={analysts.n_ranked} />
            <Stat label="Scored" value={analysts.n_scored} />
          </dl>
          <BarChart
            title="Composite score distribution"
            data={chartData}
            height={180}
            width={520}
            formatY={(v) => String(Math.round(v))}
          />
        </div>
      ) : null}
    </MetricCard>
  );
}

function AnalystRankingTable({
  title,
  entries,
  names,
}: {
  title: string;
  entries: AnalystEntry[];
  names: Map<string, string>;
}) {
  const columns = useMemo<DataTableColumnDef<AnalystEntry>[]>(
    () => [
      {
        id: "rank",
        header: "Rank",
        accessorFn: (row) => row.rank_overall ?? row.rank_in_peer_group ?? null,
        cell: (ctx) => {
          const row = ctx.row.original;
          const rank = row.rank_overall ?? row.rank_in_peer_group;
          return rank === null || rank === undefined ? "—" : `#${rank}`;
        },
      },
      {
        id: "analyst",
        header: "Analyst",
        accessorFn: (row) => names.get(row.analyst_id) ?? row.analyst_id,
        cell: (ctx) => {
          const row = ctx.row.original;
          const label = names.get(row.analyst_id) ?? row.analyst_id;
          return (
            <Link
              to="/analysts/$analystId"
              params={{ analystId: row.analyst_id }}
              search={(prev) => ({ run_id: prev.run_id })}
              className="font-medium text-(--color-accent) hover:underline"
            >
              {label}
            </Link>
          );
        },
      },
      {
        id: "peer_group",
        header: "Peer group",
        accessorFn: (row) => row.peer_group ?? "",
        cell: (ctx) => ctx.getValue<string>() || "—",
      },
      {
        id: "composite",
        header: "Composite",
        accessorFn: (row) => row.composite_score ?? null,
        cell: (ctx) => {
          const row = ctx.row.original;
          return (
            <ScoreBadge score={row.composite_score} rank={row.rank_in_peer_group} digits={3} />
          );
        },
      },
      {
        id: "eligibility",
        header: "Eligibility",
        accessorFn: (row) => row.is_ranked,
        cell: (ctx) => {
          const row = ctx.row.original;
          return <EligibilityBadge isRanked={row.is_ranked} unrankedReason={row.unranked_reason} />;
        },
      },
    ],
    [names],
  );

  return (
    <section className="card elev-sm flex flex-col gap-2">
      <h3 className="card-title">{title}</h3>
      <DataTable
        columns={columns}
        data={entries}
        caption={title}
        emptyTitle="No ranked analysts"
        emptyDescription="This run produced no composite scores."
      />
    </section>
  );
}

function CoverageCard({ coverage }: { coverage: DataCoverage }) {
  return (
    <MetricCard kicker="Data" title="Data coverage">
      <dl>
        <Stat
          label="Sessions"
          value={
            coverage.first_session
              ? `${coverage.first_session} → ${coverage.last_session ?? "?"} (${coverage.n_sessions})`
              : "—"
          }
        />
        <Stat label="Exchanges" value={coverage.n_exchanges} />
        <Stat
          label="Securities"
          value={`${coverage.n_securities} (${coverage.n_delisted_securities} delisted)`}
        />
        <Stat label="Rating events" value={coverage.n_rating_events.toLocaleString()} />
        <Stat label="Latest event" value={fmtTs(coverage.latest_event_ts)} />
        <Stat
          label="Consensus"
          value={`${coverage.n_consensus_rows.toLocaleString()} rows · latest ${coverage.latest_consensus_date ?? "—"}`}
        />
        <Stat
          label="Security daily"
          value={`${coverage.n_security_daily_rows.toLocaleString()} rows`}
        />
      </dl>
    </MetricCard>
  );
}

function RunCountsCard({ counts }: { counts: Record<string, number> }) {
  const entries = Object.entries(counts).sort(([a], [b]) => a.localeCompare(b));
  return (
    <MetricCard kicker="Runs" title="Runs by status">
      {entries.length === 0 ? (
        <p className="text-sm text-(--color-neutral-600)">No runs recorded.</p>
      ) : (
        <dl>
          {entries.map(([status, n]) => (
            <Stat key={status} label={status} value={n} />
          ))}
        </dl>
      )}
    </MetricCard>
  );
}

function MethodologyCard({ runId }: { runId: string }) {
  const detail = useRunLive(runId);
  const fingerprint = detail.data?.methodology_fingerprint;
  return (
    <MetricCard kicker="Methodology" title="Methodology version">
      {detail.isPending ? (
        <LoadingState label="Loading methodology" />
      ) : fingerprint ? (
        <div className="flex flex-col gap-1">
          <p
            className="font-mono text-sm"
            title={fingerprint}
            data-testid="methodology-fingerprint"
          >
            {shortFingerprint(fingerprint)}
          </p>
          <p className="text-xs text-(--color-neutral-600)">
            Methodology detail pages are coming soon.
          </p>
        </div>
      ) : (
        <p className="text-sm text-(--color-neutral-600)">Default methodology preset.</p>
      )}
    </MetricCard>
  );
}

/**
 * Overview dashboard for one run: run summary, analyst aggregates with a
 * score-distribution chart, top/bottom analyst tables, run counts by status,
 * and data coverage.
 */
export function OverviewDashboard({ runId }: { runId: string }) {
  const overview = useOverview(runId);
  const analystsQuery = useAnalysts({ run_id: runId, limit: 500 });
  const names = useMemo(() => {
    const map = new Map<string, string>();
    for (const a of analystsQuery.data?.items ?? []) {
      map.set(a.analyst_id, a.name ?? a.analyst_id);
    }
    return map;
  }, [analystsQuery.data]);

  if (overview.isPending) {
    return <LoadingState label="Loading overview" />;
  }
  if (overview.isError || !overview.data) {
    return (
      <ErrorState
        error={overview.error}
        onRetry={() => void overview.refetch()}
        title="Failed to load overview"
      />
    );
  }

  const { analysts, run_counts_by_status, data_coverage } = overview.data;

  return (
    <div className="flex flex-col gap-4" data-testid="overview-dashboard">
      <div className="grid grid-cols-2 gap-4">
        <RunSummaryCard runId={runId} />
        <MethodologyCard runId={runId} />
        <AnalystsCard runId={runId} />
        <CoverageCard coverage={data_coverage} />
      </div>
      <div className="grid grid-cols-2 gap-4">
        <AnalystRankingTable title="Top analysts" entries={analysts.top} names={names} />
        <AnalystRankingTable title="Bottom analysts" entries={analysts.bottom} names={names} />
      </div>
      <RunCountsCard counts={run_counts_by_status} />
    </div>
  );
}
