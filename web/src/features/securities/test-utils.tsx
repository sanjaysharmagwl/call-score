import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { createMemoryHistory, createRouter, RouterProvider } from "@tanstack/react-router";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { vi } from "vitest";
import { routeTree } from "../../routeTree.gen";

export const RUN_A = "aaaaaaaa-0000-0000-0000-00000000000a";

export function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json" },
  });
}

export function requestUrl(input: RequestInfo | URL): string {
  if (typeof input === "string") return input;
  if (input instanceof URL) return input.toString();
  if (input instanceof Request) return input.url;
  return String(input);
}

export interface FetchCall {
  url: string;
}

/**
 * Stub `fetch` with a path-prefix -> response handler. Returns the recorded
 * request URLs for assertions.
 */
export function stubApi(
  routes: Record<string, unknown | ((url: string) => Response)>,
): FetchCall[] {
  const calls: FetchCall[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL) => {
      const url = requestUrl(input);
      calls.push({ url });
      if (url.includes("/api/health")) {
        return jsonResponse({ status: "ok", version: "0.1.0" });
      }
      for (const [prefix, handler] of Object.entries(routes)) {
        if (url.includes(prefix)) {
          if (typeof handler === "function") return handler(url);
          return jsonResponse(handler);
        }
      }
      return jsonResponse({ detail: "not found" }, 404);
    }),
  );
  return calls;
}

export function renderRoute(initialEntry: string) {
  const router = createRouter({
    routeTree,
    history: createMemoryHistory({ initialEntries: [initialEntry] }),
  });
  render(
    <QueryClientProvider client={new QueryClient()}>
      <RouterProvider router={router} />
    </QueryClientProvider>,
  );
  return router;
}

export function securityListItem(overrides: Record<string, unknown> = {}) {
  return {
    security_id: "SEC_1",
    name: "Acme Corp",
    ticker: "ACME",
    sector_id: "SEC_TECHNOLOGY",
    sector_name: "Technology Hardware",
    region: "NA",
    currency: "USD",
    exchange_id: "XNYS",
    cap_bucket: "large",
    listing_date: "2010-01-04",
    is_delisted: false,
    classification_from_date: "2020-01-01",
    classification_to_date: null,
    ...overrides,
  };
}

export function sectorItem(overrides: Record<string, unknown> = {}) {
  return {
    sector_id: "SEC_TECHNOLOGY",
    name: "Technology Hardware",
    parent_sector_id: "GRP_TECH",
    security_count: 12,
    analyst_count: 5,
    delisted_count: 1,
    ...overrides,
  };
}

export function runSummary(runId: string) {
  return {
    run_id: runId,
    status: "succeeded",
    progress: 1,
    as_of_date: "2024-06-28",
    knowledge_cutoff: "2024-06-28T00:00:00Z",
    started_at: "2024-06-28T01:00:00Z",
    finished_at: "2024-06-28T01:05:00Z",
    methodology_fingerprint: "f".repeat(64),
  };
}

export function runsResponse() {
  return {
    data: [runSummary(RUN_A)],
    pagination: { limit: 100, offset: 0, count: 1, total: 1 },
  };
}

export { cleanup, fireEvent, screen, waitFor };
