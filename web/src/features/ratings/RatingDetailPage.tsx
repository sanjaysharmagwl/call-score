import { Link } from "@tanstack/react-router";
import { type ReactNode, useMemo } from "react";
import type { Schemas } from "../../api/query";
import { ProvenanceLabel } from "../../components/Badges";
import type { DataTableColumnDef } from "../../components/DataTable";
import { DataTable } from "../../components/DataTable";
import { EmptyState, ErrorState, LoadingState } from "../../components/states";
import { ActionBadge, CorrectionBadge, RatingBadge } from "../analysts/badges";
import { fmtDate, fmtNum, fmtPct, fmtSignedPct, fmtTs, humanize } from "../analysts/format";
import { useActiveRun } from "../shared/run-state";
import { useRatingDetail } from "./queries";

type ExposureLeg = Schemas["ExposureLeg"];
type DictRow = Record<string, unknown>;

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

function strOf(row: DictRow, key: string): string | null {
  const v = row[key];
  return v === null || v === undefined ? null : String(v);
}

function numOf(row: DictRow, key: string): number | null {
  const v = row[key];
  return typeof v === "number" && !Number.isNaN(v) ? v : null;
}

function boolOf(row: DictRow, key: string): boolean | null {
  const v = row[key];
  return typeof v === "boolean" ? v : null;
}

function formatPriceTarget(event: DictRow | null): string {
  const pt = event?.price_target;
  if (pt === null || pt === undefined) return "—";
  const n = typeof pt === "number" ? pt : Number(pt);
  if (Number.isNaN(n)) return "—";
  const ccy = event?.pt_currency ? String(event.pt_currency) : "";
  return `${fmtNum(n, 2)} ${ccy}`.trim();
}

/** Render an arbitrary JSON-ish row as a definition list (event_window, target_outcome). */
function DictCard({ title, row }: { title: string; row: DictRow | null | undefined }) {
  if (!row) {
    return (
      <Section title={title}>
        <EmptyState title="No data" description="Nothing was recorded for this event." />
      </Section>
    );
  }
  const pctKeys = new Set(["implied_return", "realized_return"]);
  const entries = Object.entries(row).filter(
    ([k]) => k !== "run_id" && k !== "event_id" && k !== "analyst_id",
  );
  return (
    <Section title={title}>
      <dl className="grid grid-cols-2 gap-4 md:grid-cols-3">
        {entries.map(([k, v]) => {
          let display: ReactNode = "—";
          if (v === null || v === undefined) {
            display = "—";
          } else if (typeof v === "boolean") {
            display = v ? "yes" : "no";
          } else if (typeof v === "number") {
            display = pctKeys.has(k) ? fmtPct(v, 1) : fmtNum(v, 4);
          } else {
            display = String(v);
          }
          return <Stat key={k} label={humanize(k)} value={display} />;
        })}
      </dl>
    </Section>
  );
}

