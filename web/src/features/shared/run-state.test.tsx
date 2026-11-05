import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { createMemoryHistory, createRouter, RouterProvider } from "@tanstack/react-router";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { queryKeys } from "../../api/query";
import { routeTree } from "../../routeTree.gen";

const RUN_A = "aaaaaaaa-0000-0000-0000-00000000000a";
const RUN_B = "bbbbbbbb-0000-0000-0000-00000000000b";

function runSummary(runId: string, asOf: string) {
  return {
    run_id: runId,
    status: "succeeded",
    progress: 1,
    as_of_date: asOf,
    knowledge_cutoff: `${asOf}T00:00:00Z`,
    started_at: `${asOf}T01:00:00Z`,
    finished_at: `${asOf}T01:05:00Z`,
    methodology_fingerprint: "f".repeat(64),
  };
}

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json" },
  });
}

function requestUrl(input: RequestInfo | URL): string {
  if (typeof input === "string") return input;
  if (input instanceof URL) return input.toString();
  if (input instanceof Request) return input.url;
  return String(input);
}

function stubApi() {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL) => {
      const url = requestUrl(input);
      if (url.includes("/api/health")) {
        return jsonResponse({ status: "ok", version: "0.1.0" });
      }
      if (url.includes("/api/runs")) {
        return jsonResponse({
          data: [runSummary(RUN_B, "2024-06-28"), runSummary(RUN_A, "2024-06-21")],
          pagination: { limit: 100, offset: 0, count: 2, total: 2 },
        });
      }
      return jsonResponse({ detail: "not found" }, 404);
    }),
  );
}

function renderShell(initialEntry: string, queryClient: QueryClient) {
  const router = createRouter({
    routeTree,
    history: createMemoryHistory({ initialEntries: [initialEntry] }),
  });
  render(
    <QueryClientProvider client={queryClient}>
      <RouterProvider router={router} />
    </QueryClientProvider>,
  );
  return router;
}

afterEach(cleanup);

describe("run selection", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("defaults to the latest succeeded run and syncs it into the URL", async () => {
    stubApi();
    const router = renderShell("/", new QueryClient());

    const select = (await screen.findByLabelText("Active calc run")) as HTMLSelectElement;
    await waitFor(() => expect(select.value).toBe(RUN_B));
    await waitFor(() => {
      expect((router.state.location.search as { run_id?: string }).run_id).toBe(RUN_B);
    });
  });

  it("honours an existing run_id search param", async () => {
    stubApi();
    renderShell(`/?run_id=${RUN_A}`, new QueryClient());
    const select = (await screen.findByLabelText("Active calc run")) as HTMLSelectElement;
    await waitFor(() => expect(select.value).toBe(RUN_A));
  });

  it("clears cached queries scoped to the previous run on change", async () => {
    stubApi();
    const queryClient = new QueryClient();
    const metaKey = queryKeys.meta.enums();

    renderShell(`/?run_id=${RUN_B}`, queryClient);
    const select = (await screen.findByLabelText("Active calc run")) as HTMLSelectElement;

    // Seed caches after mount so the initial eviction pass has already run.
    const keyB = queryKeys.leaderboard.list({ run_id: RUN_B, limit: 10 });
    const keyA = queryKeys.leaderboard.list({ run_id: RUN_A, limit: 10 });
    queryClient.setQueryData(keyB, { items: [], total: 0, limit: 10, offset: 0 });
    queryClient.setQueryData(keyA, { items: [], total: 0, limit: 10, offset: 0 });
    queryClient.setQueryData(metaKey, { Foo: ["a"] });

    fireEvent.change(select, { target: { value: RUN_A } });

    await waitFor(() => {
      expect(queryClient.getQueryData(keyB)).toBeUndefined();
    });
    // Run-A scoped and non-scoped caches survive.
    expect(queryClient.getQueryData(keyA)).toBeDefined();
    expect(queryClient.getQueryData(metaKey)).toBeDefined();
  });
});
