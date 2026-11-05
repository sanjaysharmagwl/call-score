import { flexRender } from "@tanstack/react-table";
import {
  getCoreRowModel,
  getPaginationRowModel,
  getSortedRowModel,
  type LegacyColumnDef,
  useLegacyTable,
} from "@tanstack/react-table/legacy";
import { type ReactNode, useCallback, useState } from "react";
import { downloadCsv } from "../api/query";
import { Pagination, type PaginationState, type SortingState } from "./controls";
import { EmptyState, ErrorState, SkeletonRows } from "./states";

export type { LegacyColumnDef as DataTableColumnDef };

/** Mirrors table-core's `RowData` (`Record<string, any> | any[]`), re-declared
 *  because `@tanstack/table-core` is not a direct dependency. */
// biome-ignore lint/suspicious/noExplicitAny: mirrors upstream RowData exactly
export type TableRowData = Record<string, any> | any[];

export interface DataTableProps<T extends TableRowData> {
  columns: LegacyColumnDef<T>[];
  data: T[];
  /** Show skeleton rows instead of data. */
  loading?: boolean;
  /** Show the error state with an optional retry action. */
  error?: unknown;
  onRetry?: () => void;
  emptyTitle?: string;
  emptyDescription?: ReactNode;
  /** Accessible caption describing the table. */
  caption?: string;
  /** Server-side sorting: pass all three of `sorting`, `onSortingChange`, `manualSorting`. */
  sorting?: SortingState;
  onSortingChange?: (sorting: SortingState) => void;
  manualSorting?: boolean;
  /** Server-side pagination: pass all three of `pagination`, `onPaginationChange`, `manualPagination`. */
  pagination?: PaginationState;
  onPaginationChange?: (pagination: PaginationState) => void;
  manualPagination?: boolean;
  /** Total page count for server-side pagination (`-1` when unknown). */
  pageCount?: number;
  rowCount?: number;
  /**
   * Server-side CSV export. `csvHref` should already include `format=csv`
   * (use `buildCsvUrl`); the button streams it through `downloadCsv`.
   * Alternatively pass `onDownloadCsv` for a custom handler.
   */
  csvHref?: string;
  csvFilename?: string;
  onDownloadCsv?: () => void;
}

function sortIndicator(sorted: false | "asc" | "desc"): string {
  if (sorted === "asc") return " ↑";
  if (sorted === "desc") return " ↓";
  return "";
}

