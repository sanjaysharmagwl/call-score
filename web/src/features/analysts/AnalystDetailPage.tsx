import { Link } from "@tanstack/react-router";
import { type ReactNode, useMemo, useState } from "react";
import type { Schemas } from "../../api/query";
import { EligibilityBadge, ProvenanceLabel, ScoreBadge } from "../../components/Badges";
import { LineChart } from "../../components/charts";
import type { DataTableColumnDef } from "../../components/DataTable";
import { DataTable } from "../../components/DataTable";
import { MetricCard } from "../../components/MetricCard";
import { EmptyState, ErrorState, LoadingState } from "../../components/states";
import { useAnalyst } from "../shared/queries";
import { useActiveRun } from "../shared/run-state";
import { ActionBadge, RatingBadge } from "./badges";
import { fmtDate, fmtNum, fmtPct, fmtSignedPct, humanize } from "./format";
import { type RatingListItem, useAnalystPortfolio, useAnalystRatings } from "./queries";

type MetricDetail = Schemas["MetricDetail"];
type Spell = Schemas["Spell"];
type FamilyScores = Schemas["FamilyScores"];

const RATINGS_PAGE_SIZE = 20;

const FAMILIES: { key: keyof FamilyScores; label: string }[] = [
  { key: "return_skill", label: "Return skill" },
  { key: "timing", label: "Timing" },
  { key: "price_target", label: "Price target" },
  { key: "behavior", label: "Behavior" },
  { key: "consistency", label: "Consistency" },
];

function Section({
  title,
  children,
  aside,
}: {
  title: string;
  children: ReactNode;
  aside?: ReactNode;
}) {
  return (
    <section className="card elev-sm flex flex-col gap-3">
      <div className="flex items-center justify-between gap-2">
        <h2 className="card-title">{title}</h2>
        {aside}
      </div>
      {children}
    </section>
  );
}

function fmtMetricValue(metric: MetricDetail): string {
  if (metric.value === null || metric.value === undefined) return "—";
  if (metric.unit === "return" || metric.unit === "fraction") return fmtPct(metric.value, 1);
  if (metric.unit === "ratio") return fmtNum(metric.value, 2);
  if (metric.unit === "count" || metric.unit === "per_year") return fmtNum(metric.value, 1);
  return fmtNum(metric.value, 3);
}

function groupByFamily(metrics: MetricDetail[]): [string, MetricDetail[]][] {
  const groups = new Map<string, MetricDetail[]>();
  for (const m of metrics) {
    const fam = m.family ?? "other";
    const list = groups.get(fam) ?? [];
    list.push(m);
    groups.set(fam, list);
  }
  const order = [...FAMILIES.map((f) => f.key), "other"];
  return [...groups.entries()].sort((a, b) => order.indexOf(a[0]) - order.indexOf(b[0]));
}

