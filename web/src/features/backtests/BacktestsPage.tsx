import { useQuery } from "@tanstack/react-query";
import { Link, useNavigate, useSearch } from "@tanstack/react-router";
import { useState } from "react";
import { DataTable, type DataTableColumnDef } from "../../components/DataTable";
import type { BacktestSummary } from "./api";
import { type BacktestSubmitResponse, fetchBacktestDetail, useBacktests } from "./api";
import { BacktestBuilder } from "./BacktestBuilder";
import { BenchmarkPanel } from "./BenchmarkPanel";
import { formatTimestamp, StatusBadge, summarizeSpec } from "./common";

type Tab = "backtests" | "benchmarks";

const VALID_TABS = new Set<Tab>(["backtests", "benchmarks"]);

function Tabs({ tab, onChange }: { tab: Tab; onChange: (t: Tab) => void }) {
  return (
    <div
      className="flex gap-1 border-b border-(--color-divider)"
      role="tablist"
      aria-label="Section"
    >
      {(["backtests", "benchmarks"] as const).map((t) => (
        <button
          key={t}
          type="button"
          role="tab"
          aria-selected={tab === t}
          className={`px-3 py-1.5 text-sm ${
            tab === t
              ? "border-b-2 border-(--color-accent) font-medium text-(--color-accent)"
              : "text-(--color-neutral-600) hover:text-(--color-text)"
          }`}
          onClick={() => onChange(t)}
        >
          {t === "backtests" ? "Backtests" : "Benchmarks"}
        </button>
      ))}
    </div>
  );
}

/** Lazily-loaded spec summary for one list row (detail endpoint, cached). */
function SpecSummaryCell({ backtest }: { backtest: BacktestSummary }) {
  const active = backtest.status === "queued" || backtest.status === "running";
  const detail = useQuery({
    queryKey: ["backtest", { backtest_id: backtest.backtest_id }],
    queryFn: () => fetchBacktestDetail(backtest.backtest_id),
    enabled: !active,
    staleTime: 30_000,
    retry: 1,
  });
  if (active) return <span className="text-xs text-(--color-neutral-500)">pending…</span>;
  if (detail.isPending) return <span className="text-xs text-(--color-neutral-500)">…</span>;
  if (detail.isError) return <span className="text-xs text-(--color-neutral-500)">—</span>;
  return (
    <span className="text-xs" title={JSON.stringify(detail.data.spec ?? {})}>
      {summarizeSpec(detail.data.spec)}
    </span>
  );
}

/**
 * `/backtests` — submitted strategy backtests (newest first) plus the custom
 * benchmark sandbox on the `?tab=benchmarks` tab. Backtests are point-in-time
 * strategies, not run-scoped: `run_id` is preserved in the URL for nav
 * consistency but never sent to backtest endpoints.
 */
export function BacktestsPage() {
  const navigate = useNavigate();
  const search = useSearch({ strict: false }) as { tab?: string; run_id?: string };
  const tab: Tab =
    search.tab && VALID_TABS.has(search.tab as Tab) ? (search.tab as Tab) : "backtests";
  const [showBuilder, setShowBuilder] = useState(false);
  const backtests = useBacktests({ limit: 200 });

  const setTab = (t: Tab) =>
    void navigate({
      to: ".",
      search: (prev: Record<string, unknown>) => ({
        ...prev,
        tab: t === "backtests" ? undefined : t,
      }),
      replace: true,
    });

  const onSubmitted = (result: BacktestSubmitResponse) => {
    setShowBuilder(false);
    void navigate({
      to: "/backtests/$backtestId",
      params: { backtestId: result.backtest_id },
      search: (prev) => ({ run_id: prev.run_id }),
    });
  };

  const columns: DataTableColumnDef<BacktestSummary>[] = [
    {
      accessorKey: "name",
      header: "Name",
      cell: ({ row }) => (
        <Link
          to="/backtests/$backtestId"
          params={{ backtestId: row.original.backtest_id }}
          search={(prev) => ({ run_id: prev.run_id })}
          className="text-(--color-accent) hover:underline"
        >
          {row.original.name ?? row.original.backtest_id}
        </Link>
      ),
    },
    {
      accessorKey: "backtest_id",
      header: "ID",
      cell: ({ row }) => (
        <code className="text-[11px] text-(--color-neutral-500)">{row.original.backtest_id}</code>
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
      // The list endpoint omits the spec; fetch the detail lazily per row
      // (terminal rows only — queued/running rows show a placeholder).
      cell: ({ row }) => <SpecSummaryCell backtest={row.original} />,
    },
    {
      accessorKey: "benchmark_id",
      header: "Benchmark",
      cell: ({ row }) =>
        row.original.benchmark_id ? (
          <code className="text-[11px]">{row.original.benchmark_id}</code>
        ) : (
          "—"
        ),
    },
    {
      accessorKey: "created_at",
      header: "Created",
      cell: ({ row }) => formatTimestamp(row.original.created_at),
    },
    {
      id: "error",
      header: "Error",
      cell: ({ row }) =>
        row.original.error ? (
          <span className="text-xs text-(--color-neutral-700)" title={row.original.error.message}>
            {row.original.error.type}: {row.original.error.message.slice(0, 80)}
          </span>
        ) : (
          "—"
        ),
    },
  ];

  return (
    <div className="flex flex-col gap-4">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="font-heading text-2xl font-semibold">Backtests</h1>
          <p className="text-sm text-(--color-neutral-600)">
            Point-in-time strategy simulations over analyst rating signals, plus the custom
            benchmark sandbox.
          </p>
        </div>
        {tab === "backtests" && (
          <button
            type="button"
            className="btn btn-primary"
            onClick={() => setShowBuilder((v) => !v)}
          >
            {showBuilder ? "Hide builder" : "New backtest"}
          </button>
        )}
      </div>

      <Tabs tab={tab} onChange={setTab} />

      {tab === "backtests" && (
        <>
          {showBuilder && (
            <BacktestBuilder onSubmitted={onSubmitted} onCancel={() => setShowBuilder(false)} />
          )}

          <DataTable
            columns={columns}
            data={backtests.data?.data ?? []}
            loading={backtests.isPending}
            error={backtests.error}
            onRetry={() => void backtests.refetch()}
            emptyTitle="No backtests yet"
            emptyDescription="Submit a backtest to simulate a rating-signal strategy over historical sessions."
            caption="Submitted backtests"
          />
        </>
      )}

      {tab === "benchmarks" && <BenchmarkPanel />}
    </div>
  );
}
