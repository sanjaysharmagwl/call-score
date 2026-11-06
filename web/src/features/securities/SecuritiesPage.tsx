import { Link, useSearch } from "@tanstack/react-router";
import { type FormEvent, useEffect, useMemo, useState } from "react";
import { buildCsvUrl, type Schemas } from "../../api/query";
import { FilterBar } from "../../components/controls";
import type { DataTableColumnDef } from "../../components/DataTable";
import { DataTable } from "../../components/DataTable";
import { useSectors, useSecurities } from "../shared/queries";
import { DelistedBadge } from "./badges";
import { fmtDate } from "./format";

const PAGE_SIZE = 50;
const CAP_BUCKETS = ["mega", "large", "mid", "small", "micro"] as const;

type SecurityRow = Schemas["SecurityListItem"];

interface FilterState {
  q: string;
  sector: string;
  cap_bucket: string;
  as_of: string;
}

function strParam(value: unknown): string {
  return typeof value === "string" ? value : "";
}

/** `/securities` — searchable, filterable, paged security master list. */
export function SecuritiesPage() {
  // Deep-linkable filters (e.g. /securities?sector=SEC_X from the sector page).
  const search = useSearch({ strict: false }) as Record<string, unknown>;
  const linkedQ = strParam(search.q);
  const linkedSector = strParam(search.sector);
  const linkedCap = strParam(search.cap_bucket);
  const linkedAsOf = strParam(search.as_of);
  const linked = useMemo<FilterState>(
    () => ({ q: linkedQ, sector: linkedSector, cap_bucket: linkedCap, as_of: linkedAsOf }),
    [linkedQ, linkedSector, linkedCap, linkedAsOf],
  );

  const [draft, setDraft] = useState<FilterState>(linked);
  const [applied, setApplied] = useState<FilterState>(linked);
  const [pageIndex, setPageIndex] = useState(0);

  // Keep the form in sync when the URL search params change (deep links,
  // back/forward navigation).
  useEffect(() => {
    setDraft(linked);
    setApplied(linked);
    setPageIndex(0);
  }, [linked]);

  const sectorsQuery = useSectors({ limit: 500 });
  const sectorOptions = useMemo(() => sectorsQuery.data?.data ?? [], [sectorsQuery.data]);

  const params = {
    q: applied.q || undefined,
    sector: applied.sector || undefined,
    cap_bucket: applied.cap_bucket || undefined,
    as_of: applied.as_of || undefined,
    limit: PAGE_SIZE,
    offset: pageIndex * PAGE_SIZE,
  };
  const listQuery = useSecurities(params);
  const rows = listQuery.data?.data ?? [];
  const total = listQuery.data?.pagination?.total ?? 0;

  const csvHref = buildCsvUrl("/api/securities", {
    q: params.q,
    sector: params.sector,
    cap_bucket: params.cap_bucket,
    as_of: params.as_of,
    limit: params.limit,
    offset: params.offset,
  });

  const apply = (event: FormEvent) => {
    event.preventDefault();
    setApplied({ ...draft });
    setPageIndex(0);
  };

  const reset = () => {
    const empty: FilterState = { q: "", sector: "", cap_bucket: "", as_of: "" };
    setDraft(empty);
    setApplied(empty);
    setPageIndex(0);
  };

  const columns = useMemo<DataTableColumnDef<SecurityRow>[]>(
    () => [
      {
        id: "security",
        header: "Security",
        accessorFn: (row) => row.ticker ?? row.security_id,
        cell: (ctx) => {
          const row = ctx.row.original;
          return (
            <Link
              to="/securities/$securityId"
              params={{ securityId: row.security_id }}
              search={(prev) => ({ run_id: prev.run_id })}
              className="font-medium text-(--color-accent) hover:underline"
            >
              {row.ticker ?? row.security_id}
            </Link>
          );
        },
      },
      {
        id: "name",
        header: "Name",
        accessorFn: (row) => row.name,
        cell: (ctx) => ctx.row.original.name,
      },
      {
        id: "sector",
        header: "Sector",
        accessorFn: (row) => row.sector_name ?? "",
        cell: (ctx) => ctx.row.original.sector_name ?? "—",
      },
      {
        id: "cap_bucket",
        header: "Cap",
        accessorFn: (row) => row.cap_bucket ?? "",
        cell: (ctx) => ctx.row.original.cap_bucket ?? "—",
      },
      {
        id: "status",
        header: "Status",
        accessorFn: (row) => (row.is_delisted ? "delisted" : "active"),
        cell: (ctx) =>
          ctx.row.original.is_delisted ? (
            <DelistedBadge />
          ) : (
            <span className="text-(--color-neutral-500)">—</span>
          ),
      },
      {
        id: "classification",
        header: "Classification",
        accessorFn: (row) => row.classification_from_date ?? "",
        cell: (ctx) => {
          const row = ctx.row.original;
          const from = fmtDate(row.classification_from_date);
          const to = row.classification_to_date ? fmtDate(row.classification_to_date) : "open";
          return (
            <span className="text-xs text-(--color-neutral-700)">
              {from} → {to}
            </span>
          );
        },
      },
    ],
    [],
  );

  return (
    <div className="flex flex-col gap-4">
      <div>
        <h1 className="font-heading text-2xl font-semibold">Securities</h1>
        <p className="text-sm text-(--color-neutral-600)">
          Security master list — point-in-time classifications; delisted securities are always
          included.
        </p>
      </div>

      <FilterBar label="Security filters">
        <form onSubmit={apply} className="flex flex-wrap items-end gap-3">
          <label className="flex flex-col gap-1 text-xs text-(--color-neutral-600)">
            Search
            <input
              type="search"
              className="input w-56"
              value={draft.q}
              onChange={(e) => setDraft({ ...draft, q: e.target.value })}
              placeholder="ticker, name, id…"
            />
          </label>
          <label className="flex flex-col gap-1 text-xs text-(--color-neutral-600)">
            Sector
            <select
              className="input"
              value={draft.sector}
              onChange={(e) => setDraft({ ...draft, sector: e.target.value })}
            >
              <option value="">All sectors</option>
              {sectorOptions.map((s) => (
                <option key={s.sector_id} value={s.sector_id}>
                  {s.name}
                </option>
              ))}
            </select>
          </label>
          <label className="flex flex-col gap-1 text-xs text-(--color-neutral-600)">
            Cap bucket
            <select
              className="input"
              value={draft.cap_bucket}
              onChange={(e) => setDraft({ ...draft, cap_bucket: e.target.value })}
            >
              <option value="">All caps</option>
              {CAP_BUCKETS.map((b) => (
                <option key={b} value={b}>
                  {b}
                </option>
              ))}
            </select>
          </label>
          <label className="flex flex-col gap-1 text-xs text-(--color-neutral-600)">
            As of
            <input
              type="date"
              className="input"
              value={draft.as_of}
              onChange={(e) => setDraft({ ...draft, as_of: e.target.value })}
            />
          </label>
          <button type="submit" className="btn btn-primary">
            Apply
          </button>
          <button type="button" className="btn btn-ghost" onClick={reset}>
            Reset
          </button>
        </form>
      </FilterBar>

      <DataTable<SecurityRow>
        columns={columns}
        data={rows}
        loading={listQuery.isPending}
        error={listQuery.isError ? listQuery.error : undefined}
        onRetry={() => void listQuery.refetch()}
        caption="Securities"
        emptyTitle="No securities match"
        emptyDescription="Adjust the filters or clear the search to see more rows."
        manualPagination
        pagination={{ pageIndex, pageSize: PAGE_SIZE }}
        onPaginationChange={(p) => setPageIndex(p.pageIndex)}
        rowCount={total}
        pageCount={Math.max(1, Math.ceil(total / PAGE_SIZE))}
        csvHref={csvHref}
        csvFilename="securities.csv"
      />
    </div>
  );
}
