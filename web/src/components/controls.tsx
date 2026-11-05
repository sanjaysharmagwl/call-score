import type { ReactNode } from "react";

export interface PaginationState {
  pageIndex: number;
  pageSize: number;
}

export interface SortingStateItem {
  id: string;
  desc: boolean;
}

export type SortingState = SortingStateItem[];

export function Pagination({
  pageIndex,
  pageCount,
  onPageChange,
  pageSize,
  total,
}: {
  pageIndex: number;
  pageCount: number;
  onPageChange: (pageIndex: number) => void;
  pageSize?: number;
  total?: number;
}) {
  const canPrev = pageIndex > 0;
  const canNext = pageCount > 0 ? pageIndex < pageCount - 1 : false;
  const range =
    pageSize !== undefined && total !== undefined
      ? `Showing ${pageIndex * pageSize + 1}–${Math.min((pageIndex + 1) * pageSize, total)} of ${total}`
      : pageCount > 0
        ? `Page ${pageIndex + 1} of ${pageCount}`
        : "No pages";

  return (
    <nav aria-label="Pagination" className="flex items-center gap-3 text-sm">
      <button
        type="button"
        className="btn btn-secondary"
        disabled={!canPrev}
        onClick={() => onPageChange(pageIndex - 1)}
      >
        Previous
      </button>
      <span aria-live="polite" className="text-(--color-neutral-700)">
        {range}
      </span>
      <button
        type="button"
        className="btn btn-secondary"
        disabled={!canNext}
        onClick={() => onPageChange(pageIndex + 1)}
      >
        Next
      </button>
    </nav>
  );
}

export function FilterBar({
  children,
  label = "Filters",
}: {
  children: ReactNode;
  label?: string;
}) {
  return (
    // biome-ignore lint/a11y/useSemanticElements: <search> landmark; labelled region for filter controls
    <div
      role="search"
      aria-label={label}
      className="flex flex-wrap items-end gap-3 border border-(--color-divider) bg-(--color-surface) p-3"
    >
      {children}
    </div>
  );
}
