import { Link, useNavigate, useSearch } from "@tanstack/react-router";
import { type FormEvent, useEffect, useMemo, useState } from "react";
import type { Schemas } from "../../api/query";
import { EligibilityBadge, ScoreBadge } from "../../components/Badges";
import type { SortingState } from "../../components/controls";
import { FilterBar } from "../../components/controls";
import type { DataTableColumnDef } from "../../components/DataTable";
import { DataTable } from "../../components/DataTable";
import { MetricValue } from "../../components/MetricCard";
import { useLeaderboard } from "../shared/queries";
import { useActiveRun } from "../shared/run-state";
import { useMetricCatalog } from "./queries";

type LeaderboardRow = Schemas["LeaderboardRow"];
type InlineMetric = Schemas["InlineMetric"];
type FamilyScores = Schemas["FamilyScores"];

const PAGE_SIZE = 50;

const FAMILIES: { key: keyof FamilyScores; label: string }[] = [
  { key: "return_skill", label: "Return" },
  { key: "timing", label: "Timing" },
  { key: "price_target", label: "PT" },
  { key: "behavior", label: "Behavior" },
  { key: "consistency", label: "Consistency" },
];

/** Key metrics rendered in their own sortable columns (sort ids are catalog codes). */
const KEY_METRIC_COLS: { code: string; header: string }[] = [
  { code: "mean_contribution", header: "Mean contrib" },
  { code: "hit_rate", header: "Hit rate" },
  { code: "information_ratio", header: "Info ratio" },
  { code: "target_hit_rate", header: "Target hit" },
];

interface FilterState {
  peer_group: string;
  metric: string;
  min_obs: string;
  eligible: string;
}

const EMPTY_FILTERS: FilterState = { peer_group: "", metric: "", min_obs: "", eligible: "" };

function strParam(value: unknown): string {
  return typeof value === "string" ? value : "";
}

function InlineMetricCell({ metric }: { metric: InlineMetric | undefined }) {
  if (!metric) {
    return <span className="text-(--color-neutral-500)">—</span>;
  }
  return (
    <MetricValue
      value={metric.value}
      nObs={metric.n_obs}
      unavailableReason={metric.unavailable_reason}
      digits={3}
    />
  );
}

function FamilyBreakdown({ scores }: { scores: FamilyScores | undefined }) {
  if (!scores) return <span className="text-(--color-neutral-500)">—</span>;
  return (
    <span className="inline-flex flex-wrap gap-x-2 gap-y-0.5 text-xs">
      {FAMILIES.map(({ key, label }) => {
        const v = scores[key];
        return (
          <span key={key} className="whitespace-nowrap" title={`${label} family score`}>
            <span className="text-(--color-neutral-500)">{label}</span>{" "}
            <span className="font-medium">
              {v === null || v === undefined ? "—" : v.toFixed(2)}
            </span>
          </span>
        );
      })}
    </span>
  );
}

