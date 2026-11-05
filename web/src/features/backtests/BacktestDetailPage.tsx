import { useState } from "react";
import { buildCsvUrl } from "../../api/query";
import { LineChart } from "../../components/charts";
import { DataTable, type DataTableColumnDef } from "../../components/DataTable";
import { MetricCard, MetricValue } from "../../components/MetricCard";
import { EmptyState, ErrorState, LoadingState } from "../../components/states";
import {
  type BacktestDetail,
  type BacktestMonthlyRow,
  type BacktestNavRow,
  type BacktestStatRow,
  type BacktestTradeRow,
  type NavInterval,
  useBacktest,
  useBacktestAttribution,
  useBacktestMonthly,
  useBacktestNav,
  useBacktestStats,
  useBacktestTrades,
} from "./api";
import { formatNum, formatPct, formatTimestamp, StatusBadge } from "./common";

const PAGE_SIZE = 50;
const NAV_INTERVALS: NavInterval[] = ["1d", "1w", "1M"];
const NAV_SERIES = [
  { key: "nav", label: "NAV" },
  { key: "benchmark_nav", label: "Benchmark NAV" },
  { key: "drawdown", label: "Drawdown" },
  { key: "active_return", label: "Active return" },
] as const;
type NavSeriesKey = (typeof NAV_SERIES)[number]["key"];

interface StatSpec {
  label: string;
  /** "pct" renders value*100 with sign; "num" renders raw. */
  kind: "pct" | "num";
  digits?: number;
}

const STAT_SPECS: Record<string, StatSpec> = {
  annualized_return: { label: "Annualized return", kind: "pct" },
  annualized_volatility: { label: "Annualized volatility", kind: "pct" },
  sharpe_ratio: { label: "Sharpe", kind: "num", digits: 2 },
  information_ratio: { label: "Information ratio", kind: "num", digits: 2 },
  max_drawdown: { label: "Max drawdown", kind: "pct" },
  turnover: { label: "Turnover (mean daily)", kind: "num", digits: 4 },
  hit_rate: { label: "Hit rate", kind: "pct" },
  n_sessions: { label: "Sessions", kind: "num", digits: 0 },
};

function StatCards({ stats }: { stats: BacktestStatRow[] }) {
  if (stats.length === 0) {
    return <EmptyState title="No stats" description="Stats appear once the backtest succeeds." />;
  }
  return (
    <div className="grid grid-cols-4 gap-3" data-testid="stat-cards">
      {stats.map((row) => {
        const spec = STAT_SPECS[row.stat_code];
        const label = spec?.label ?? row.stat_code;
        const isPct = spec?.kind === "pct";
        return (
          <MetricCard key={row.stat_code} title={label}>
            <MetricValue
              value={row.value}
              ciLow={row.ci_low}
              ciHigh={row.ci_high}
              stdErr={row.std_err}
              digits={isPct ? 2 : (spec?.digits ?? 4)}
            />
            <span className="text-[11px] text-(--color-neutral-500)">
              {row.value === null || row.value === undefined
                ? "unavailable"
                : isPct
                  ? formatPct(row.value)
                  : formatNum(row.value, spec?.digits ?? 4)}
            </span>
          </MetricCard>
        );
      })}
    </div>
  );
}