/** `/analysts/$analystId` — profile, composite, metrics, spells, portfolio, ratings. */
export function AnalystDetailPage({ analystId }: { analystId: string }) {
  const { runId } = useActiveRun();
  const profileQuery = useAnalyst(analystId, runId);
  const portfolioQuery = useAnalystPortfolio(analystId, runId);
  const [ratingsPage, setRatingsPage] = useState(0);
  const ratingsQuery = useAnalystRatings(analystId, {
    run_id: runId ?? "",
    sort: "entry_session",
    order: "desc",
    limit: RATINGS_PAGE_SIZE,
    offset: ratingsPage * RATINGS_PAGE_SIZE,
  });

  const profile = profileQuery.data;
  const composite = profile?.composite ?? null;
  const metrics = profile?.metrics ?? [];
  const metricsByFamily = useMemo(() => groupByFamily(metrics), [metrics]);

  const spellColumns = useMemo<DataTableColumnDef<Spell>[]>(
    () => [
      {
        id: "firm_or_security",
        header: "Firm / security",
        accessorFn: (row) => row.firm_id ?? row.security_id ?? "",
        cell: (ctx) => {
          const row = ctx.row.original;
          return row.firm_id ?? row.security_id ?? "—";
        },
      },
      {
        id: "from_date",
        header: "From",
        accessorKey: "from_date",
        cell: (ctx) => fmtDate(ctx.getValue<string>()),
      },
      {
        id: "to_date",
        header: "To",
        accessorKey: "to_date",
        cell: (ctx) => {
          const v = ctx.getValue<string | null | undefined>();
          return v ? fmtDate(v) : "open";
        },
      },
      {
        id: "is_lead",
        header: "Lead",
        accessorKey: "is_lead",
        cell: (ctx) => (ctx.getValue<boolean | null | undefined>() ? "yes" : "—"),
      },
    ],
    [],
  );

  const metricColumns = useMemo<DataTableColumnDef<MetricDetail>[]>(
    () => [
      {
        id: "metric_code",
        header: "Metric",
        accessorKey: "metric_code",
        cell: (ctx) => (
          <span className="font-mono text-xs" title={ctx.row.original.formula ?? undefined}>
            {ctx.row.original.metric_code}
          </span>
        ),
      },
      {
        id: "value",
        header: "Value",
        accessorFn: (row) => row.value ?? null,
        cell: (ctx) => {
          const m = ctx.row.original;
          if (m.value === null || m.value === undefined) {
            return (
              <span
                className="text-(--color-neutral-500)"
                title={m.unavailable_reason ?? undefined}
              >
                n/a{m.unavailable_reason ? ` — ${m.unavailable_reason}` : ""}
              </span>
            );
          }
          return (
            <span className="inline-flex items-center gap-1">
              {fmtMetricValue(m)}
              {m.in_composite === false && (
                <span className="tag tag-neutral" title="Not in composite">
                  excl
                </span>
              )}
            </span>
          );
        },
      },
      {
        id: "n_obs",
        header: "n",
        accessorKey: "n_obs",
      },
      {
        id: "ci",
        header: "95% CI",
        accessorFn: (row) => row.ci_low ?? null,
        cell: (ctx) => {
          const m = ctx.row.original;
          if (m.ci_low === null || m.ci_low === undefined || m.ci_high === null) return "—";
          return `[${fmtMetricValue({ ...m, value: m.ci_low })}, ${fmtMetricValue({ ...m, value: m.ci_high ?? 0 })}]`;
        },
      },
      {
        id: "peer_percentile",
        header: "Peer %",
        accessorFn: (row) => row.peer_percentile ?? null,
        cell: (ctx) => {
          const p = ctx.row.original.peer_percentile;
          return p === null || p === undefined ? "—" : `${Math.round(p)}%`;
        },
      },
      {
        id: "shrunk_value",
        header: "Shrunk",
        accessorFn: (row) => row.shrunk_value ?? null,
        cell: (ctx) => {
          const s = ctx.row.original.shrunk_value;
          return s === null || s === undefined
            ? "—"
            : fmtMetricValue({ ...ctx.row.original, value: s });
        },
      },
    ],
    [],
  );

  const ratingColumns = useMemo<DataTableColumnDef<RatingListItem>[]>(
    () => [
      {
        id: "entry_session",
        header: "Entry",
        accessorKey: "entry_session",
        cell: (ctx) => fmtDate(ctx.getValue<string>()),
      },
      {
        id: "exit_session",
        header: "Exit",
        accessorKey: "exit_session",
        cell: (ctx) => fmtDate(ctx.getValue<string>()),
      },
      {
        id: "security_id",
        header: "Security",
        accessorKey: "security_id",
        cell: (ctx) => {
          const row = ctx.row.original;
          return (
            <Link
              to="/securities/$securityId"
              params={{ securityId: row.security_id }}
              search={(prev) => ({ run_id: prev.run_id })}
              className="font-mono text-xs text-(--color-accent) hover:underline"
            >
              {row.security_id}
            </Link>
          );
        },
      },
      {
        id: "rating",
        header: "Rating",
        accessorFn: (row) => row.canonical_rating,
        cell: (ctx) => {
          const row = ctx.row.original;
          return (
            <span className="inline-flex items-center gap-1">
              <RatingBadge rating={row.canonical_rating} />
              {row.native_rating && row.native_rating !== row.canonical_rating && (
                <span className="text-xs text-(--color-neutral-500)" title="Native rating">
                  ({row.native_rating})
                </span>
              )}
            </span>
          );
        },
      },
      {
        id: "action_type",
        header: "Action",
        accessorKey: "action_type",
        cell: (ctx) => <ActionBadge action={ctx.getValue<string>()} />,
      },
      {
        id: "exit_reason",
        header: "Exit reason",
        accessorKey: "exit_reason",
        cell: (ctx) => humanize(ctx.getValue<string | null | undefined>()),
      },
      {
        id: "weight",
        header: "Weight",
        accessorFn: (row) => row.exposures?.[0]?.position_weight ?? null,
        cell: (ctx) => {
          const legs = ctx.row.original.exposures ?? [];
          if (legs.length === 0) return "—";
          return legs
            .map((e) => (e.position_weight == null ? "—" : e.position_weight.toFixed(2)))
            .join(" / ");
        },
      },
      {
        id: "excess",
        header: "Excess",
        accessorFn: (row) => row.return_summary?.excess_return ?? null,
        cell: (ctx) => fmtSignedPct(ctx.row.original.return_summary?.excess_return),
      },
      {
        id: "interval",
        header: "",
        accessorKey: "interval_id",
        enableSorting: false,
        cell: (ctx) => {
          const row = ctx.row.original;
          return (
            <Link
              to="/analysts/$analystId/ratings/$intervalId"
              params={{ analystId, intervalId: row.interval_id }}
              search={(prev) => ({ run_id: prev.run_id })}
              className="text-xs text-(--color-accent) hover:underline"
            >
              detail →
            </Link>
          );
        },
      },
    ],
    [analystId],
  );

  const portfolio = portfolioQuery.data;
  const positionRows = portfolio?.positions.rows ?? [];
  const positionCols = useMemo(() => {
    const keys = new Set<string>();
    for (const row of positionRows.slice(0, 50)) {
      for (const k of Object.keys(row)) keys.add(k);
    }
    return [...keys];
  }, [positionRows]);

  if (profileQuery.isPending) {
    return <LoadingState label="Loading analyst" />;
  }
  if (profileQuery.isError || !profile) {
    return (
      <ErrorState
        error={profileQuery.error}
        onRetry={() => void profileQuery.refetch()}
        title="Failed to load analyst"
      />
    );
  }

  const ratings = ratingsQuery.data?.items ?? [];
  const ratingsTotal = ratingsQuery.data?.total ?? 0;
  const navPoints = (portfolio?.points ?? []).map((p) => ({
    x: p.session_date,
    y: p.nav,
  }));
  const drawdownPoints = (portfolio?.points ?? []).map((p) => ({
    x: p.session_date,
    y: p.drawdown,
  }));

  return (
    <div className="flex flex-col gap-4">
      <div className="flex flex-col gap-1">
        <div className="flex flex-wrap items-center gap-3">
          <h1 className="font-heading text-2xl font-semibold">
            {profile.name ?? profile.analyst_id}
          </h1>
          {profile.firm_name && <span className="tag tag-neutral">{profile.firm_name}</span>}
          {profile.peer_group && <span className="tag tag-neutral">{profile.peer_group}</span>}
        </div>
        <div className="flex flex-wrap items-center gap-x-4 gap-y-1 text-sm text-(--color-neutral-700)">
          <span className="font-mono text-xs">{profile.analyst_id}</span>
          {profile.composite && (
            <EligibilityBadge
              isRanked={profile.composite.is_ranked}
              unrankedReason={profile.composite.unranked_reason}
            />
          )}
        </div>
        {profile.composite?.unranked_reason && !profile.composite.is_ranked && (
          <p className="text-sm text-(--color-neutral-600)">
            Unranked: {profile.composite.unranked_reason}
          </p>
        )}
      </div>

      {composite && (
        <Section
          title="Composite"
          aside={
            <ProvenanceLabel
              run_id={profile.provenance?.run_id}
              methodology_fingerprint={profile.provenance?.methodology_fingerprint}
              as_of_date={profile.provenance?.as_of_date}
            />
          }
        >
          <div className="grid grid-cols-2 gap-4 md:grid-cols-4">
            <MetricCard title="Composite score">
              <ScoreBadge
                score={composite.composite_score}
                rank={composite.rank_overall}
                digits={2}
              />
              {composite.rank_in_peer_group !== null &&
                composite.rank_in_peer_group !== undefined && (
                  <span className="text-xs text-(--color-neutral-600)">
                    #{composite.rank_in_peer_group} in peer group
                  </span>
                )}
            </MetricCard>
            <MetricCard title="Observations">
              <span className="font-heading text-2xl font-semibold">{composite.n_obs}</span>
              <span className="text-xs text-(--color-neutral-600)">investable intervals</span>
            </MetricCard>
            <MetricCard title="Shrinkage">
              <span className="font-heading text-2xl font-semibold">
                {composite.shrinkage?.method ?? "—"}
              </span>
              <span className="text-xs text-(--color-neutral-600)">
                {composite.shrinkage?.shrunk_values_present
                  ? "shrunk values present"
                  : "no shrunk values"}
              </span>
            </MetricCard>
            <MetricCard title="Peer group">
              <span className="font-heading text-2xl font-semibold">
                {composite.peer_group ?? "—"}
              </span>
            </MetricCard>
          </div>
          {composite.family_scores && (
            <div className="grid grid-cols-2 gap-4 md:grid-cols-5">
              {FAMILIES.map(({ key, label }) => {
                const v = composite.family_scores?.[key];
                return (
                  <MetricCard key={key} title={label}>
                    <span className="font-heading text-2xl font-semibold">
                      {v === null || v === undefined ? "—" : v.toFixed(2)}
                    </span>
                  </MetricCard>
                );
              })}
            </div>
          )}
        </Section>
      )}

      <Section title="Metrics by family">
        {metrics.length === 0 ? (
          <EmptyState
            title="No metrics for this run"
            description="This analyst has no metric results in the active run."
          />
        ) : (
          metricsByFamily.map(([family, items]) => (
            <div key={family} className="flex flex-col gap-1">
              <h3 className="text-xs font-semibold uppercase tracking-[0.08em] text-(--color-neutral-600)">
                {humanize(family)}
              </h3>
              <DataTable<MetricDetail>
                columns={metricColumns}
                data={items}
                caption={`${humanize(family)} metrics`}
              />
            </div>
          ))
        )}
      </Section>

      <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
        <Section title="Employment spells">
          <DataTable<Spell>
            columns={spellColumns}
            data={profile.employment_spells}
            caption="Employment spells"
            emptyTitle="No employment spells"
          />
        </Section>
        <Section title="Coverage spells">
          <DataTable<Spell>
            columns={spellColumns}
            data={profile.coverage_spells}
            caption="Coverage spells"
            emptyTitle="No coverage spells"
          />
        </Section>
      </div>

      <Section
        title="Portfolio"
        aside={
          portfolio?.downsampled ? (
            <span className="text-xs text-(--color-neutral-600)">
              downsampled to {portfolio.points.length} of {portfolio.total_points} points
            </span>
          ) : undefined
        }
      >
        {portfolioQuery.isPending ? (
          <LoadingState label="Loading portfolio" />
        ) : portfolioQuery.isError ? (
          <ErrorState
            error={portfolioQuery.error}
            onRetry={() => void portfolioQuery.refetch()}
            title="Failed to load portfolio"
          />
        ) : (
          <div className="flex flex-col gap-4">
            {navPoints.length === 0 ? (
              <EmptyState
                title="No NAV history"
                description="No portfolio NAV has been computed for this analyst in the active run."
              />
            ) : (
              <>
                <LineChart
                  title="NAV"
                  data={navPoints}
                  height={200}
                  formatY={(v) => v.toFixed(2)}
                />
                <LineChart
                  title="Drawdown"
                  data={drawdownPoints}
                  height={160}
                  formatY={(v) => `${(v * 100).toFixed(1)}%`}
                />
              </>
            )}
            {positionRows.length > 0 && (
              <div className="flex flex-col gap-1">
                <h3 className="text-xs font-semibold uppercase tracking-[0.08em] text-(--color-neutral-600)">
                  Positions snapshot
                  {portfolio?.positions.session_date
                    ? ` — ${fmtDate(portfolio.positions.session_date)}`
                    : ""}
                </h3>
                <div className="overflow-x-auto">
                  <table className="table">
                    <thead>
                      <tr>
                        {positionCols.map((k) => (
                          <th key={k} scope="col">
                            {k}
                          </th>
                        ))}
                      </tr>
                    </thead>
                    <tbody>
                      {positionRows.map((row, i) => (
                        // biome-ignore lint/suspicious/noArrayIndexKey: snapshot rows have no stable id
                        <tr key={i}>
                          {positionCols.map((k) => {
                            const v = row[k];
                            return (
                              <td key={k}>
                                {v === null || v === undefined
                                  ? "—"
                                  : typeof v === "number"
                                    ? fmtNum(v, 4)
                                    : String(v)}
                              </td>
                            );
                          })}
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              </div>
            )}
          </div>
        )}
      </Section>

      <Section
        title="Ratings"
        aside={
          ratingsQuery.data ? (
            <span className="text-xs text-(--color-neutral-600)">
              {ratings.length} of {ratingsTotal}
            </span>
          ) : undefined
        }
      >
        {ratingsQuery.isPending ? (
          <LoadingState label="Loading ratings" />
        ) : ratingsQuery.isError ? (
          <ErrorState
            error={ratingsQuery.error}
            onRetry={() => void ratingsQuery.refetch()}
            title="Failed to load ratings"
          />
        ) : (
          <DataTable<RatingListItem>
            columns={ratingColumns}
            data={ratings}
            caption="Rating intervals"
            emptyTitle="No ratings in this run"
            emptyDescription="This analyst has no rating intervals in the active run."
            manualPagination
            pagination={{ pageIndex: ratingsPage, pageSize: RATINGS_PAGE_SIZE }}
            onPaginationChange={(p) => setRatingsPage(p.pageIndex)}
            rowCount={ratingsTotal}
            pageCount={Math.max(1, Math.ceil(ratingsTotal / RATINGS_PAGE_SIZE))}
          />
        )}
      </Section>

      <p className="text-xs text-(--color-neutral-500)">
        Entry/exit timestamps shown in UTC; spells use inclusive end dates (open-ended when blank).
      </p>
    </div>
  );
}
