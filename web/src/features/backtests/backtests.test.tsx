import { afterEach, describe, expect, it, vi } from "vitest";
import {
  attributionResponse,
  BENCH_ID,
  BT_ID,
  backtestDetail,
  backtestListResponse,
  backtestSummary,
  baseBacktestsRoutes,
  benchmarkListResponse,
  benchmarkSeriesResponse,
  benchmarkSummary,
  cleanup,
  fireEvent,
  monthlyResponse,
  navResponse,
  renderRoute,
  screen,
  statsResponse,
  stubApi,
  tradesResponse,
  waitFor,
} from "./test-utils";

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe("BacktestsPage list", () => {
  it("renders backtest rows with status badge, dates and spec summary", async () => {
    const calls = stubApi(
      baseBacktestsRoutes({
        "GET /api/backtests": backtestListResponse([backtestSummary()]),
        [`GET /api/backtests/${BT_ID}`]: backtestDetail(),
      }),
    );
    renderRoute("/backtests");

    expect(await screen.findByText("Quality momentum")).toBeTruthy();
    expect(screen.getByTestId("status-badge").textContent).toBe("succeeded");
    // Spec summary is lazily fetched from the detail endpoint.
    expect(await screen.findByText(/all · equal · long_only · monthly/)).toBeTruthy();
    // Detail fetch happened for the spec cell.
    expect(calls.some((c) => c.url.includes(`/api/backtests/${BT_ID}`))).toBe(true);
    // No run_id is sent to backtest endpoints.
    expect(
      calls.filter((c) => c.url.includes("/api/backtests") && c.url.includes("run_id")),
    ).toHaveLength(0);
  });

  it("shows the empty state when no backtests exist", async () => {
    stubApi(baseBacktestsRoutes());
    renderRoute("/backtests");
    expect(await screen.findByText("No backtests yet")).toBeTruthy();
  });
});