function NavSection({ backtestId }: { backtestId: string }) {
  const [interval, setInterval] = useState<NavInterval>("1d");
  const [series, setSeries] = useState<NavSeriesKey>("nav");
  const nav = useBacktestNav(backtestId, { interval, limit: 500 });
  const rows = nav.data?.data ?? [];

  const data = rows.map((r: BacktestNavRow) => {
    const y =
      series === "nav"
        ? r.nav
        : series === "benchmark_nav"
          ? r.benchmark_nav
          : series === "drawdown"
            ? r.drawdown
            : r.active_return;
    return { x: r.session_date, y: y ?? null };
  });
  const isReturn = series === "active_return" || series === "drawdown";

  return (
    <section className="card elev-sm flex flex-col gap-2 p-4" data-testid="nav-section">
      <div className="flex items-center justify-between gap-2">
        <h3 className="card-title">Equity curve</h3>
        <div className="flex items-center gap-2">
          <fieldset className="flex gap-1 border-0 p-0">
            <legend className="sr-only">Series</legend>
            {NAV_SERIES.map((s) => (
              <button
                key={s.key}
                type="button"
                className={`btn ${series === s.key ? "btn-primary" : "btn-secondary"}`}
                aria-pressed={series === s.key}
                onClick={() => setSeries(s.key)}
              >
                {s.label}
              </button>
            ))}
          </fieldset>
          <fieldset className="flex gap-1 border-0 p-0">
            <legend className="sr-only">Interval</legend>
            {NAV_INTERVALS.map((i) => (
              <button
                key={i}
                type="button"
                className={`btn ${interval === i ? "btn-primary" : "btn-secondary"}`}
                aria-pressed={interval === i}
                onClick={() => setInterval(i)}
              >
                {i}
              </button>
            ))}
          </fieldset>
        </div>
      </div>
      {nav.isPending ? (
        <LoadingState label="Loading NAV" />
      ) : nav.isError ? (
        <ErrorState error={nav.error} onRetry={() => void nav.refetch()} />
      ) : (
        <>
          <LineChart
            title={`${NAV_SERIES.find((s) => s.key === series)?.label} (${interval})`}
            data={data}
            height={240}
            width={720}
            formatY={(v) => (isReturn ? formatPct(v) : v.toFixed(2))}
          />
          {nav.data?.downsampled && (
            <p className="text-xs text-(--color-neutral-500)">
              Downsampled from {nav.data.source_rows} rows ({nav.data.total} total).
            </p>
          )}
        </>
      )}
    </section>
  );
}

const MONTH_NAMES = [
  "Jan",
  "Feb",
  "Mar",
  "Apr",
  "May",
  "Jun",
  "Jul",
  "Aug",
  "Sep",
  "Oct",
  "Nov",
  "Dec",
];

