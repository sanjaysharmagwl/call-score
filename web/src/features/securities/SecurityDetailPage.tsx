import { Link } from "@tanstack/react-router";
import { type ReactNode, useMemo, useState } from "react";
import type { Schemas } from "../../api/query";
import { ProvenanceLabel } from "../../components/Badges";
import { LineChart } from "../../components/charts";
import type { DataTableColumnDef } from "../../components/DataTable";
import { DataTable } from "../../components/DataTable";
import { MetricCard } from "../../components/MetricCard";
import { EmptyState, ErrorState, LoadingState } from "../../components/states";
import { useSectors, useSecurity } from "../shared/queries";
import { useRunSelection } from "../shared/run-state";
import { ActionBadge, CorrectionBadge, DelistedBadge, RatingBadge } from "./badges";
import { fmtDate, fmtNum, fmtPct, fmtTs, humanize } from "./format";
import {
  type RatingEventItem,
  type TargetOutcomeItem,
  useSecurityConsensus,
  useSecurityPerformance,
  useSecurityPrices,
  useSecurityRatings,
} from "./queries";

const PRICE_INTERVALS = ["1d", "1w", "1M"] as const;
const RATINGS_PAGE = 100;

type ClassificationSpell = Schemas["ClassificationSpell"];
type IdentifierSpell = Schemas["IdentifierSpell"];
type HorizonAggregate = Schemas["HorizonAggregate"];

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

function Stat({ label, value }: { label: string; value: ReactNode }) {
  return (
    <div className="flex flex-col">
      <dt className="text-xs text-(--color-neutral-500)">{label}</dt>
      <dd className="text-sm font-medium">{value}</dd>
    </div>
  );
}

/** `/securities/$securityId` — full security detail: identifiers,
 *  classifications, delisting, prices, consensus, ratings, performance. */
