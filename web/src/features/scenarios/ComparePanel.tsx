import { shortFingerprint, shortRunId } from "../../api/query";
import { DataTable, type DataTableColumnDef } from "../../components/DataTable";
import { ErrorState, LoadingState } from "../../components/states";
import type { AnalystDelta, FamilyDelta, MetricDelta, ScenarioCompareResponse } from "./api";

/** Below this Spearman rank agreement the delta report is flagged. */
export const LOW_AGREEMENT_THRESHOLD = 0.8;

function fmtDelta(n: number | null | undefined, digits = 3): string {
  if (n === null || n === undefined) return "—";
  const s = n.toFixed(digits);
  return n > 0 ? `+${s}` : s;
}

function RunHeader({
  label,
  runId,
  status,
  asOf,
  fp,
}: {
  label: string;
  runId: string;
  status: string;
  asOf?: string | null;
  fp?: string | null;
}) {
  return (
    <div className="flex flex-col gap-0.5 text-xs">
      <span className="font-semibold uppercase tracking-[0.08em] text-(--color-neutral-500)">
        {label}
      </span>
      <span className="font-mono" title={runId}>
        {shortRunId(runId)} · {status}
      </span>
      {asOf && <span>as of {asOf}</span>}
      {fp && (
        <span className="font-mono" title={fp}>
          meth {shortFingerprint(fp)}
        </span>
      )}
    </div>
  );
}

const analystColumns: DataTableColumnDef<AnalystDelta>[] = [
  { accessorKey: "analyst_id", header: "Analyst" },
  {
    accessorKey: "composite_score_a",
    header: "Score A",
    cell: ({ row }) => row.original.composite_score_a?.toFixed(3) ?? "—",
  },
  {
    accessorKey: "composite_score_b",
    header: "Score B (what-if)",
    cell: ({ row }) => row.original.composite_score_b?.toFixed(3) ?? "—",
  },
  {
    accessorKey: "score_delta",
    header: "Δ score",
    cell: ({ row }) => fmtDelta(row.original.score_delta),
  },
  {
    accessorKey: "rank_overall_a",
    header: "Rank A",
    cell: ({ row }) => row.original.rank_overall_a ?? "—",
  },
  {
    accessorKey: "rank_overall_b",
    header: "Rank B",
    cell: ({ row }) => row.original.rank_overall_b ?? "—",
  },
  {
    accessorKey: "rank_delta",
    header: "Δ rank",
    cell: ({ row }) => fmtDelta(row.original.rank_delta, 0),
  },
  { accessorKey: "transition", header: "Transition" },
];

const familyColumns: DataTableColumnDef<FamilyDelta>[] = [
  { accessorKey: "family", header: "Family" },
  { accessorKey: "n_metrics", header: "Metrics" },
  {
    accessorKey: "mean_delta",
    header: "Mean Δ (b−a)",
    cell: ({ row }) => fmtDelta(row.original.mean_delta),
  },
];

const metricColumns: DataTableColumnDef<MetricDelta>[] = [
  { accessorKey: "metric_code", header: "Metric" },
  { accessorKey: "family", header: "Family" },
  { accessorKey: "n_pairs", header: "Pairs" },
  {
    accessorKey: "mean_delta",
    header: "Mean Δ",
    cell: ({ row }) => fmtDelta(row.original.mean_delta),
  },
  {
    accessorKey: "mean_abs_delta",
    header: "Mean |Δ|",
    cell: ({ row }) => fmtDelta(row.original.mean_abs_delta),
  },
];

function UniverseNote({ data }: { data: ScenarioCompareResponse }) {
  const u = data.universe;
  const aOnly = (u.a ?? []).filter((x) => !(u.intersection ?? []).includes(x));
  const bOnly = (u.b ?? []).filter((x) => !(u.intersection ?? []).includes(x));
  return (
    <p className="text-xs text-(--color-neutral-600)" data-testid="universe-note">
      Universe: {u.compatible ? "compatible" : "INCOMPATIBLE — filters differ"}
      {u.intersection ? ` · ${u.intersection.length} shared` : ""}
      {aOnly.length > 0 ? ` · ${aOnly.length} only in A` : ""}
      {bOnly.length > 0 ? ` · ${bOnly.length} only in B` : ""}
      {!u.compatible && " — interpret deltas with care"}
    </p>
  );
}