function MonthlyReturns({ rows }: { rows: BacktestMonthlyRow[] }) {
  const byYear = new Map<number, Map<number, BacktestMonthlyRow>>();
  for (const row of rows) {
    if (!byYear.has(row.year)) byYear.set(row.year, new Map());
    byYear.get(row.year)?.set(row.month, row);
  }
  const years = [...byYear.keys()].sort();
  if (years.length === 0) {
    return <EmptyState title="No monthly returns" />;
  }
  return (
    <section className="card elev-sm flex flex-col gap-2 p-4" data-testid="monthly-section">
      <h3 className="card-title">Monthly returns</h3>
      <div className="overflow-x-auto">
        <table className="table" aria-label="Monthly returns heatmap">
          <thead>
            <tr>
              <th scope="col">Year</th>
              {MONTH_NAMES.map((m) => (
                <th key={m} scope="col">
                  {m}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {years.map((year) => (
              <tr key={year}>
                <th scope="row">{year}</th>
                {MONTH_NAMES.map((_, i) => {
                  const cell = byYear.get(year)?.get(i + 1);
                  const v = cell?.month_return;
                  const intensity = v === undefined ? 0 : Math.min(0.85, Math.abs(v) * 4);
                  const bg =
                    v === undefined
                      ? "transparent"
                      : v >= 0
                        ? `rgba(76, 154, 108, ${intensity})`
                        : `rgba(200, 80, 80, ${intensity})`;
                  return (
                    <td
                      key={MONTH_NAMES[i]}
                      className="text-center"
                      style={{ backgroundColor: bg }}
                      title={
                        cell
                          ? `${year}-${String(i + 1).padStart(2, "0")}: ${formatPct(v)}`
                          : undefined
                      }
                    >
                      {v === undefined ? "" : formatPct(v, 1)}
                    </td>
                  );
                })}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </section>
  );
}

function AttributionSection({
  rows,
}: {
  rows: { dimension: string; group_key: string; contribution: number }[];
}) {
  const byDim = new Map<string, typeof rows>();
  for (const row of rows) {
    if (!byDim.has(row.dimension)) byDim.set(row.dimension, []);
    byDim.get(row.dimension)?.push(row);
  }
  if (byDim.size === 0) return <EmptyState title="No attribution" />;
  return (
    <section className="card elev-sm flex flex-col gap-3 p-4" data-testid="attribution-section">
      <h3 className="card-title">Attribution</h3>
      <div className="grid grid-cols-3 gap-4">
        {[...byDim.entries()].map(([dim, dimRows]) => (
          <div key={dim}>
            <h4 className="mb-1 text-xs font-semibold uppercase tracking-[0.08em] text-(--color-neutral-600)">
              {dim.replace("_", " ")}
            </h4>
            <table className="table" aria-label={`Attribution by ${dim}`}>
              <thead>
                <tr>
                  <th scope="col">Group</th>
                  <th scope="col">Contribution</th>
                </tr>
              </thead>
              <tbody>
                {dimRows
                  .slice()
                  .sort((a, b) => Math.abs(b.contribution) - Math.abs(a.contribution))
                  .map((r) => (
                    <tr key={r.group_key}>
                      <td>{r.group_key}</td>
                      <td>{formatPct(r.contribution)}</td>
                    </tr>
                  ))}
              </tbody>
            </table>
          </div>
        ))}
      </div>
    </section>
  );
}

const TRADE_COLUMNS: DataTableColumnDef<BacktestTradeRow>[] = [
  { accessorKey: "trade_seq", header: "#" },
  { accessorKey: "session_date", header: "Date" },
  { accessorKey: "security_id", header: "Security" },
  { accessorKey: "side", header: "Side" },
  {
    accessorKey: "quantity",
    header: "Qty",
    cell: ({ row }) => formatNum(row.original.quantity, 4),
  },
  {
    accessorKey: "price",
    header: "Price",
    cell: ({ row }) => formatNum(Number(row.original.price), 4),
  },
  {
    accessorKey: "notional",
    header: "Notional",
    cell: ({ row }) =>
      row.original.notional === null || row.original.notional === undefined
        ? "—"
        : formatNum(Number(row.original.notional), 2),
  },
  {
    accessorKey: "fee",
    header: "Fee",
    cell: ({ row }) =>
      row.original.fee === null || row.original.fee === undefined
        ? "—"
        : formatNum(Number(row.original.fee), 4),
  },
  {
    accessorKey: "realized_pnl",
    header: "Realized P&L",
    cell: ({ row }) =>
      row.original.realized_pnl === null || row.original.realized_pnl === undefined
        ? "—"
        : formatNum(Number(row.original.realized_pnl), 4),
  },
];

function TradesSection({ backtestId }: { backtestId: string }) {
  const [pageIndex, setPageIndex] = useState(0);
  const trades = useBacktestTrades(backtestId, { limit: PAGE_SIZE, offset: pageIndex * PAGE_SIZE });
  const rows = trades.data?.data ?? [];
  const total = trades.data?.pagination.total ?? rows.length;
  const pageCount = Math.max(1, Math.ceil(total / PAGE_SIZE));

  return (
    <section className="card elev-sm flex flex-col gap-2 p-4" data-testid="trades-section">
      <h3 className="card-title">Trades</h3>
      {trades.data?.truncated && (
        <p className="text-xs text-(--color-neutral-500)">
          Result truncated by the server export cap.
        </p>
      )}
      <DataTable
        columns={TRADE_COLUMNS}
        data={rows}
        loading={trades.isPending}
        error={trades.error}
        onRetry={() => void trades.refetch()}
        emptyTitle="No trades"
        caption="Backtest trades"
        manualPagination
        pagination={{ pageIndex, pageSize: PAGE_SIZE }}
        onPaginationChange={(p) => setPageIndex(p.pageIndex)}
        pageCount={pageCount}
        rowCount={total}
        csvHref={buildCsvUrl(`/api/backtests/${backtestId}/trades`, {})}
        csvFilename={`backtest-${backtestId}-trades.csv`}
      />
    </section>
  );
}

function Results({ detail }: { detail: BacktestDetail }) {
  const succeeded = detail.status === "succeeded";
  const stats = useBacktestStats(detail.backtest_id, succeeded);
  const monthly = useBacktestMonthly(detail.backtest_id, succeeded);
  const attribution = useBacktestAttribution(detail.backtest_id, succeeded);
  // monthly_returns/attribution also ship inline on the detail payload.
  const monthlyRows = monthly.data?.data ?? detail.monthly_returns ?? [];
  const attributionRows = attribution.data?.data ?? detail.attribution ?? [];

  return (
    <div className="flex flex-col gap-4" data-testid="backtest-results">
      {stats.isPending ? (
        <LoadingState label="Loading stats" />
      ) : stats.isError ? (
        <ErrorState error={stats.error} title="Failed to load stats" />
      ) : (
        <StatCards stats={stats.data?.data ?? []} />
      )}
      <NavSection backtestId={detail.backtest_id} />
      {monthly.isPending ? (
        <LoadingState label="Loading monthly returns" />
      ) : (
        <MonthlyReturns rows={monthlyRows as BacktestMonthlyRow[]} />
      )}
      {attribution.isPending ? (
        <LoadingState label="Loading attribution" />
      ) : (
        <AttributionSection rows={attributionRows as BacktestAttributionRowLike[]} />
      )}
      <TradesSection backtestId={detail.backtest_id} />
    </div>
  );
}

type BacktestAttributionRowLike = { dimension: string; group_key: string; contribution: number };

/** `/backtests/$backtestId` — status, stats, charts, attribution, trades. */
export function BacktestDetailPage({ backtestId }: { backtestId: string }) {
  const detail = useBacktest(backtestId);

  if (detail.isPending) {
    return <LoadingState label="Loading backtest" />;
  }
  if (detail.isError) {
    return <ErrorState error={detail.error} title="Failed to load backtest" />;
  }
  const d = detail.data;
  const active = d.status === "queued" || d.status === "running";

  return (
    <div className="flex flex-col gap-4">
      <div className="flex flex-col gap-1">
        <div className="flex items-center gap-3">
          <h1 className="font-heading text-2xl font-semibold">{d.name ?? d.backtest_id}</h1>
          <StatusBadge status={d.status} />
          {active && (
            <span className="text-sm text-(--color-neutral-600)">polling for updates…</span>
          )}
        </div>
        <div className="flex flex-wrap gap-x-4 gap-y-1 text-xs text-(--color-neutral-600)">
          <code>{d.backtest_id}</code>
          <span>created {formatTimestamp(d.created_at)}</span>
          {d.started_at && <span>started {formatTimestamp(d.started_at)}</span>}
          {d.finished_at && <span>finished {formatTimestamp(d.finished_at)}</span>}
          {d.benchmark_id && <span>benchmark {d.benchmark_id}</span>}
        </div>
        {d.status === "failed" && d.error && (
          <div role="alert" className="border border-(--color-divider) bg-(--color-surface) p-3">
            <p className="text-sm font-semibold text-(--color-neutral-700)">
              Backtest failed: {d.error.type}
            </p>
            <p className="text-sm text-(--color-neutral-700)">{d.error.message}</p>
          </div>
        )}
        {d.status === "interrupted" && (
          <p className="text-sm text-(--color-neutral-600)">
            This backtest was interrupted before completion.
          </p>
        )}
      </div>

      {active ? (
        <LoadingState label={`Backtest ${d.status}`} />
      ) : d.status === "succeeded" ? (
        <Results detail={d} />
      ) : d.status === "failed" ? null : (
        <EmptyState title={`Status: ${d.status}`} />
      )}
    </div>
  );
}