export function DataTable<T extends TableRowData>({
  columns,
  data,
  loading = false,
  error,
  onRetry,
  emptyTitle = "No rows",
  emptyDescription,
  caption,
  sorting,
  onSortingChange,
  manualSorting = false,
  pagination,
  onPaginationChange,
  manualPagination = false,
  pageCount,
  rowCount,
  csvHref,
  csvFilename,
  onDownloadCsv,
}: DataTableProps<T>) {
  // v9 table state is always controlled: feature writes route through the
  // `on*Change` options, so uncontrolled usage is implemented with internal
  // state fallbacks here.
  const [innerSorting, setInnerSorting] = useState<SortingState>([]);
  const [innerPagination, setInnerPagination] = useState<PaginationState>({
    pageIndex: 0,
    pageSize: 10,
  });
  const effSorting = sorting ?? innerSorting;
  const effPagination = pagination ?? innerPagination;

  const table = useLegacyTable<T>({
    columns,
    data,
    getCoreRowModel: getCoreRowModel(),
    ...(manualSorting ? {} : { getSortedRowModel: getSortedRowModel() }),
    ...(manualPagination ? {} : { getPaginationRowModel: getPaginationRowModel() }),
    manualSorting,
    manualPagination,
    pageCount,
    rowCount,
    state: {
      sorting: effSorting,
      pagination: effPagination,
    },
    onSortingChange: (updater) => {
      const next = typeof updater === "function" ? updater(effSorting) : (updater as SortingState);
      if (onSortingChange) onSortingChange(next as SortingState);
      else setInnerSorting(next as SortingState);
    },
    onPaginationChange: (updater) => {
      const next =
        typeof updater === "function" ? updater(effPagination) : (updater as PaginationState);
      if (onPaginationChange) onPaginationChange(next as PaginationState);
      else setInnerPagination(next as PaginationState);
    },
  });

  const handleCsv = useCallback(() => {
    if (onDownloadCsv) {
      onDownloadCsv();
    } else if (csvHref) {
      void downloadCsv(csvHref, csvFilename ?? "export.csv");
    }
  }, [onDownloadCsv, csvHref, csvFilename]);

  const showCsv = Boolean(onDownloadCsv || csvHref);
  const rows = table.getRowModel().rows;

  return (
    <div className="flex flex-col gap-2">
      <div className="flex items-center justify-between gap-2">
        {caption ? <span className="text-xs text-(--color-neutral-600)">{caption}</span> : <span />}
        {showCsv && (
          <button type="button" className="btn btn-secondary" onClick={handleCsv}>
            Download CSV
          </button>
        )}
      </div>
      {error ? (
        <ErrorState error={error} onRetry={onRetry} title="Failed to load table" />
      ) : loading ? (
        <SkeletonRows
          rows={Math.min(Math.max(data.length, 3), 8)}
          cols={Math.min(columns.length, 6)}
        />
      ) : rows.length === 0 ? (
        <EmptyState title={emptyTitle} description={emptyDescription} />
      ) : (
        <div className="overflow-x-auto">
          <table className="table">
            {caption && <caption className="sr-only">{caption}</caption>}
            <thead>
              {table.getHeaderGroups().map((headerGroup) => (
                <tr key={headerGroup.id}>
                  {headerGroup.headers.map((header) => {
                    const canSort = header.column.getCanSort();
                    const sorted = header.column.getIsSorted();
                    const onSort = header.column.getToggleSortingHandler();
                    return (
                      <th
                        key={header.id}
                        scope="col"
                        aria-sort={
                          sorted === "asc"
                            ? "ascending"
                            : sorted === "desc"
                              ? "descending"
                              : canSort
                                ? "none"
                                : undefined
                        }
                      >
                        {canSort ? (
                          <button
                            type="button"
                            className="cursor-pointer select-none font-inherit uppercase tracking-[0.08em] text-inherit"
                            onClick={onSort}
                          >
                            {flexRender(header.column.columnDef.header, header.getContext())}
                            {sortIndicator(sorted)}
                          </button>
                        ) : (
                          flexRender(header.column.columnDef.header, header.getContext())
                        )}
                      </th>
                    );
                  })}
                </tr>
              ))}
            </thead>
            <tbody>
              {rows.map((row) => (
                <tr key={row.id}>
                  {row.getVisibleCells().map((cell) => (
                    <td key={cell.id}>
                      {flexRender(cell.column.columnDef.cell, cell.getContext())}
                    </td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      {!loading &&
        !error &&
        (manualPagination
          ? Boolean(onPaginationChange && pagination)
          : data.length > effPagination.pageSize || effPagination.pageIndex > 0) && (
          <Pagination
            pageIndex={effPagination.pageIndex}
            pageSize={effPagination.pageSize}
            total={rowCount}
            pageCount={
              manualPagination
                ? pageCount !== undefined && pageCount > 0
                  ? pageCount
                  : rowCount !== undefined
                    ? Math.max(1, Math.ceil(rowCount / effPagination.pageSize))
                    : effPagination.pageIndex + (data.length === effPagination.pageSize ? 2 : 1)
                : table.getPageCount()
            }
            onPageChange={(pageIndex) => {
              if (onPaginationChange && manualPagination) {
                onPaginationChange({ ...effPagination, pageIndex });
              } else {
                setInnerPagination({ ...effPagination, pageIndex });
              }
            }}
          />
        )}
    </div>
  );
}
