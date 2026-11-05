/** Shared display helpers for the backtests feature. */

const STATUS_TAG: Record<string, string> = {
  succeeded: "tag tag-accent",
  ready: "tag tag-accent",
  queued: "tag tag-neutral",
  building: "tag tag-neutral",
  running: "tag tag-neutral",
  draft: "tag tag-neutral",
  failed: "tag tag-outline",
  interrupted: "tag tag-outline",
};

/** Lifecycle badge for backtests (run statuses) and benchmarks. */
export function StatusBadge({ status }: { status: string | null | undefined }) {
  const value = status ?? "unknown";
  return (
    <span className={STATUS_TAG[value] ?? "tag tag-neutral"} data-testid="status-badge">
      {value}
    </span>
  );
}

/** Format a ratio/fraction as a percentage with sign. */
export function formatPct(value: number | null | undefined, digits = 2): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return "—";
  const pct = value * 100;
  return `${pct >= 0 ? "+" : ""}${pct.toFixed(digits)}%`;
}

/** Format a plain number; `—` for null/undefined/NaN. */
export function formatNum(value: number | null | undefined, digits = 4): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return "—";
  return value.toFixed(digits);
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

/**
 * One-line summary of a stored spec dict for list rows, e.g.
 * `all · equal · long_only · monthly`. Unknown shapes degrade to the first
 * few primitive values.
 */
export function summarizeSpec(spec: unknown): string {
  if (!isRecord(spec)) return "—";
  const parts: string[] = [];
  for (const key of [
    "selection",
    "preset",
    "sizing",
    "mode",
    "rebalance",
    "weighting",
    "horizon",
    "score_field",
  ]) {
    const value = spec[key];
    if (typeof value === "string" && value !== "") parts.push(value);
  }
  const analystIds = spec.analyst_ids;
  if (Array.isArray(analystIds) && analystIds.length > 0) {
    parts.push(`${analystIds.length} analyst${analystIds.length === 1 ? "" : "s"}`);
  }
  const topN = spec.top_n;
  if (typeof topN === "number") parts.push(`top ${topN}`);
  const peerGroup = spec.peer_group;
  if (typeof peerGroup === "string" && peerGroup !== "") parts.push(`pg:${peerGroup}`);
  if (parts.length === 0) {
    const primitives = Object.values(spec).filter(
      (v) => typeof v === "string" || typeof v === "number" || typeof v === "boolean",
    );
    return primitives.slice(0, 4).map(String).join(" · ") || "—";
  }
  return parts.join(" · ");
}

/** ISO timestamp -> short local-ish display (first 16 chars, 'T' -> ' '). */
export function formatTimestamp(ts: string | null | undefined): string {
  if (!ts) return "—";
  return ts.replace("T", " ").slice(0, 16);
}
