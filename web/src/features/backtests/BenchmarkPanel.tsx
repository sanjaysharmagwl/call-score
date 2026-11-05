import { type FormEvent, useState } from "react";
import { LineChart } from "../../components/charts";
import { DataTable, type DataTableColumnDef } from "../../components/DataTable";
import { ErrorState, LoadingState } from "../../components/states";
import { formatApiError } from "../methodologies/api";
import { useSectors } from "../shared/queries";
import {
  type BenchmarkSummary,
  useBenchmarkSeries,
  useBenchmarks,
  useCreateBenchmark,
  useMaterializeBenchmark,
} from "./api";
import { formatNum, StatusBadge } from "./common";
import {
  BENCHMARK_PRESETS,
  BENCHMARK_REBALANCES,
  BENCHMARK_WEIGHTINGS,
  type BenchmarkFormState,
  buildBenchmarkRequest,
  defaultBenchmarkForm,
  validateBenchmarkForm,
} from "./spec";

function BenchmarkCreateForm({ onCreated }: { onCreated?: () => void }) {
  const [state, setState] = useState<BenchmarkFormState>(defaultBenchmarkForm);
  const [clientErrors, setClientErrors] = useState<string[]>([]);
  const create = useCreateBenchmark();
  const sectors = useSectors({ limit: 500 });
  const sectorOptions = sectors.data?.data ?? [];

  const patch = (partial: Partial<BenchmarkFormState>) =>
    setState((prev) => ({ ...prev, ...partial }));

  const onSubmit = (event: FormEvent) => {
    event.preventDefault();
    const errors = validateBenchmarkForm(state);
    if (errors.length > 0) {
      setClientErrors(errors);
      return;
    }
    setClientErrors([]);
    create.mutate(buildBenchmarkRequest(state), {
      onSuccess: () => {
        setState(defaultBenchmarkForm());
        onCreated?.();
      },
    });
  };

  return (
    <form
      onSubmit={onSubmit}
      className="flex flex-col gap-3 border border-(--color-divider) bg-(--color-surface) p-4"
      data-testid="benchmark-form"
    >
      <h3 className="text-sm font-semibold">New custom benchmark</h3>
      <div className="grid grid-cols-3 gap-3">
        <label className="flex flex-col gap-1 text-xs text-(--color-neutral-600)">
          Name (optional)
          <input
            className="input"
            value={state.name}
            onChange={(e) => patch({ name: e.target.value })}
            aria-label="Benchmark name"
          />
        </label>
        <label className="flex flex-col gap-1 text-xs text-(--color-neutral-600)">
          Preset
          <select
            className="input"
            value={state.preset}
            aria-label="Preset"
            onChange={(e) => patch({ preset: e.target.value as BenchmarkFormState["preset"] })}
          >
            {BENCHMARK_PRESETS.map((p) => (
              <option key={p} value={p}>
                {p}
              </option>
            ))}
          </select>
        </label>
        <label className="flex flex-col gap-1 text-xs text-(--color-neutral-600)">
          Weighting
          <select
            className="input"
            value={state.weighting}
            aria-label="Weighting"
            onChange={(e) =>
              patch({ weighting: e.target.value as BenchmarkFormState["weighting"] })
            }
          >
            {BENCHMARK_WEIGHTINGS.map((w) => (
              <option key={w} value={w}>
                {w}
              </option>
            ))}
          </select>
        </label>
        <label className="flex flex-col gap-1 text-xs text-(--color-neutral-600)">
          Rebalance
          <select
            className="input"
            value={state.rebalance}
            aria-label="Rebalance"
            onChange={(e) =>
              patch({ rebalance: e.target.value as BenchmarkFormState["rebalance"] })
            }
          >
            {BENCHMARK_REBALANCES.map((r) => (
              <option key={r} value={r}>
                {r}
              </option>
            ))}
          </select>
        </label>
        {state.preset === "sector_neutral" && (
          <label className="flex flex-col gap-1 text-xs text-(--color-neutral-600)">
            Long-leg sector (required)
            <select
              className="input"
              value={state.sectorId}
              aria-label="Sector neutral long leg"
              onChange={(e) => patch({ sectorId: e.target.value })}
            >
              <option value="">Select…</option>
              {sectorOptions.map((s) => (
                <option key={s.sector_id} value={s.sector_id}>
                  {s.name} ({s.sector_id})
                </option>
              ))}
            </select>
          </label>
        )}
        <label className="flex flex-col gap-1 text-xs text-(--color-neutral-600)">
          Universe sector (optional)
          <select
            className="input"
            value={state.universeSectorId}
            aria-label="Universe sector"
            onChange={(e) => patch({ universeSectorId: e.target.value })}
          >
            <option value="">All sectors</option>
            {sectorOptions.map((s) => (
              <option key={s.sector_id} value={s.sector_id}>
                {s.name} ({s.sector_id})
              </option>
            ))}
          </select>
        </label>
        <label className="flex flex-col gap-1 text-xs text-(--color-neutral-600)">
          Cap bucket (optional)
          <input
            className="input"
            value={state.capBucket}
            onChange={(e) => patch({ capBucket: e.target.value })}
            aria-label="Cap bucket"
            placeholder="e.g. large"
          />
        </label>
        <label className="flex flex-col gap-1 text-xs text-(--color-neutral-600)">
          Region (optional)
          <input
            className="input"
            value={state.region}
            onChange={(e) => patch({ region: e.target.value })}
            aria-label="Region"
            placeholder="e.g. US"
          />
        </label>
        <label className="flex flex-col gap-1 text-xs text-(--color-neutral-600)">
          Security ids (optional, comma-separated)
          <input
            className="input"
            value={state.securityIds}
            onChange={(e) => patch({ securityIds: e.target.value })}
            aria-label="Security ids"
            placeholder="SEC_1, SEC_2"
          />
        </label>
        <label className="flex flex-col gap-1 text-xs text-(--color-neutral-600)">
          Start date
          <input
            className="input"
            type="date"
            value={state.startDate}
            aria-label="Benchmark start date"
            onChange={(e) => patch({ startDate: e.target.value })}
          />
        </label>
        <label className="flex flex-col gap-1 text-xs text-(--color-neutral-600)">
          End date
          <input
            className="input"
            type="date"
            value={state.endDate}
            aria-label="Benchmark end date"
            onChange={(e) => patch({ endDate: e.target.value })}
          />
        </label>
      </div>
      <label className="flex items-center gap-2 text-xs text-(--color-neutral-700)">
        <input
          type="checkbox"
          checked={state.includeDelisted}
          onChange={(e) => patch({ includeDelisted: e.target.checked })}
        />
        Include delisted securities in the universe
      </label>

      {clientErrors.length > 0 && (
        <ul className="list-disc pl-5 text-sm text-(--color-neutral-700)" role="alert">
          {clientErrors.map((e) => (
            <li key={e}>{e}</li>
          ))}
        </ul>
      )}
      {create.isError && (
        <p
          className="text-sm text-(--color-neutral-700)"
          role="alert"
          data-testid="benchmark-error"
        >
          {formatApiError(create.error)}
        </p>
      )}

      <div>
        <button type="submit" className="btn btn-primary" disabled={create.isPending}>
          {create.isPending ? "Creating…" : "Create benchmark"}
        </button>
      </div>
    </form>
  );
}