describe("BacktestBuilder", () => {
  it("blocks dynamic selection without score_lookback_sessions client-side", async () => {
    const calls = stubApi(baseBacktestsRoutes());
    renderRoute("/backtests");

    fireEvent.click(await screen.findByRole("button", { name: "New backtest" }));
    const selection = await screen.findByLabelText("Selection");
    fireEvent.change(selection, { target: { value: "top_n" } });
    fireEvent.click(screen.getByRole("button", { name: "Submit backtest" }));

    expect(await screen.findByRole("alert")).toBeTruthy();
    expect(screen.getByText(/score_lookback_sessions/)).toBeTruthy();
    // No POST was attempted.
    expect(
      calls.filter((c) => c.method === "POST" && c.url.includes("/api/backtests")),
    ).toHaveLength(0);
  });

  it("requires top_n when selection is top_n", async () => {
    stubApi(baseBacktestsRoutes());
    renderRoute("/backtests");

    fireEvent.click(await screen.findByRole("button", { name: "New backtest" }));
    fireEvent.change(await screen.findByLabelText("Selection"), {
      target: { value: "top_n" },
    });
    fireEvent.change(screen.getByLabelText("Score lookback sessions"), {
      target: { value: "20" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Submit backtest" }));

    expect(await screen.findByText(/requires top_n/)).toBeTruthy();
  });

  it("submits the expected BacktestCreateRequest payload", async () => {
    const calls = stubApi(
      baseBacktestsRoutes({
        "POST /api/backtests": { backtest_id: BT_ID, status: "queued" },
      }),
    );
    renderRoute("/backtests");

    fireEvent.click(await screen.findByRole("button", { name: "New backtest" }));
    fireEvent.change(await screen.findByLabelText("Backtest name"), {
      target: { value: "My strategy" },
    });
    fireEvent.change(await screen.findByLabelText("Rebalance"), {
      target: { value: "weekly" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Submit backtest" }));

    await waitFor(() => {
      const post = calls.find((c) => c.method === "POST" && c.url.includes("/api/backtests"));
      expect(post).toBeTruthy();
      const body = post?.body as Record<string, unknown>;
      expect(body.name).toBe("My strategy");
      const spec = body.spec as Record<string, unknown>;
      expect(spec.selection).toBe("all");
      expect(spec.rebalance).toBe("weekly");
      expect(spec.mode).toBe("long_only");
      expect(spec.gross_exposure_cap).toBe(1);
      expect(spec.transaction_cost_bps).toBe(0);
      // No run_id leakage.
      expect(JSON.stringify(body)).not.toContain("run_id");
    });
  });

  it("surfaces server 422 detail verbatim", async () => {
    stubApi(
      baseBacktestsRoutes({
        "POST /api/backtests": () =>
          new Response(
            JSON.stringify({
              detail: [
                { loc: ["body", "spec", "selection"], msg: "bad selection", type: "value_error" },
              ],
            }),
            { status: 422, headers: { "content-type": "application/json" } },
          ),
      }),
    );
    renderRoute("/backtests");

    fireEvent.click(await screen.findByRole("button", { name: "New backtest" }));
    fireEvent.click(screen.getByRole("button", { name: "Submit backtest" }));

    expect(await screen.findByTestId("backtest-error")).toBeTruthy();
    expect(screen.getByText(/bad selection/)).toBeTruthy();
  });
});

describe("BacktestDetailPage", () => {
  function detailRoutes(detail: unknown) {
    return baseBacktestsRoutes({
      [`GET /api/backtests/${BT_ID}`]: detail,
      [`GET /api/backtests/${BT_ID}/nav`]: navResponse(),
      [`GET /api/backtests/${BT_ID}/stats`]: statsResponse(),
      [`GET /api/backtests/${BT_ID}/monthly`]: monthlyResponse(),
      [`GET /api/backtests/${BT_ID}/attribution`]: attributionResponse(),
      [`GET /api/backtests/${BT_ID}/trades`]: tradesResponse(),
    });
  }

  it("renders stats cards, equity chart, monthly, attribution and trades", async () => {
    stubApi(detailRoutes(backtestDetail()));
    renderRoute(`/backtests/${BT_ID}`);

    // Stats cards.
    expect(await screen.findByText("Annualized return")).toBeTruthy();
    expect(screen.getByText("Sharpe")).toBeTruthy();
    expect(screen.getByTestId("stat-cards")).toBeTruthy();

    // Equity chart (visx svg) + series toggle.
    expect(await screen.findByTestId("nav-section")).toBeTruthy();
    expect(document.querySelector("svg[role='img']")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Drawdown" }));

    // Monthly heatmap.
    expect(await screen.findByTestId("monthly-section")).toBeTruthy();
    expect(screen.getByText("Jan")).toBeTruthy();

    // Attribution grouped by dimension.
    expect(await screen.findByTestId("attribution-section")).toBeTruthy();
    expect(screen.getByText("tech")).toBeTruthy();

    // Trades table.
    expect(await screen.findByTestId("trades-section")).toBeTruthy();
    expect(screen.getByText("SEC_1")).toBeTruthy();
    expect(screen.getByText("SEC_2")).toBeTruthy();
  });

  it("shows polling indicator while queued", async () => {
    stubApi(
      detailRoutes(backtestDetail({ status: "queued", started_at: null, finished_at: null })),
    );
    renderRoute(`/backtests/${BT_ID}`);
    expect(await screen.findByText(/polling for updates/)).toBeTruthy();
  });

  it("shows the error block for failed backtests", async () => {
    stubApi(
      detailRoutes(
        backtestDetail({
          status: "failed",
          error: {
            type: "ValueError",
            message: "selection='top_n' requires score_lookback_sessions",
          },
        }),
      ),
    );
    renderRoute(`/backtests/${BT_ID}`);
    expect(await screen.findByRole("alert")).toBeTruthy();
    expect(screen.getByText(/requires score_lookback_sessions/)).toBeTruthy();
    expect(screen.getByText(/ValueError/)).toBeTruthy();
    // No results sections for a failed run.
    expect(screen.queryByTestId("stat-cards")).toBeNull();
  });
});

describe("BenchmarkPanel", () => {
  it("creates a benchmark and materializes it", async () => {
    const calls = stubApi(
      baseBacktestsRoutes({
        "GET /api/benchmarks": benchmarkListResponse([benchmarkSummary()]),
        "POST /api/benchmarks": {
          benchmark_id: BENCH_ID,
          status: "draft",
          created: true,
        },
        [`POST /api/benchmarks/${BENCH_ID}/materialize`]: {
          benchmark_id: BENCH_ID,
          status: "ready",
          row_count: 250,
          as_of_date: "2024-06-28",
          exclusion_counts: {},
          revision: 1,
        },
        [`GET /api/benchmarks/${BENCH_ID}/series`]: benchmarkSeriesResponse(),
      }),
    );
    renderRoute("/backtests?tab=benchmarks");

    // List row visible.
    expect(await screen.findByText("Tech large-cap")).toBeTruthy();

    // Create form.
    fireEvent.click(screen.getByRole("button", { name: "New benchmark" }));
    fireEvent.change(await screen.findByLabelText("Benchmark name"), {
      target: { value: "My bench" },
    });
    fireEvent.change(screen.getByLabelText("Cap bucket"), { target: { value: "large" } });
    fireEvent.click(screen.getByRole("button", { name: "Create benchmark" }));

    await waitFor(() => {
      const post = calls.find((c) => c.method === "POST" && c.url.endsWith("/api/benchmarks"));
      expect(post).toBeTruthy();
      const body = post?.body as Record<string, unknown>;
      expect(body.name).toBe("My bench");
      const spec = body.spec as Record<string, unknown>;
      expect(spec.preset).toBe("custom");
      expect(spec.weighting).toBe("equal");
      expect((spec.universe as Record<string, unknown>).cap_bucket).toBe("large");
      expect(spec.benchmark_id).toBeUndefined();
    });

    // Materialize (sync) — button shows spinner state then note.
    fireEvent.click(screen.getByRole("button", { name: "Materialize" }));
    await waitFor(() => {
      expect(
        calls.some(
          (c) => c.method === "POST" && c.url.includes(`/api/benchmarks/${BENCH_ID}/materialize`),
        ),
      ).toBe(true);
    });
    expect(await screen.findByTestId("materialize-note")).toBeTruthy();
    expect(screen.getByText(/materialized 250 rows/)).toBeTruthy();

    // Series preview chart on row click.
    fireEvent.click(screen.getByText("Tech large-cap"));
    expect(await screen.findByTestId("series-preview")).toBeTruthy();
  });
});
