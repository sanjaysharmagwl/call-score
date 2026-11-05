import { Link } from "@tanstack/react-router";
import { type FormEvent, useMemo, useState } from "react";
import { buildCsvUrl, type Schemas } from "../../api/query";
import { FilterBar } from "../../components/controls";
import type { DataTableColumnDef } from "../../components/DataTable";
import { DataTable } from "../../components/DataTable";
import { useSectors } from "../shared/queries";

type SectorRow = Schemas["SectorItem"];

/** `/sectors` — sector table with coverage counts (not run-scoped). */
export function SectorsPage() {
  const [asOf, setAsOf] = useState("");
  const [appliedAsOf, setAppliedAsOf] = useState("");

  const params = useMemo(() => ({ as_of: appliedAsOf || undefined, limit: 500 }), [appliedAsOf]);
  const sectorsQuery = useSectors(params);
  const rows = sectorsQuery.data?.data ?? [];

  const csvHref = buildCsvUrl("/api/sectors", {
    as_of: params.as_of,
    limit: params.limit,
  });

  const apply = (event: FormEvent) => {
    event.preventDefault();
    setAppliedAsOf(asOf);
  };

  const columns = useMemo<DataTableColumnDef<SectorRow>[]>(
    () => [
      {
        id: "name",
        header: "Sector",
        accessorKey: "name",
        cell: (ctx) => {
          const row = ctx.row.original;
          return (
            <Link
              to="/sectors/$sectorId"
              params={{ sectorId: row.sector_id }}
              search={(prev) => ({ run_id: prev.run_id })}
              className="font-medium text-(--color-accent) hover:underline"
            >
              {row.name}
            </Link>
          );
        },
      },
      { id: "sector_id", header: "ID", accessorKey: "sector_id" },
      {
        id: "parent",
        header: "Parent",
        accessorKey: "parent_sector_id",
        cell: (ctx) => ctx.row.original.parent_sector_id ?? "—",
      },
      {
        id: "securities",
        header: "Securities",
        accessorKey: "security_count",
      },
      {
        id: "analysts",
        header: "Analysts",
        accessorKey: "analyst_count",
      },
      {
        id: "delisted",
        header: "Delisted",
        accessorKey: "delisted_count",
        cell: (ctx) =>
          ctx.row.original.delisted_count > 0 ? (
            <span className="font-medium">{ctx.row.original.delisted_count}</span>
          ) : (
            "0"
          ),
      },
    ],
    [],
  );

  return (
    <div className="flex flex-col gap-4">
      <div>
        <h1 className="font-heading text-2xl font-semibold">Sectors</h1>
        <p className="text-sm text-(--color-neutral-600)">
          Sector coverage and run-scoped performance aggregates.
        </p>
      </div>

      <FilterBar label="Sector filters">
        <form onSubmit={apply} className="flex flex-wrap items-end gap-3">
          <label className="flex flex-col gap-1 text-xs text-(--color-neutral-600)">
            As of
            <input
              type="date"
              className="input"
              value={asOf}
              onChange={(e) => setAsOf(e.target.value)}
            />
          </label>
          <button type="submit" className="btn btn-primary">
            Apply
          </button>
        </form>
      </FilterBar>

      <DataTable<SectorRow>
        columns={columns}
        data={rows}
        loading={sectorsQuery.isPending}
        error={sectorsQuery.isError ? sectorsQuery.error : undefined}
        onRetry={() => void sectorsQuery.refetch()}
        caption="Sectors"
        emptyTitle="No sectors"
        emptyDescription="No sector classifications are available."
        csvHref={csvHref}
        csvFilename="sectors.csv"
      />
    </div>
  );
}
