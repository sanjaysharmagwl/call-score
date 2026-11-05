import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { createMemoryHistory, createRouter, RouterProvider } from "@tanstack/react-router";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { routeTree } from "../../routeTree.gen";
import {
  jsonResponse,
  RUN_A,
  requestUrl,
  runSummary,
  runsResponse,
} from "../securities/test-utils";

const RUN_NEW = "cccccccc-0000-0000-0000-00000000000c";
const RUN_FAIL = "dddddddd-0000-0000-0000-00000000000d";

interface RecordedCall {
  url: string;
  method: string;
  body: unknown;
}

type Handler = (call: RecordedCall, url: URL) => Response | undefined;

function pathnameOf(url: string): { path: string; search: URLSearchParams } {
  const u = new URL(url, "http://localhost");
  return { path: u.pathname, search: u.searchParams };
}

/**
 * Stub fetch with a small router. Handlers are tried in order; the first to
 * return a Response wins. Falls back to 404.
 */
function stubApi(handlers: Handler[]): RecordedCall[] {
  const calls: RecordedCall[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = requestUrl(input);
      // openapi-fetch passes a Request as `input` (method/body on the Request,
      // `init` undefined); plain fetch callers may pass a string + init.
      const req = input instanceof Request ? input : null;
      const method = (init?.method ?? req?.method ?? "GET").toUpperCase();
      const rawBody =
        typeof init?.body === "string" ? init.body : req ? await req.clone().text() : null;
      let body: unknown = null;
      if (rawBody) {
        try {
          body = JSON.parse(rawBody);
        } catch {
          body = rawBody;
        }
      }
      const call: RecordedCall = { url, method, body };
      calls.push(call);
      for (const handler of handlers) {
        const res = handler(call, new URL(url, "http://localhost"));
        if (res) return res;
      }
      return jsonResponse({ detail: "not found" }, 404);
    }),
  );
  return calls;
}

function match(
  method: string,
  path: string | RegExp,
  respond: (call: RecordedCall) => Response,
): Handler {
  return (call) => {
    const { path: p } = pathnameOf(call.url);
    const okPath = typeof path === "string" ? p === path : path.test(p);
    if (call.method === method && okPath) return respond(call);
    return undefined;
  };
}

