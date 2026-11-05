import type { ReactNode } from "react";

function errorMessage(error: unknown): string {
  if (error instanceof Error) return error.message;
  if (typeof error === "object" && error !== null) {
    const body = error as Record<string, unknown>;
    if (typeof body.message === "string") return body.message;
    if (typeof body.detail === "string") return body.detail;
    try {
      return JSON.stringify(error);
    } catch {
      return "Unknown error";
    }
  }
  return String(error);
}

const LOADING_WIDTHS = ["85%", "65%", "45%"];

export function LoadingState({ label = "Loading…" }: { label?: string }) {
  return (
    <div role="status" aria-live="polite" className="flex flex-col gap-2 p-4">
      <span className="sr-only">{label}</span>
      {LOADING_WIDTHS.map((width) => (
        <div
          key={width}
          aria-hidden="true"
          className="h-4 animate-pulse bg-(--color-neutral-200)"
          style={{ width }}
        />
      ))}
    </div>
  );
}

export function SkeletonRows({ rows = 5, cols = 4 }: { rows?: number; cols?: number }) {
  const rowKeys = Array.from({ length: rows }, (_, i) => `row-${i}`);
  const cellKeys = Array.from({ length: cols }, (_, i) => `cell-${i}`);
  return (
    <div role="status" aria-label="Loading" className="flex flex-col gap-2 p-4">
      {rowKeys.map((rowKey) => (
        <div key={rowKey} className="flex gap-3">
          {cellKeys.map((cellKey) => (
            <div
              key={cellKey}
              aria-hidden="true"
              className="h-4 flex-1 animate-pulse bg-(--color-neutral-200)"
            />
          ))}
        </div>
      ))}
    </div>
  );
}

export function ErrorState({
  error,
  onRetry,
  title = "Something went wrong",
}: {
  error?: unknown;
  onRetry?: () => void;
  title?: string;
}) {
  return (
    <div role="alert" className="flex flex-col items-start gap-2 p-4">
      <h3 className="text-base font-semibold">{title}</h3>
      {error !== undefined && error !== null && (
        <p className="text-sm text-(--color-neutral-700)">{errorMessage(error)}</p>
      )}
      {onRetry && (
        <button type="button" className="btn btn-secondary" onClick={onRetry}>
          Retry
        </button>
      )}
    </div>
  );
}

export function EmptyState({
  title = "Nothing here yet",
  description,
  action,
}: {
  title?: string;
  description?: ReactNode;
  action?: ReactNode;
}) {
  return (
    <div className="flex flex-col items-start gap-2 p-4">
      <h3 className="text-base font-semibold">{title}</h3>
      {description && <div className="text-sm text-(--color-neutral-700)">{description}</div>}
      {action}
    </div>
  );
}