/**
 * Delta report between two runs (baseline/reference = A, what-if = B;
 * all deltas are B − A). Renders rank-agreement warning, universe
 * compatibility, transition counts, per-analyst score/rank deltas and
 * per-family/per-metric rollups.
 */
export function ComparePanel({
  data,
  isPending,
  error,
  onRetry,
  aLabel = "Baseline / reference run",
  bLabel = "What-if run",
}: {
  data: ScenarioCompareResponse | undefined;
  isPending: boolean;
  error: unknown;
  onRetry?: () => void;
  aLabel?: string;
  bLabel?: string;
}) {
  if (isPending) return <LoadingState label="Loading comparison" />;
  if (error)
    return <ErrorState error={error} onRetry={onRetry} title="Failed to load comparison" />;
  if (!data) return null;

  const spearman = data.rank_correlation.spearman;
  const lowAgreement =
    spearman !== null && spearman !== undefined && spearman < LOW_AGREEMENT_THRESHOLD;
  const s = data.summary;

  return (
    <div className="flex flex-col gap-4" data-testid="compare-panel">
      <div className="grid grid-cols-2 gap-4">
        <RunHeader
          label={aLabel}
          runId={data.run_a_id}
          status={data.run_a_status}
          asOf={data.as_of_date_a}
          fp={data.methodology_a_fingerprint}
        />
        <RunHeader
          label={bLabel}
          runId={data.run_b_id}
          status={data.run_b_status}
          asOf={data.as_of_date_b}
          fp={data.methodology_b_fingerprint}
        />
      </div>

      {lowAgreement && (
        <p
          role="alert"
          className="border border-(--color-accent) bg-(--color-accent-100) p-3 text-sm"
          data-testid="low-agreement-warning"
        >
          Low rank agreement: Spearman ρ = {spearman?.toFixed(3)} across {data.rank_correlation.n}{" "}
          commonly ranked analysts (&lt; {LOW_AGREEMENT_THRESHOLD}). The what-if changes analyst
          ordering materially — review per-analyst deltas before trusting aggregate movements.
        </p>
      )}

      <div className="flex flex-wrap items-center gap-4 text-sm">
        <span>
          Spearman rank correlation:{" "}
          <strong data-testid="spearman-value">
            {spearman === null || spearman === undefined ? "n/a" : spearman.toFixed(3)}
          </strong>{" "}
          (n={data.rank_correlation.n})
        </span>
        <UniverseNote data={data} />
      </div>

      <div className="flex flex-wrap gap-2 text-xs" data-testid="transition-counts">
        <span className="tag tag-neutral">analysts A: {s.n_analysts_a}</span>
        <span className="tag tag-neutral">analysts B: {s.n_analysts_b}</span>
        <span className="tag tag-neutral">common: {s.n_common}</span>
        <span className="tag tag-neutral">only in A: {s.only_in_a}</span>
        <span className="tag tag-neutral">only in B: {s.only_in_b}</span>
        <span className="tag tag-accent">ranked→ranked: {s.ranked_to_ranked}</span>
        <span className="tag tag-neutral">ranked→unranked: {s.ranked_to_unranked}</span>
        <span className="tag tag-neutral">unranked→ranked: {s.unranked_to_ranked}</span>
        <span className="tag tag-neutral">unranked→unranked: {s.unranked_to_unranked}</span>
      </div>

      <section>
        <h4 className="mb-1 text-xs font-semibold uppercase tracking-[0.08em] text-(--color-neutral-600)">
          Per-analyst deltas (B − A)
        </h4>
        <DataTable
          columns={analystColumns}
          data={data.analysts ?? []}
          emptyTitle="No analyst deltas"
          caption="Per-analyst score and rank deltas"
        />
      </section>

      <section>
        <h4 className="mb-1 text-xs font-semibold uppercase tracking-[0.08em] text-(--color-neutral-600)">
          Per-family deltas
        </h4>
        <DataTable
          columns={familyColumns}
          data={data.family_deltas ?? []}
          emptyTitle="No family deltas"
          caption="Per-family mean metric deltas"
        />
      </section>

      <section>
        <h4 className="mb-1 text-xs font-semibold uppercase tracking-[0.08em] text-(--color-neutral-600)">
          Key-metric deltas
        </h4>
        <DataTable
          columns={metricColumns}
          data={data.metric_deltas ?? []}
          emptyTitle="No metric deltas"
          caption="Per-metric mean deltas"
        />
      </section>
    </div>
  );
}
