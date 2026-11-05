import { Link, useNavigate, useSearch } from "@tanstack/react-router";
import { type FormEvent, useEffect, useMemo, useState } from "react";
import type { Schemas } from "../../api/query";
import { EligibilityBadge, ScoreBadge } from "../../components/Badges";
import type { SortingState } from "../../components/controls";
import { FilterBar } from "../../components/controls";
import type { DataTableColumnDef } from "../../components/DataTable";
import { DataTable } from "../../components/DataTable";
import { useAnalysts } from "../shared/queries";
import { useActiveRun } from "../shared/run-state";

type AnalystListItem = Schemas["AnalystListItem"];

const PAGE_SIZE = 50;

interface FilterState {
  q: string;
  peer_group: string;
  eligible: string;
}

const EMPTY_FILTERS: FilterState = { q: "", peer_group: "", eligible: "" };

function strParam(value: unknown): string {
  return typeof value === "string" ? value : "";
}

/** `/analysts` — searchable analyst list for the active run. */
export function AnalystsPage() {
  const { runId } = useActiveRun();
  const navigate = useNavigate();
  const search = useSearch({ strict: false }) as Record<string, unknown>;

  const linked = useMemo<FilterState>(
    () => ({
      q: strParam(search.q),
      peer_group: strParam(search.peer_group),
      eligible: strParam(search.eligible),
    }),
    [search.q, search.peer_group, search.eligible],
  );

  const [draft, setDraft] = useState<FilterState>(linked);
  const [applied, setApplied] = useState<FilterState>(linked);
  const [pageIndex, setPageIndex] = useState(0);
  const [sorting, setSorting] = useState<SortingState>([{ id: "rank_overall", desc: false }]);

  useEffect(() => {
    setDraft(linked);
    setApplied(linked);
    setPageIndex(0);
  }, [linked]);

  const sort = sorting[0]?.id ?? "rank_overall";
  const order: "asc" | "desc" = sorting[0]?.desc ? "desc" : "asc";

  const listQuery = useAnalysts(
    {
      run_id: runId ?? "",
      q: applied.q || undefined,
      peer_group: applied.peer_group || undefined,
      eligible:
        applied.eligible === "ranked" || applied.eligible === "unranked" ? applied.eligible : "all",
      sort,
      order,
      limit: PAGE_SIZE,
      offset: pageIndex * PAGE_SIZE,
    },
    Boolean(runId),
  );
  const rows = listQuery.data?.items ?? [];
  const total = listQuery.data?.total ?? 0;

  const apply = (event: FormEvent) => {
    event.preventDefault();
    setApplied({ ...draft });
    setPageIndex(0);
    void navigate({
      to: ".",
      search: (prev: Record<string, unknown>) => ({
        ...prev,
        q: draft.q || undefined,
        peer_group: draft.peer_group || undefined,
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
        q: undefined,
        peer_group: undefined,
        eligible: undefined,
      }),
      replace: true,
    });
  };

  const columns = useMemo<DataTableColumnDef<AnalystListItem>[]>(
    () => [
      {
        id: "rank_overall",
        header: "Rank",
        accessorFn: (row) => row.rank_overall ?? null,
        cell: (ctx) => {
          const rank = ctx.row.original.rank_overall;
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
            <span className="inline-flex flex-col">
              <Link
                to="/analysts/$analystId"
                params={{ analystId: row.analyst_id }}
                search={(prev) => ({ run_id: prev.run_id })}
                className="font-medium text-(--color-accent) hover:underline"
              >
                {row.name ?? row.analyst_id}
              </Link>
              <span className="font-mono text-xs text-(--color-neutral-500)">{row.analyst_id}</span>
            </span>
          );
        },
      },
      {
        id: "firm_name",
        header: "Firm",
        accessorFn: (row) => row.firm_name ?? row.firm_id ?? "",
        enableSorting: false,
        cell: (ctx) => ctx.row.original.firm_name ?? ctx.row.original.firm_id ?? "—",
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
        id: "n_obs",
        header: "N obs",
        accessorFn: (row) => row.n_obs,
        cell: (ctx) => ctx.row.original.n_obs,
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
        <h1 className="font-heading text-2xl font-semibold">Analysts</h1>
        <p className="text-sm text-(--color-neutral-600)">
          All analysts covered by the active run, ranked and unranked.
        </p>
      </div>

      <FilterBar label="Analyst filters">
        <form onSubmit={apply} className="flex flex-wrap items-end gap-3">
          <label className="flex flex-col gap-1 text-xs text-(--color-neutral-600)">
            Search
            <input
              type="search"
              className="input w-56"
              value={draft.q}
              onChange={(e) => setDraft({ ...draft, q: e.target.value })}
              placeholder="name or id…"
            />
          </label>
          <label className="flex flex-col gap-1 text-xs text-(--color-neutral-600)">
            Peer group
            <input
              type="text"
              className="input w-44"
              value={draft.peer_group}
              onChange={(e) => setDraft({ ...draft, peer_group: e.target.value })}
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

      <DataTable<AnalystListItem>
        columns={columns}
        data={rows}
        loading={listQuery.isPending}
        error={listQuery.isError ? listQuery.error : undefined}
        onRetry={() => void listQuery.refetch()}
        caption="Analysts"
        emptyTitle="No analysts match"
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
    </div>
  );
}