function renderPage(initialEntry: string) {
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

function runDetail(overrides: Record<string, unknown> = {}) {
  return {
    ...runSummary(RUN_A),
    algorithm_version: "engine/0.1.0",
    source_fingerprint: "a".repeat(64),
    row_counts: { analyst_metric: 1200, analyst_composite: 42, calc_run: 3 },
    ...overrides,
  };
}

function overviewResponse(overrides: Record<string, unknown> = {}) {
  return {
    run: {
      run_id: RUN_A,
      status: "succeeded",
      as_of_date: "2024-06-28",
      methodology_fingerprint: "f".repeat(64),
      source_fingerprint: "a".repeat(64),
      algorithm_version: "engine/0.1.0",
    },
    analysts: {
      n_analysts: 42,
      n_ranked: 40,
      n_scored: 38,
      score_distribution: [
        { bucket: "low", low: 0, high: 0.33, count: 10 },
        { bucket: "mid", low: 0.33, high: 0.66, count: 20 },
        { bucket: "high", low: 0.66, high: 1, count: 8 },
      ],
      top: [
        {
          analyst_id: "AN_1",
          peer_group: "large/tech/NA",
          composite_score: 0.91,
          rank_overall: 1,
          rank_in_peer_group: 1,
          is_ranked: true,
        },
      ],
      bottom: [
        {
          analyst_id: "AN_9",
          peer_group: "small/energy/EM",
          composite_score: 0.12,
          rank_overall: 42,
          rank_in_peer_group: 3,
          is_ranked: true,
        },
      ],
    },
    run_counts_by_status: { succeeded: 3, failed: 1 },
    data_coverage: {
      n_sessions: 250,
      first_session: "2023-07-03",
      last_session: "2024-06-28",
      n_exchanges: 2,
      n_securities: 150,
      n_delisted_securities: 7,
      n_rating_events: 9000,
      latest_event_ts: "2024-06-27T15:30:00Z",
      n_consensus_rows: 42000,
      latest_consensus_date: "2024-06-28",
      n_security_daily_rows: 30000,
    },
    provenance: { generated_at: "2024-06-28T02:00:00Z" },
    ...overrides,
  };
}

function analystsResponse() {
  return {
    items: [
      {
        analyst_id: "AN_1",
        name: "Alice Analyst",
        firm_id: "F1",
        peer_group: "large/tech/NA",
        composite_score: 0.91,
        rank_overall: 1,
        is_ranked: true,
        n_obs: 120,
      },
      {
        analyst_id: "AN_9",
        name: "Zed Analyst",
        firm_id: "F2",
        peer_group: "small/energy/EM",
        composite_score: 0.12,
        rank_overall: 42,
        is_ranked: true,
        n_obs: 80,
      },
    ],
    total: 2,
    limit: 500,
    offset: 0,
    provenance: { generated_at: "2024-06-28T02:00:00Z" },
  };
}

function baseHandlers(overrides: { runDetail?: unknown; overview?: unknown } = {}): Handler[] {
  return [
    match("GET", "/api/health", () => jsonResponse({ status: "ok", version: "0.1.0" })),
    match("GET", "/api/runs", () => jsonResponse(runsResponse())),
    match("GET", `/api/runs/${RUN_A}`, () => jsonResponse(overrides.runDetail ?? runDetail())),
    match("GET", `/api/runs/${RUN_FAIL}`, () =>
      jsonResponse(
        runDetail({
          run_id: RUN_FAIL,
          status: "failed",
          progress: 0.4,
          error: { type: "RuntimeError", message: "engine exploded" },
        }),
      ),
    ),
    match("GET", "/api/overview", () => jsonResponse(overrides.overview ?? overviewResponse())),
    match("GET", "/api/analysts", () => jsonResponse(analystsResponse())),
    match("GET", "/api/methodologies", () =>
      jsonResponse({
        data: [
          {
            id: 1,
            fingerprint: "b".repeat(64),
            is_baseline: true,
            created_at: "2024-01-01T00:00:00Z",
          },
        ],
        pagination: { limit: 100, offset: 0, count: 1, total: 1 },
      }),
    ),
  ];
}

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe("OverviewPage dashboard", () => {
  it("renders run summary, analyst aggregates, and top/bottom tables", async () => {
    stubApi(baseHandlers());
    renderPage(`/?run_id=${RUN_A}`);

    expect(await screen.findByTestId("overview-dashboard")).toBeTruthy();
    expect(screen.getByText("Run summary")).toBeTruthy();
    expect(screen.getAllByText("engine/0.1.0").length).toBeGreaterThan(0);
    expect(screen.getByTestId("methodology-fingerprint").textContent).toBe("f".repeat(12));

    // Analyst aggregates.
    expect(screen.getByText("Alice Analyst")).toBeTruthy();
    expect(screen.getByText("Zed Analyst")).toBeTruthy();
    expect(screen.getByText("Composite score distribution")).toBeTruthy();

    // Coverage + run counts.
    expect(screen.getByText("Data coverage")).toBeTruthy();
    expect(screen.getByText("2023-07-03 → 2024-06-28 (250)")).toBeTruthy();
    expect(screen.getByText("Runs by status")).toBeTruthy();

    // Analyst links point at the analyst detail route and keep run_id.
    const link = screen.getByText("Alice Analyst").closest("a");
    expect(link?.getAttribute("href")).toContain(`/analysts/AN_1`);
  });

  it("propagates run_id to the overview query", async () => {
    const calls = stubApi(baseHandlers());
    renderPage(`/?run_id=${RUN_A}`);
    await screen.findByTestId("overview-dashboard");

    const overviewCalls = calls.filter((c) => pathnameOf(c.url).path === "/api/overview");
    expect(overviewCalls.length).toBeGreaterThan(0);
    for (const c of overviewCalls) {
      expect(pathnameOf(c.url).search.get("run_id")).toBe(RUN_A);
    }
  });
});

describe("New run flow", () => {
  it("submits the payload, polls the run, and switches the active run", async () => {
    let detailCalls = 0;
    const calls = stubApi([
      match("GET", "/api/health", () => jsonResponse({ status: "ok", version: "0.1.0" })),
      match("GET", "/api/runs", () =>
        jsonResponse({ data: [], pagination: { limit: 1, offset: 0, count: 0, total: 0 } }),
      ),
      match("POST", "/api/runs", () =>
        jsonResponse({ run_id: RUN_NEW, status: "queued", created: true }, 202),
      ),
      match("GET", `/api/runs/${RUN_NEW}`, () => {
        detailCalls += 1;
        if (detailCalls === 1) {
          return jsonResponse(runDetail({ run_id: RUN_NEW, status: "running", progress: 0.5 }));
        }
        return jsonResponse(
          runDetail({
            run_id: RUN_NEW,
            status: "succeeded",
            progress: 1,
            row_counts: { analyst_metric: 10 },
          }),
        );
      }),
      match("GET", "/api/overview", () => jsonResponse(overviewResponse())),
      match("GET", "/api/analysts", () => jsonResponse(analystsResponse())),
      match("GET", "/api/methodologies", () =>
        jsonResponse({ data: [], pagination: { limit: 100, offset: 0, count: 0, total: 0 } }),
      ),
    ]);
    const router = renderPage("/");

    // First-use empty state is visible; open the new-run form.
    expect(await screen.findByText("No scoring runs yet")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "New run" }));

    const asOf = screen.getByLabelText("As of date") as HTMLInputElement;
    fireEvent.change(asOf, { target: { value: "2024-06-28" } });
    // jsdom does not run implicit form submission on button click; submit the form.
    fireEvent.submit(screen.getByTestId("new-run-form"));

    // POST payload is correct.
    await waitFor(() => {
      const post = calls.find((c) => c.method === "POST" && pathnameOf(c.url).path === "/api/runs");
      expect(post).toBeTruthy();
      expect(post?.body).toMatchObject({
        as_of_date: "2024-06-28",
        methodology: "default",
      });
    });

    // Polling observed: more than one GET on the new run id.
    await waitFor(
      () => {
        const polls = calls.filter(
          (c) => c.method === "GET" && pathnameOf(c.url).path === `/api/runs/${RUN_NEW}`,
        );
        expect(polls.length).toBeGreaterThanOrEqual(2);
      },
      { timeout: 6000 },
    );

    // Active run switched to the new run.
    await waitFor(
      () => {
        expect((router.state.location.search as { run_id?: string }).run_id).toBe(RUN_NEW);
      },
      { timeout: 6000 },
    );
  });
});

