/** Percentage-style metric (fraction 0.05 -> "5.0%"); null renders as em dash. */
export function fmtPct(value: number | null | undefined, digits = 1): string {
  if (value === null || value === undefined || Number.isNaN(value)) return "—";
  return `${(value * 100).toFixed(digits)}%`;
}

/** Signed percentage for returns/excess (fraction 0.05 -> "+5.0%"). */
export function fmtSignedPct(value: number | null | undefined, digits = 1): string {
  if (value === null || value === undefined || Number.isNaN(value)) return "—";
  const sign = value > 0 ? "+" : "";
  return `${sign}${(value * 100).toFixed(digits)}%`;
}

/** Plain number with fixed digits; null renders as em dash. */
export function fmtNum(value: number | null | undefined, digits = 2): string {
  if (value === null || value === undefined || Number.isNaN(value)) return "—";
  return value.toFixed(digits);
}

/** ISO date (or datetime) -> "YYYY-MM-DD"; null/empty -> em dash. */
export function fmtDate(value: string | null | undefined): string {
  if (!value) return "—";
  return value.slice(0, 10);
}

/** ISO datetime -> "YYYY-MM-DD HH:mm" (UTC string, no locale dependence). */
export function fmtTs(value: string | null | undefined): string {
  if (!value) return "—";
  return value.slice(0, 16).replace("T", " ");
}

/** "snake_case" enum value -> "Snake case" label. */
export function humanize(value: string | null | undefined): string {
  if (!value) return "—";
  const spaced = value.replace(/_/g, " ");
  return spaced.charAt(0).toUpperCase() + spaced.slice(1);
}