/** `/analysts/$analystId/ratings/$intervalId` — full rating explanation. */
export function RatingDetailPage({
  analystId,
  intervalId,
}: {
  analystId: string;
  intervalId: string;
}) {
  const { runId } = useActiveRun();
  const detailQuery = useRatingDetail(analystId, intervalId, runId);
  const detail = detailQuery.data;

  const returnColumns = useMemo<DataTableColumnDef<DictRow>[]>(
    () => [
      { id: "horizon", header: "Horizon", accessorFn: (r) => strOf(r, "horizon") ?? "" },
      {
        id: "window",
        header: "Window",
        accessorFn: (r) =>
          `${strOf(r, "start_session") ?? "?"} → ${strOf(r, "end_session") ?? "open"}`,
        enableSorting: false,
      },
      {
        id: "entry_price",
        header: "Entry px",
        accessorFn: (r) => numOf(r, "entry_price"),
        cell: (ctx) => fmtNum(numOf(ctx.row.original, "entry_price"), 2),
      },
      {
        id: "exit_price",
        header: "Exit px",
        accessorFn: (r) => numOf(r, "exit_price"),
        cell: (ctx) => fmtNum(numOf(ctx.row.original, "exit_price"), 2),
      },
      {
        id: "stock_return",
        header: "Stock",
        accessorFn: (r) => numOf(r, "stock_return"),
        cell: (ctx) => fmtSignedPct(numOf(ctx.row.original, "stock_return")),
      },
      {
        id: "benchmark_return",
        header: "Benchmark",
        accessorFn: (r) => numOf(r, "benchmark_return"),
        cell: (ctx) => (
          <span title={strOf(ctx.row.original, "benchmark_id") ?? undefined}>
            {fmtSignedPct(numOf(ctx.row.original, "benchmark_return"))}
          </span>
        ),
      },
      {
        id: "excess_return",
        header: "Excess",
        accessorFn: (r) => numOf(r, "excess_return"),
        cell: (ctx) => fmtSignedPct(numOf(ctx.row.original, "excess_return")),
      },
      {
        id: "terminated",
        header: "Delisted",
        accessorFn: (r) => boolOf(r, "terminated_by_delisting"),
        enableSorting: false,
        cell: (ctx) => (boolOf(ctx.row.original, "terminated_by_delisting") ? "yes" : "—"),
      },
      {
        id: "unavailable_reason",
        header: "Note",
        accessorFn: (r) => strOf(r, "unavailable_reason") ?? "",
        enableSorting: false,
        cell: (ctx) =>
          ctx.row.original.unavailable_reason ? String(ctx.row.original.unavailable_reason) : "—",
      },
    ],
    [],
  );

  const contributionColumns = useMemo<DataTableColumnDef<DictRow>[]>(
    () => [
      { id: "horizon", header: "Horizon", accessorFn: (r) => strOf(r, "horizon") ?? "" },
      {
        id: "position_weight",
        header: "Weight",
        accessorFn: (r) => numOf(r, "position_weight"),
        cell: (ctx) => fmtNum(numOf(ctx.row.original, "position_weight"), 4),
      },
      {
        id: "excess_return",
        header: "Excess",
        accessorFn: (r) => numOf(r, "excess_return"),
        cell: (ctx) => fmtSignedPct(numOf(ctx.row.original, "excess_return")),
      },
      {
        id: "contribution",
        header: "Contribution",
        accessorFn: (r) => numOf(r, "contribution"),
        cell: (ctx) => fmtSignedPct(numOf(ctx.row.original, "contribution")),
      },
      {
        id: "unavailable_reason",
        header: "Note",
        accessorFn: (r) => strOf(r, "unavailable_reason") ?? "",
        enableSorting: false,
        cell: (ctx) =>
          ctx.row.original.unavailable_reason ? String(ctx.row.original.unavailable_reason) : "—",
      },
    ],
    [],
  );

  const exposureColumns = useMemo<DataTableColumnDef<ExposureLeg>[]>(
    () => [
      { id: "segment_no", header: "Seg", accessorKey: "segment_no" },
      {
        id: "start",
        header: "Start",
        accessorFn: (row) => `${row.start_session} ${row.start_point}`,
        cell: (ctx) => {
          const row = ctx.row.original;
          return `${fmtDate(row.start_session)} ${row.start_point}`;
        },
      },
      {
        id: "end",
        header: "End",
        accessorFn: (row) => `${row.end_session} ${row.end_point}`,
        cell: (ctx) => {
          const row = ctx.row.original;
          return `${fmtDate(row.end_session)} ${row.end_point}`;
        },
      },
      {
        id: "position_weight",
        header: "Weight",
        accessorFn: (row) => row.position_weight ?? null,
        cell: (ctx) => fmtNum(ctx.row.original.position_weight, 4),
      },
      { id: "weight_source", header: "Source", accessorKey: "weight_source" },
    ],
    [],
  );

  if (detailQuery.isPending) {
    return <LoadingState label="Loading rating" />;
  }
  if (detailQuery.isError || !detail) {
    return (
      <ErrorState
        error={detailQuery.error}
        onRetry={() => void detailQuery.refetch()}
        title="Failed to load rating"
      />
    );
  }

  const event = (detail.event ?? null) as DictRow | null;
  const eventWindow = (detail.event_window ?? null) as DictRow | null;
  const targetOutcome = (detail.target_outcome ?? null) as DictRow | null;
  const isCorrection = boolOf(event ?? {}, "is_correction") === true;
  const supersedes = event ? strOf(event, "supersedes_event_id") : null;

  return (
    <div className="flex flex-col gap-4">
      <div className="flex flex-col gap-1">
        <div className="flex flex-wrap items-center gap-3">
          <Link
            to="/securities/$securityId"
            params={{ securityId: detail.security_id }}
            search={(prev) => ({ run_id: prev.run_id })}
            className="font-heading text-2xl font-semibold text-(--color-accent) hover:underline"
          >
            {detail.security_id}
          </Link>
          <ActionBadge action={detail.action_type} />
          <RatingBadge rating={detail.canonical_rating} />
          {isCorrection && <CorrectionBadge />}
          {!detail.is_investable && (
            <span className="tag tag-outline" data-testid="non-investable-badge">
              not investable
            </span>
          )}
        </div>
        <div className="flex flex-wrap items-center gap-x-4 gap-y-1 text-sm text-(--color-neutral-700)">
          <span className="font-mono text-xs">{detail.interval_id}</span>
          <span className="font-mono text-xs text-(--color-neutral-500)">
            analyst {detail.analyst_id}
          </span>
          {detail.native_rating && detail.native_rating !== detail.canonical_rating && (
            <span className="text-xs text-(--color-neutral-600)">
              native: {detail.native_rating}
            </span>
          )}
          {detail.flags && (
            <span className="text-xs text-(--color-neutral-600)">{detail.flags}</span>
          )}
        </div>
        {detail.non_investable_reason && (
          <p className="text-sm text-(--color-neutral-600)">
            Not investable: {detail.non_investable_reason}
          </p>
        )}
      </div>

      <Section title="Explanation" aside={<ProvenanceLabel run_id={detail.provenance?.run_id} />}>
        <p className="text-base leading-relaxed" data-testid="rating-explanation">
          {detail.explanation}
        </p>
      </Section>

      <Section title="Event">
        <dl className="grid grid-cols-2 gap-4 md:grid-cols-4">
          <Stat label="Native rating" value={detail.native_rating} />
          <Stat label="Canonical rating" value={humanize(detail.canonical_rating)} />
          <Stat
            label="Prior canonical"
            value={
              event?.prior_canonical_rating ? humanize(String(event.prior_canonical_rating)) : "—"
            }
          />
          <Stat label="Action" value={humanize(detail.action_type)} />
          <Stat label="Announced" value={fmtTs(detail.announced_ts)} />
          <Stat label="Effective" value={fmtTs(detail.effective_ts)} />
          <Stat label="Knowledge ts" value={fmtTs(detail.knowledge_ts)} />
          <Stat label="Available" value={fmtTs(detail.available_ts)} />
          <Stat label="Price target" value={formatPriceTarget(event)} />
          <Stat
            label="PT horizon"
            value={event?.pt_horizon_months ? `${event.pt_horizon_months}m` : "—"}
          />
          <Stat
            label="Expiry"
            value={fmtDate(detail.expiry_date ?? strOf(event ?? {}, "expiry_date"))}
          />
          <Stat label="Scale" value={event ? String(event.scale_id ?? "—") : "—"} />
          <Stat label="Source" value={event ? String(event.source ?? "—") : "—"} />
          <Stat label="Correction" value={isCorrection ? "yes" : "no"} />
          <Stat
            label="Supersedes"
            value={
              supersedes ? (
                <span className="font-mono text-xs" title={supersedes}>
                  {supersedes}
                </span>
              ) : (
                "—"
              )
            }
          />
        </dl>
      </Section>

      <Section title="Interval legs">
        <dl className="grid grid-cols-2 gap-4 md:grid-cols-4">
          <Stat label="Entry" value={`${fmtDate(detail.entry_session)} ${detail.entry_point}`} />
          <Stat label="Entry ts" value={fmtTs(detail.entry_ts)} />
          <Stat label="Exit" value={`${fmtDate(detail.exit_session)} ${detail.exit_point}`} />
          <Stat label="Exit ts" value={fmtTs(detail.exit_ts)} />
          <Stat label="Exit reason" value={humanize(detail.exit_reason)} />
          <Stat label="Expiry" value={fmtDate(detail.expiry_date)} />
          <Stat label="Decay start" value={fmtDate(detail.decay_start_session)} />
          <Stat label="Investable" value={detail.is_investable ? "yes" : "no"} />
        </dl>
        {detail.exposures.length > 0 && (
          <DataTable<ExposureLeg>
            columns={exposureColumns}
            data={detail.exposures as unknown as ExposureLeg[]}
            caption="Exposure legs"
          />
        )}
      </Section>

      <Section title="Returns">
        <DataTable<DictRow>
          columns={returnColumns}
          data={detail.returns as DictRow[]}
          caption="Interval returns by horizon"
          emptyTitle="No return legs"
          emptyDescription="No returns were computed for this interval."
        />
      </Section>

      <Section title="Contributions">
        <DataTable<DictRow>
          columns={contributionColumns}
          data={detail.contributions as DictRow[]}
          caption="Interval contributions by horizon"
          emptyTitle="No contributions"
          emptyDescription="No contribution rows were stored for this interval."
        />
      </Section>

      <DictCard title="Event window" row={eventWindow} />
      <DictCard title="Target outcome" row={targetOutcome} />

      <p className="text-xs text-(--color-neutral-500)">
        Prices are split-adjusted execution levels; returns use the run's configured benchmark and
        horizon convention.
      </p>
    </div>
  );
}