describe("Failed run", () => {
  it("shows the error detail and retries via POST /runs/{id}/retry", async () => {
    const calls = stubApi([
      ...baseHandlers(),
      match("POST", `/api/runs/${RUN_FAIL}/retry`, () => jsonResponse(runSummary(RUN_FAIL), 200)),
    ]);
    renderPage(`/?run_id=${RUN_FAIL}`);

    expect(await screen.findByTestId("run-error")).toBeTruthy();
    expect(screen.getByTestId("run-error").textContent).toContain("engine exploded");

    fireEvent.click(screen.getByRole("button", { name: "Retry run" }));

    await waitFor(() => {
      const retry = calls.find(
        (c) => c.method === "POST" && pathnameOf(c.url).path === `/api/runs/${RUN_FAIL}/retry`,
      );
      expect(retry).toBeTruthy();
    });
  });
});

describe("Empty states", () => {
  it("shows first-use guidance when no runs exist", async () => {
    stubApi([
      match("GET", "/api/health", () => jsonResponse({ status: "ok", version: "0.1.0" })),
      match("GET", "/api/runs", () =>
        jsonResponse({ data: [], pagination: { limit: 1, offset: 0, count: 0, total: 0 } }),
      ),
      match("GET", "/api/methodologies", () =>
        jsonResponse({ data: [], pagination: { limit: 100, offset: 0, count: 0, total: 0 } }),
      ),
    ]);
    renderPage("/");

    expect(await screen.findByText("No scoring runs yet")).toBeTruthy();
    expect(screen.getAllByText(/just seed small/).length).toBeGreaterThan(0);
    expect(screen.getAllByText("POST /api/runs").length).toBeGreaterThan(0);
  });

  it("shows the empty-database state when the run produced no rows", async () => {
    stubApi(
      baseHandlers({
        runDetail: runDetail({ row_counts: { analyst_metric: 0, analyst_composite: 0 } }),
      }),
    );
    renderPage(`/?run_id=${RUN_A}`);

    expect(await screen.findByText("Database is empty")).toBeTruthy();
    expect(screen.getByText(/just seed small/)).toBeTruthy();
  });
});