export function SecurityDetailPage({ securityId }: { securityId: string }) {
  const detailQuery = useSecurity(securityId);
  const { runId } = useRunSelection();
  const [interval, setInterval] = useState<string>("1w");

  const detail = detailQuery.data;
  const pricesQuery = useSecurityPrices(securityId, { interval, adjusted: true, limit: 500 });
  const ratingsQuery = useSecurityRatings(securityId, {
    run_id: runId,
    limit: RATINGS_PAGE,
  });
  const consensusQuery = useSecurityConsensus(securityId, { run_id: runId });
  const perfQuery = useSecurityPerformance(securityId, runId);

  const sectorId = detail?.latest_classification?.sector_id ?? null;
  const sectorsQuery = useSectors({ limit: 500 });
  const sectorName = useMemo(() => {
    if (!sectorId) return detail?.sector_name ?? null;
    const found = sectorsQuery.data?.data?.find((s) => s.sector_id === sectorId);
    return found?.name ?? detail?.sector_name ?? sectorId;
  }, [sectorId, sectorsQuery.data, detail?.sector_name]);

  const identifierColumns = useMemo<DataTableColumnDef<IdentifierSpell>[]>(
    () => [
      { id: "id_type", header: "Type", accessorKey: "id_type" },
      { id: "value", header: "Value", accessorKey: "value" },
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
          return v ? fmtDate(v) : "present";
        },
      },
    ],
    [],
  );

  const spellColumns = useMemo<DataTableColumnDef<ClassificationSpell>[]>(
    () => [
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
      { id: "exchange_id", header: "Exchange", accessorKey: "exchange_id" },
      { id: "currency", header: "Ccy", accessorKey: "currency" },
      { id: "sector_id", header: "Sector", accessorKey: "sector_id" },
      { id: "region", header: "Region", accessorKey: "region" },
      { id: "cap_bucket", header: "Cap", accessorKey: "cap_bucket" },
      {
        id: "listing_date",
        header: "Listed",
        accessorKey: "listing_date",
        cell: (ctx) => fmtDate(ctx.getValue<string>()),
      },
      {
        id: "delisting_date",
        header: "Delisted",
        accessorKey: "delisting_date",
        cell: (ctx) => {
          const v = ctx.getValue<string | null | undefined>();
          return v ? fmtDate(v) : "—";
        },
      },
    ],
    [],
  );

  const ratingColumns = useMemo<DataTableColumnDef<RatingEventItem>[]>(
    () => [
      {
        id: "announced",
        header: "Announced",
        accessorKey: "announced_ts",
        cell: (ctx) => fmtTs(ctx.getValue<string>()),
      },
      {
        id: "analyst",
        header: "Analyst",
        accessorFn: (row) => row.analyst_name ?? row.analyst_id,
        cell: (ctx) => {
          const row = ctx.row.original;
          return (
            <Link
              to="/analysts/$analystId"
              params={{ analystId: row.analyst_id }}
              search={(prev) => ({ run_id: prev.run_id })}
              className="text-(--color-accent) hover:underline"
            >
              {row.analyst_name ?? row.analyst_id}
            </Link>
          );
        },
      },
      {
        id: "action",
        header: "Action",
        accessorKey: "action_type",
        cell: (ctx) => <ActionBadge action={ctx.getValue<string>()} />,
      },
      {
        id: "rating",
        header: "Rating",
        accessorFn: (row) => row.canonical_rating,
        cell: (ctx) => {
          const row = ctx.row.original;
          return (
            <span className="inline-flex items-center gap-1">
              {row.prior_canonical_rating && (
                <span className="text-xs text-(--color-neutral-500)">
                  {humanize(row.prior_canonical_rating)} →
                </span>
              )}
              <RatingBadge rating={row.canonical_rating} />
            </span>
          );
        },
      },
      {
        id: "pt",
        header: "Price target",
        accessorFn: (row) => row.price_target ?? null,
        cell: (ctx) => {
          const row = ctx.row.original;
          if (row.price_target == null) return "—";
          return `${fmtNum(row.price_target)} ${row.pt_currency ?? ""}`.trim();
        },
      },
      {
        id: "flags",
        header: "Flags",
        accessorFn: (row) => (row.is_correction ? "correction" : ""),
        cell: (ctx) => (ctx.row.original.is_correction ? <CorrectionBadge /> : null),
      },
      {
        id: "excess",
        header: "Excess",
        accessorFn: (row) => row.excess_return ?? null,
        cell: (ctx) => fmtPct(ctx.row.original.excess_return),
      },
    ],
    [],
  );

  const outcomeColumns = useMemo<DataTableColumnDef<TargetOutcomeItem>[]>(
    () => [
      {
        id: "analyst",
        header: "Analyst",
        accessorFn: (row) => row.analyst_id,
        cell: (ctx) => {
          const row = ctx.row.original;
          return (
            <Link
              to="/analysts/$analystId"
              params={{ analystId: row.analyst_id }}
              search={(prev) => ({ run_id: prev.run_id })}
              className="text-(--color-accent) hover:underline"
            >
              {row.analyst_id}
            </Link>
          );
        },
      },
      { id: "rating", header: "Rating", accessorKey: "canonical_rating" },
      {
        id: "entry",
        header: "Entry",
        accessorKey: "entry_session",
        cell: (ctx) => fmtDate(ctx.getValue<string>()),
      },
      {
        id: "horizon_end",
        header: "Horizon end",
        accessorKey: "horizon_end_session",
        cell: (ctx) => fmtDate(ctx.getValue<string>()),
      },
      {
        id: "implied",
        header: "Implied",
        accessorKey: "implied_return",
        cell: (ctx) => fmtPct(ctx.getValue<number>()),
      },
      {
        id: "realized",
        header: "Realized",
        accessorKey: "realized_return",
        cell: (ctx) => fmtPct(ctx.getValue<number | null | undefined>()),
      },
      {
        id: "hit",
        header: "Hit",
        accessorKey: "hit",
        cell: (ctx) => {
          const hit = ctx.getValue<boolean | null | undefined>();
          if (hit === null || hit === undefined) return "—";
          return hit ? "yes" : "no";
        },
      },
    ],
    [],
  );

  const horizonColumns = useMemo<DataTableColumnDef<HorizonAggregate>[]>(
    () => [
      { id: "horizon", header: "Horizon", accessorKey: "horizon" },
      { id: "n", header: "n", accessorKey: "n" },
      {
        id: "excess",
        header: "Mean excess",
        accessorKey: "excess_return",
        cell: (ctx) => fmtPct(ctx.getValue<number | null | undefined>()),
      },
      {
        id: "stock",
        header: "Mean stock",
        accessorKey: "stock_return",
        cell: (ctx) => fmtPct(ctx.getValue<number | null | undefined>()),
      },
      {
        id: "benchmark",
        header: "Mean benchmark",
        accessorKey: "benchmark_return",
        cell: (ctx) => fmtPct(ctx.getValue<number | null | undefined>()),
      },
    ],
    [],
  );

  if (detailQuery.isPending) {
    return <LoadingState label="Loading security" />;
  }
  if (detailQuery.isError || !detail) {
    return (
      <ErrorState
        error={detailQuery.error}
        onRetry={() => void detailQuery.refetch()}
        title="Failed to load security"
      />
    );
  }

  const cls = detail.latest_classification;
  const delisting = detail.delisting;
  const prices = pricesQuery.data?.rows ?? [];
  const pricePoints = prices.map((r) => ({
    x: r.session_date,
    y: r.adj_close ?? r.close ?? null,
  }));
  const consensusPoints = (consensusQuery.data?.consensus ?? []).map((c) => ({
    x: c.session_date,
    y: c.consensus_score ?? null,
  }));
  const ratings = ratingsQuery.data?.data ?? [];
  const targetOutcomes = consensusQuery.data?.target_outcomes ?? [];
  const perf = perfQuery.data;

  return (
    <div className="flex flex-col gap-4">
      <div className="flex flex-col gap-1">
        <div className="flex flex-wrap items-center gap-3">
          <h1 className="font-heading text-2xl font-semibold">{detail.name}</h1>
          {detail.is_delisted && <DelistedBadge />}
        </div>
        <div className="flex flex-wrap items-center gap-x-4 gap-y-1 text-sm text-(--color-neutral-700)">
          <span className="font-mono text-xs">{detail.security_id}</span>
          {sectorId && sectorName && (
            <Link
              to="/sectors/$sectorId"
              params={{ sectorId }}
              search={(prev) => ({ run_id: prev.run_id })}
              className="text-(--color-accent) hover:underline"
            >
              {sectorName}
            </Link>
          )}
          {cls?.cap_bucket && <span className="tag tag-neutral">{cls.cap_bucket}</span>}
          {cls?.region && <span className="tag tag-neutral">{cls.region}</span>}
          {cls?.currency && <span className="tag tag-neutral">{cls.currency}</span>}
          {cls?.exchange_id && <span className="tag tag-neutral">{cls.exchange_id}</span>}
        </div>
        {detail.is_delisted && delisting && (
          <div
            role="alert"
            className="border border-(--color-accent) bg-(--color-accent-100) px-3 py-2 text-sm"
            data-testid="delisting-banner"
          >
            <strong>Delisted</strong> on {fmtDate(delisting.delisting_date)} — final return{" "}
            {fmtPct(delisting.final_return)} — {delisting.reason}
          </div>
        )}
      </div>

      <Section title="Coverage">
        <dl className="grid grid-cols-2 gap-4 md:grid-cols-4">
          <Stat label="Covering analysts" value={detail.coverage_analysts} />
          <Stat label="Rating events" value={detail.rating_event_count} />
          <Stat
            label="Price sessions"
            value={`${detail.price_coverage.n_rows} (${fmtDate(detail.price_coverage.first_session)} → ${fmtDate(detail.price_coverage.last_session)})`}
          />
          <Stat
            label="Latest classification"
            value={
              cls
                ? `${cls.sector_id} · ${cls.cap_bucket} · ${fmtDate(cls.from_date)} → ${cls.to_date ? fmtDate(cls.to_date) : "open"}`
                : "—"
            }
          />
        </dl>
      </Section>

      <Section title="Identifiers">
        <DataTable<IdentifierSpell>
          columns={identifierColumns}
          data={detail.identifiers}
          caption="Security identifiers"
          emptyTitle="No identifiers"
        />
      </Section>

      <Section title="Classification spells">
        <DataTable<ClassificationSpell>
          columns={spellColumns}
          data={detail.classification_spells}
          caption="Classification spells"
          emptyTitle="No classification spells"
        />
      </Section>

      {perf && runId && (
        <Section
          title="Run performance"
          aside={
            <ProvenanceLabel
              run_id={perf.run.run_id}
              run_status={perf.run.status}
              methodology_fingerprint={perf.run.methodology_fingerprint}
              as_of_date={perf.run.as_of_date}
              knowledge_cutoff={perf.run.knowledge_cutoff}
              algorithm_version={perf.run.algorithm_version}
            />
          }
        >
          <div className="grid grid-cols-2 gap-4 md:grid-cols-5">
            <MetricCard title="Ratings">
              <span className="font-heading text-2xl font-semibold">{perf.n_ratings}</span>
              <span className="text-xs text-(--color-neutral-600)">
                {perf.n_investable} investable
              </span>
            </MetricCard>
            <MetricCard title="Mean excess">
              <span className="font-heading text-2xl font-semibold">
                {fmtPct(perf.mean_excess_return)}
              </span>
            </MetricCard>
            <MetricCard title="Hit rate">
              <span className="font-heading text-2xl font-semibold">{fmtPct(perf.hit_rate)}</span>
            </MetricCard>
            <MetricCard title="Mean contribution">
              <span className="font-heading text-2xl font-semibold">
                {fmtPct(perf.mean_contribution)}
              </span>
            </MetricCard>
            <MetricCard title="Target hit rate">
              <span className="font-heading text-2xl font-semibold">
                {fmtPct(perf.target_hit_rate)}
              </span>
              <span className="text-xs text-(--color-neutral-600)">
                {perf.n_target_outcomes} outcomes
              </span>
            </MetricCard>
          </div>
          {perf.by_horizon.length > 0 && (
            <DataTable<HorizonAggregate>
              columns={horizonColumns}
              data={perf.by_horizon}
              caption="Performance by horizon"
            />
          )}
        </Section>
      )}

      <Section
        title="Price history"
        aside={
          <label className="flex items-center gap-2 text-xs text-(--color-neutral-600)">
            Interval
            <select
              className="input py-1 text-xs"
              value={interval}
              onChange={(e) => setInterval(e.target.value)}
              aria-label="Price interval"
            >
              {PRICE_INTERVALS.map((i) => (
                <option key={i} value={i}>
                  {i}
                </option>
              ))}
            </select>
          </label>
        }
      >
        {pricesQuery.isPending ? (
          <LoadingState label="Loading prices" />
        ) : pricesQuery.isError ? (
          <ErrorState
            error={pricesQuery.error}
            onRetry={() => void pricesQuery.refetch()}
            title="Failed to load prices"
          />
        ) : (
          <>
            {pricesQuery.data?.downsampled && (
              <p className="text-xs text-(--color-neutral-600)">
                Showing {pricesQuery.data.rows.length} of {pricesQuery.data.source_rows} bars
                (downsampled).
              </p>
            )}
            <LineChart
              title="Adjusted close"
              data={pricePoints}
              height={240}
              formatY={(v) => v.toFixed(2)}
            />
          </>
        )}
      </Section>

      <Section
        title="Analyst consensus"
        aside={
          consensusQuery.data?.run_id ? (
            <ProvenanceLabel run_id={consensusQuery.data.run_id} />
          ) : undefined
        }
      >
        {consensusQuery.isPending ? (
          <LoadingState label="Loading consensus" />
        ) : consensusQuery.isError ? (
          <ErrorState
            error={consensusQuery.error}
            onRetry={() => void consensusQuery.refetch()}
            title="Failed to load consensus"
          />
        ) : consensusPoints.every((p) => p.y === null) ? (
          <EmptyState
            title="No consensus data"
            description="No analyst consensus has been recorded for this security."
          />
        ) : (
          <LineChart
            title="Consensus score"
            data={consensusPoints}
            height={200}
            formatY={(v) => v.toFixed(2)}
          />
        )}
        {targetOutcomes.length > 0 && (
          <DataTable<TargetOutcomeItem>
            columns={outcomeColumns}
            data={targetOutcomes}
            caption="Target outcomes (active run)"
          />
        )}
      </Section>

      <Section
        title="Rating events"
        aside={
          ratingsQuery.data ? (
            <span className="text-xs text-(--color-neutral-600)">
              showing {ratings.length} of {ratingsQuery.data.pagination.total}
              {runId ? " (run-scoped)" : ""}
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
          <DataTable<RatingEventItem>
            columns={ratingColumns}
            data={ratings}
            caption="Rating events"
            emptyTitle="No rating events"
            emptyDescription="No analyst has rated this security."
          />
        )}
      </Section>
    </div>
  );
}