function SeriesPreview({ benchmarkId, label }: { benchmarkId: string; label: string }) {
  const series = useBenchmarkSeries(benchmarkId);
  if (series.isPending) return <LoadingState label="Loading series" />;
  if (series.isError) return <ErrorState error={series.error} title="Failed to load series" />;
  const rows = series.data?.data ?? [];
  if (rows.length === 0) {
    return <p className="text-sm text-(--color-neutral-600)">No materialized rows yet.</p>;
  }
  return (
    <LineChart
      title={`${label} — close level`}
      data={rows.map((r) => ({ x: r.session_date, y: r.close_level }))}
      height={180}
      width={640}
      formatY={(v) => v.toFixed(1)}
    />
  );
}

/**
 * Benchmarks panel (`/backtests?tab=benchmarks`): custom benchmark defs with
 * create form, synchronous materialize, and a series preview chart.
 */
export function BenchmarkPanel() {
  const [showCreate, setShowCreate] = useState(false);
  const [previewId, setPreviewId] = useState<string | null>(null);
  const benchmarks = useBenchmarks({ limit: 200 });
  const materialize = useMaterializeBenchmark();
  const [materializeNote, setMaterializeNote] = useState<string | null>(null);

  const columns: DataTableColumnDef<BenchmarkSummary>[] = [
    {
      accessorKey: "name",
      header: "Name",
      cell: ({ row }) => (
        <span className="flex items-center gap-2">
          <button
            type="button"
            className="text-left text-(--color-accent) hover:underline"
            onClick={() =>
              setPreviewId((prev) =>
                prev === row.original.benchmark_id ? null : row.original.benchmark_id,
              )
            }
          >
            {row.original.name ?? row.original.benchmark_id}
          </button>
          <code className="text-[11px] text-(--color-neutral-500)">
            {row.original.benchmark_id}
          </code>
        </span>
      ),
    },
    {
      accessorKey: "status",
      header: "Status",
      cell: ({ row }) => <StatusBadge status={row.original.status} />,
    },
    {
      id: "spec",
      header: "Spec",
      cell: ({ row }) => {
        // List rows carry preset/weighting/rebalance columns, not the spec.
        const parts = [row.original.preset, row.original.weighting, row.original.rebalance].filter(
          Boolean,
        );
        return <span className="text-xs">{parts.join(" · ") || "—"}</span>;
      },
    },
    {
      accessorKey: "row_count",
      header: "Rows",
      cell: ({ row }) => formatNum(row.original.row_count, 0),
    },
    {
      id: "actions",
      header: "",
      cell: ({ row }) => (
        <button
          type="button"
          className="btn btn-secondary"
          disabled={materialize.isPending}
          onClick={() => {
            setMaterializeNote(null);
            materialize.mutate(row.original.benchmark_id, {
              onSuccess: (result) =>
                setMaterializeNote(
                  `${row.original.benchmark_id}: materialized ${result.row_count} rows as of ${result.as_of_date} (rev ${result.revision})`,
                ),
              onError: (err) => setMaterializeNote(`Materialize failed: ${formatApiError(err)}`),
            });
          }}
        >
          {materialize.isPending && materialize.variables === row.original.benchmark_id
            ? "Materializing…"
            : "Materialize"}
        </button>
      ),
    },
  ];

  const previewRow = benchmarks.data?.data.find((b) => b.benchmark_id === previewId);

  return (
    <div className="flex flex-col gap-4">
      <div className="flex items-center justify-between">
        <p className="text-sm text-(--color-neutral-600)">
          Custom total-return benchmarks built from the security universe. Materialize to publish
          levels, then select one as a backtest benchmark.
        </p>
        <button type="button" className="btn btn-primary" onClick={() => setShowCreate((v) => !v)}>
          {showCreate ? "Hide new benchmark" : "New benchmark"}
        </button>
      </div>

      {materializeNote && (
        <p
          className="text-sm text-(--color-neutral-700)"
          role="status"
          data-testid="materialize-note"
        >
          {materializeNote}
        </p>
      )}
      {materialize.isError && !materializeNote && (
        <p className="text-sm text-(--color-neutral-700)" role="alert">
          {formatApiError(materialize.error)}
        </p>
      )}

      {showCreate && <BenchmarkCreateForm />}

      <DataTable
        columns={columns}
        data={benchmarks.data?.data ?? []}
        loading={benchmarks.isPending}
        error={benchmarks.error}
        onRetry={() => void benchmarks.refetch()}
        emptyTitle="No custom benchmarks"
        emptyDescription="Create a benchmark definition, then materialize it to produce index levels."
        caption="Custom benchmarks"
      />

      {previewId && previewRow && (
        <section className="card elev-sm flex flex-col gap-2 p-4" data-testid="series-preview">
          <h3 className="card-title">
            {previewRow.name ?? previewRow.benchmark_id}{" "}
            <span className="text-xs text-(--color-neutral-500)">{previewRow.benchmark_id}</span>
          </h3>
          <SeriesPreview
            benchmarkId={previewId}
            label={previewRow.name ?? previewRow.benchmark_id}
          />
        </section>
      )}
    </div>
  );
}
