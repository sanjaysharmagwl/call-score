import type { ReactNode } from "react";

export interface MetricValueProps {
  /** Point estimate; `null`/`undefined` renders as unavailable. */
  value?: number | null;
  /** Two-sided confidence interval bounds (same unit as `value`). */
  ciLow?: number | null;
  ciHigh?: number | null;
  /** Standard error, shown when CI bounds are absent. */
  stdErr?: number | null;
  /** Number of observations backing the estimate. */
  nObs?: number | null;
  /** Machine/human reason the metric could not be computed. */
  unavailableReason?: string | null;
  /** 0–100 percentile within the peer group, when available. */
  peerPercentile?: number | null;
  /** Whether the estimate is statistically significant. */
  isSignificant?: boolean | null;
  /** Unit suffix (e.g. "pp", "x") appended to numeric values. */
  unit?: string;
  /** Digits after the decimal point for rendered numbers. */
  digits?: number;
  /** Extra classes for the root element. */
  className?: string;
}

function fmt(n: number, digits: number, unit?: string): string {
  const s = n.toFixed(digits);
  return unit ? `${s}${unit}` : s;
}

/**
 * Renders one metric estimate with its uncertainty and eligibility context.
 * Uncertainty (CI / n_obs) is rendered at the same visual weight as the
 * score itself — per scoring semantics, an estimate without its interval is
 * not actionable.
 */
export function MetricValue({
  value,
  ciLow,
  ciHigh,
  stdErr,
  nObs,
  unavailableReason,
  peerPercentile,
  isSignificant,
  unit,
  digits = 2,
  className = "",
}: MetricValueProps) {
  const unavailable = value === null || value === undefined || unavailableReason != null;
  const hasCi = ciLow != null && ciHigh != null;

  return (
    <div className={`flex flex-col gap-1 ${className}`} data-testid="metric-value">
      <div className="flex items-baseline gap-2">
        <span
          className={`font-heading text-2xl font-semibold ${unavailable ? "text-(--color-neutral-500)" : ""}`}
          data-testid="metric-value-score"
        >
          {unavailable ? "n/a" : fmt(value as number, digits, unit)}
        </span>
        {isSignificant === false && !unavailable && (
          <span className="tag tag-neutral" title="Not statistically significant">
            n.s.
          </span>
        )}
      </div>

      {unavailable && unavailableReason && (
        <p className="text-xs text-(--color-neutral-600)" data-testid="metric-unavailable-reason">
          {unavailableReason}
        </p>
      )}

      {!unavailable && (
        <dl className="flex flex-wrap gap-x-4 gap-y-1 text-xs text-(--color-neutral-700)">
          {hasCi ? (
            <div className="flex gap-1">
              <dt className="text-(--color-neutral-500)">95% CI</dt>
              <dd data-testid="metric-ci">
                [{fmt(ciLow as number, digits)}, {fmt(ciHigh as number, digits)}]
              </dd>
            </div>
          ) : stdErr != null ? (
            <div className="flex gap-1">
              <dt className="text-(--color-neutral-500)">SE</dt>
              <dd>{fmt(stdErr, digits)}</dd>
            </div>
          ) : null}
          {nObs != null && (
            <div className="flex gap-1">
              <dt className="text-(--color-neutral-500)">n</dt>
              <dd data-testid="metric-n-obs">{nObs}</dd>
            </div>
          )}
          {peerPercentile != null && (
            <div className="flex gap-1">
              <dt className="text-(--color-neutral-500)">peer pct</dt>
              <dd data-testid="metric-peer-percentile">{Math.round(peerPercentile)}%</dd>
            </div>
          )}
        </dl>
      )}
    </div>
  );
}

export function MetricCard({
  title,
  kicker,
  children,
}: {
  title?: ReactNode;
  kicker?: ReactNode;
  children: ReactNode;
}) {
  return (
    <section className="card elev-sm flex flex-col gap-2">
      {kicker && <span className="card-kicker">{kicker}</span>}
      {title && <h3 className="card-title">{title}</h3>}
      {children}
    </section>
  );
}

/** Convenience wrapper: a card containing a single {@link MetricValue}. */
export function MetricStatCard({ label, metric }: { label: string; metric: MetricValueProps }) {
  return (
    <MetricCard title={label}>
      <MetricValue {...metric} />
    </MetricCard>
  );
}
