import type { components } from "./schema.gen";

export type Schemas = components["schemas"];

export type RunSummary = Schemas["RunSummary"];
export type RunDetail = Schemas["RunDetail"];
export type RunListResponse = Schemas["RunListResponse"];
export type RunProvenanceBlock = Schemas["RunProvenanceBlock"];
export type AnalystListItem = Schemas["AnalystListItem"];
export type AnalystListResponse = Schemas["AnalystListResponse"];
export type AnalystProfileResponse = Schemas["AnalystProfileResponse"];
export type LeaderboardRow = Schemas["LeaderboardRow"];
export type LeaderboardResponse = Schemas["LeaderboardResponse"];
export type MetricDetail = Schemas["MetricDetail"];
export type MetricDrilldownResponse = Schemas["MetricDrilldownResponse"];
export type SecurityListItem = Schemas["SecurityListItem"];
export type SecurityListResponse = Schemas["SecurityListResponse"];
export type SecurityDetailResponse = Schemas["SecurityDetailResponse"];
export type SectorItem = Schemas["SectorItem"];
export type SectorListResponse = Schemas["SectorListResponse"];
export type SectorPerformanceResponse = Schemas["SectorPerformanceResponse"];
export type ChangesResponse = Schemas["ChangesResponse"];
export type OverviewResponse = Schemas["OverviewResponse"];
export type MethodologyListResponse = Schemas["MethodologyListResponse"];
export type InlineMetric = Schemas["InlineMetric"];

/**
 * Query-key factory. Convention: every run-scoped query key is
 * `[domain, params]` where `params` is a plain object that includes
 * `run_id` when the endpoint is run-scoped. `runIdOfQueryKey` relies on
 * this shape for cache eviction when the active run changes.
 */
export const queryKeys = {
  health: () => ["health"] as const,
  meta: {
    enums: () => ["meta", "enums"] as const,
    metrics: () => ["meta", "metrics"] as const,
    methodologyDefaults: () => ["meta", "methodology-defaults"] as const,
  },
  runs: {
    list: (params?: Record<string, unknown>) => ["runs", params ?? {}] as const,
    detail: (runId: string) => ["run", { run_id: runId }] as const,
  },
  methodologies: {
    list: () => ["methodologies"] as const,
    detail: (fingerprint: string) => ["methodology", { fingerprint }] as const,
  },
  analysts: {
    list: (params: Record<string, unknown>) => ["analysts", params] as const,
    profile: (analystId: string, runId?: string) =>
      ["analyst", { analyst_id: analystId, ...(runId ? { run_id: runId } : {}) }] as const,
    metrics: (analystId: string, params: Record<string, unknown>) =>
      ["analyst-metrics", { analyst_id: analystId, ...params }] as const,
    ratings: (analystId: string, params: Record<string, unknown>) =>
      ["analyst-ratings", { analyst_id: analystId, ...params }] as const,
    portfolio: (analystId: string, runId?: string) =>
      [
        "analyst-portfolio",
        { analyst_id: analystId, ...(runId ? { run_id: runId } : {}) },
      ] as const,
  },
  leaderboard: {
    list: (params: Record<string, unknown>) => ["leaderboard", params] as const,
  },
  securities: {
    list: (params?: Record<string, unknown>) => ["securities", params ?? {}] as const,
    detail: (securityId: string) => ["security", { security_id: securityId }] as const,
    prices: (securityId: string, params?: Record<string, unknown>) =>
      ["security-prices", { security_id: securityId, ...(params ?? {}) }] as const,
    ratings: (securityId: string, params: Record<string, unknown>) =>
      ["security-ratings", { security_id: securityId, ...params }] as const,
    consensus: (securityId: string, params: Record<string, unknown>) =>
      ["security-consensus", { security_id: securityId, ...params }] as const,
    performance: (securityId: string, runId: string) =>
      ["security-performance", { security_id: securityId, run_id: runId }] as const,
  },
  overview: {
    get: (runId?: string) => ["overview", { ...(runId ? { run_id: runId } : {}) }] as const,
  },
  sectors: {
    list: (params?: Record<string, unknown>) => ["sectors", params ?? {}] as const,
    performance: (sectorId: string, runId: string) =>
      ["sector-performance", { sector_id: sectorId, run_id: runId }] as const,
  },
  changes: {
    list: (params?: Record<string, unknown>) => ["changes", params ?? {}] as const,
  },
};

/**
 * Extract the `run_id` carried by a query key, or `undefined` when the key
 * is not run-scoped. Relies on the `queryKeys` convention above.
 */
export function runIdOfQueryKey(key: readonly unknown[]): string | undefined {
  const params = key[1];
  if (typeof params !== "object" || params === null) return undefined;
  const runId = (params as Record<string, unknown>).run_id;
  return typeof runId === "string" ? runId : undefined;
}

/** Short display form of a run id (first 8 chars of the uuid). */
export function shortRunId(runId: string): string {
  return runId.length <= 8 ? runId : runId.slice(0, 8);
}

/** Short display form of a methodology fingerprint. */
export function shortFingerprint(fingerprint: string): string {
  return fingerprint.length <= 12 ? fingerprint : fingerprint.slice(0, 12);
}

/**
 * Build a `?format=csv` URL for an endpoint. CSV download is server-side:
 * the API echoes the same filters and returns `text/csv`; the client only
 * triggers the browser download (see {@link downloadCsv}).
 */
export function buildCsvUrl(path: string, params?: Record<string, unknown>): string {
  const search = new URLSearchParams();
  for (const [key, value] of Object.entries(params ?? {})) {
    if (value === undefined || value === null) continue;
    if (Array.isArray(value)) {
      for (const v of value) search.append(key, String(v));
    } else {
      search.set(key, String(value));
    }
  }
  search.set("format", "csv");
  const qs = search.toString();
  return qs ? `${path}?${qs}` : `${path}?format=csv`;
}

/** Fetch a CSV export and trigger a browser download. */
export async function downloadCsv(url: string, filename: string): Promise<void> {
  const res = await fetch(url, { headers: { accept: "text/csv" } });
  if (!res.ok) {
    throw new Error(`CSV download failed: HTTP ${res.status}`);
  }
  const blob = await res.blob();
  const objectUrl = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = objectUrl;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  a.remove();
  URL.revokeObjectURL(objectUrl);
}