/** `/leaderboard` — ranked analyst table for the active run. */
export function LeaderboardPage() {
  const { runId } = useActiveRun();
  const navigate = useNavigate();
  const search = useSearch({ strict: false }) as Record<string, unknown>;

  const linked = useMemo<FilterState>(
    () => ({
      peer_group: strParam(search.peer_group),
      metric: strParam(search.metric),
      min_obs: strParam(search.min_obs),
      eligible: strParam(search.eligible),
    }),
    [search.peer_group, search.metric, search.min_obs, search.eligible],
  );

  const [draft, setDraft] = useState<FilterState>(linked);
  const [applied, setApplied] = useState<FilterState>(linked);
  const [pageIndex, setPageIndex] = useState(0);
  const [sorting, setSorting] = useState<SortingState>([{ id: "score", desc: true }]);

  useEffect(() => {
    setDraft(linked);
    setApplied(linked);
    setPageIndex(0);
  }, [linked]);

  const catalogQuery = useMetricCatalog();
  const metricOptions = useMemo(() => catalogQuery.data?.metrics ?? [], [catalogQuery.data]);

  const sort = sorting[0]?.id ?? "score";
  const order: "asc" | "desc" = sorting[0]?.desc ? "desc" : "asc";

  const minObs = applied.min_obs.trim() === "" ? undefined : Number(applied.min_obs);
  const listQuery = useLeaderboard(
    {
      run_id: runId ?? "",
      peer_group: applied.peer_group || undefined,
      metric: applied.metric || undefined,
      min_obs: minObs !== undefined && Number.isFinite(minObs) ? minObs : undefined,
      sort,
      order,
      limit: PAGE_SIZE,
      offset: pageIndex * PAGE_SIZE,
    },
    Boolean(runId),
  );

  const allRows = listQuery.data?.items ?? [];
  const total = listQuery.data?.total ?? 0;

  // NOTE: the leaderboard API has no `eligible` param (analysts list does).
  // Filter the fetched page client-side until the endpoint supports it.
  const rows = useMemo(() => {
    if (applied.eligible === "ranked") return allRows.filter((r) => r.is_ranked);
    if (applied.eligible === "unranked") return allRows.filter((r) => !r.is_ranked);
    return allRows;
  }, [allRows, applied.eligible]);

  const apply = (event: FormEvent) => {
    event.preventDefault();
    setApplied({ ...draft });
    setPageIndex(0);
    void navigate({
      to: ".",
      search: (prev: Record<string, unknown>) => ({
        ...prev,
        peer_group: draft.peer_group || undefined,
        metric: draft.metric || undefined,
        min_obs: draft.min_obs || undefined,
        eligible: draft.eligible || undefined,
      }),
      replace: true,
    });
  };

  const reset = () => {
    const empty = { ...EMPTY_FILTERS };
    setDraft(empty);
    setApplied(empty);
    setPageIndex(0);
    void navigate({
      to: ".",
      search: (prev: Record<string, unknown>) => ({
        ...prev,
        peer_group: undefined,
        metric: undefined,
        min_obs: undefined,
        eligible: undefined,
      }),
      replace: true,
    });
  };

  const columns = useMemo<DataTableColumnDef<LeaderboardRow>[]>(
    () => [
      {
        id: "rank_overall",
        header: "Rank",
        accessorFn: (row) => row.rank ?? null,
        cell: (ctx) => {
          const rank = ctx.row.original.rank;
          return rank === null || rank === undefined ? (
            <span className="text-(--color-neutral-500)">—</span>
          ) : (
            <span className="font-medium">#{rank}</span>
          );
        },
      },
      {
        id: "name",
        header: "Analyst",
        accessorFn: (row) => row.name ?? row.analyst_id,
        cell: (ctx) => {
          const row = ctx.row.original;
          return (
            <Link
              to="/analysts/$analystId"
              params={{ analystId: row.analyst_id }}
              search={(prev) => ({ run_id: prev.run_id })}
              className="font-medium text-(--color-accent) hover:underline"
            >
              {row.name ?? row.analyst_id}
            </Link>
          );
        },
      },
      {
        id: "peer_group",
        header: "Peer group",
        accessorFn: (row) => row.peer_group ?? "",
        enableSorting: false,
        cell: (ctx) => ctx.row.original.peer_group ?? "—",
      },
      {
        id: "score",
        header: "Score",
        accessorFn: (row) => row.composite_score ?? null,
        cell: (ctx) => {
          const row = ctx.row.original;
          return <ScoreBadge score={row.composite_score} rank={row.rank_in_peer_group} />;
        },
      },
      {
        id: "family_scores",
        header: "Families",
        accessorFn: (row) => row.family_scores ?? null,
        enableSorting: false,
        cell: (ctx) => <FamilyBreakdown scores={ctx.row.original.family_scores} />,
      },
      ...KEY_METRIC_COLS.map(
        ({ code, header }): DataTableColumnDef<LeaderboardRow> => ({
          id: code,
          header,
          accessorFn: (row) => row.key_metrics?.[code]?.value ?? null,
          cell: (ctx) => <InlineMetricCell metric={ctx.row.original.key_metrics?.[code]} />,
        }),
      ),
      {
        id: "n_obs",
        header: "N investable",
        accessorFn: (row) => row.n_investable,
        cell: (ctx) => ctx.row.original.n_investable,
      },
      {
        id: "eligibility",
        header: "Eligibility",
        accessorFn: (row) => (row.is_ranked ? "ranked" : "unranked"),
        enableSorting: false,
        cell: (ctx) => (
          <EligibilityBadge
            isRanked={ctx.row.original.is_ranked}
            unrankedReason={ctx.row.original.unranked_reason}
          />
        ),
      },
    ],
    [],
  );

  return (
    <div className="flex flex-col gap-4">
      <div>
        <h1 className="font-heading text-2xl font-semibold">Leaderboard</h1>
        <p className="text-sm text-(--color-neutral-600)">
          Analyst composite scores for the active run — ranked within peer groups.
        </p>
      </div>

      <FilterBar label="Leaderboard filters">
        <form onSubmit={apply} className="flex flex-wrap items-end gap-3">
          <label className="flex flex-col gap-1 text-xs text-(--color-neutral-600)">
            Peer group
            <input
              type="text"
              className="input w-44"
              value={draft.peer_group}
              onChange={(e) => setDraft({ ...draft, peer_group: e.target.value })}
              placeholder="e.g. sector:tech"
            />
          </label>
          <label className="flex flex-col gap-1 text-xs text-(--color-neutral-600)">
            Metric
            <select
              className="input"
              value={draft.metric}
              onChange={(e) => setDraft({ ...draft, metric: e.target.value })}
              aria-label="Metric filter"
            >
              <option value="">All analysts</option>
              {metricOptions.map((m) => (
                <option key={m.code} value={m.code}>
                  {m.code}
                </option>
              ))}
            </select>
          </label>
          <label className="flex flex-col gap-1 text-xs text-(--color-neutral-600)">
            Min obs
            <input
              type="number"
              min={0}
              className="input w-24"
              value={draft.min_obs}
              onChange={(e) => setDraft({ ...draft, min_obs: e.target.value })}
              placeholder="0"
            />
          </label>
          <label className="flex flex-col gap-1 text-xs text-(--color-neutral-600)">
            Eligibility
            <select
              className="input"
              value={draft.eligible}
              onChange={(e) => setDraft({ ...draft, eligible: e.target.value })}
              aria-label="Eligibility filter"
            >
              <option value="">All</option>
              <option value="ranked">Ranked</option>
              <option value="unranked">Unranked</option>
            </select>
          </label>
          <button type="submit" className="btn btn-primary">
            Apply
          </button>
          <button type="button" className="btn btn-ghost" onClick={reset}>
            Reset
          </button>
        </form>
      </FilterBar>

      <DataTable<LeaderboardRow>
        columns={columns}
        data={rows}
        loading={listQuery.isPending}
        error={listQuery.isError ? listQuery.error : undefined}
        onRetry={() => void listQuery.refetch()}
        caption="Analyst leaderboard"
        emptyTitle="No analysts on the leaderboard"
        emptyDescription="Adjust the filters or pick a different run."
        sorting={sorting}
        onSortingChange={(next) => {
          setSorting(next);
          setPageIndex(0);
        }}
        manualSorting
        manualPagination
        pagination={{ pageIndex, pageSize: PAGE_SIZE }}
        onPaginationChange={(p) => setPageIndex(p.pageIndex)}
        rowCount={total}
        pageCount={Math.max(1, Math.ceil(total / PAGE_SIZE))}
      />

      <p className="text-xs text-(--color-neutral-600)">
        Scores measure analyst rating skill only — they do not capture client service, research
        model quality, corporate access, or commission generation.
      </p>
    </div>
  );
}
