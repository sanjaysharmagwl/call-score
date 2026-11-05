import { afterEach, describe, expect, it, vi } from "vitest";
import {
  ANALYST_ID,
  analystProfile,
  cleanup,
  INTERVAL_ID,
  portfolioResponse,
  RUN_A,
  ratingListResponse,
  renderRoute,
  runsResponse,
  screen,
  stubApi,
} from "./test-utils";

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

function detailRoutes(overrides: Record<string, unknown> = {}) {
  return {
    "/api/runs": runsResponse(),
    [`/api/analysts/${ANALYST_ID}/ratings`]: ratingListResponse(),
    [`/api/analysts/${ANALYST_ID}/portfolio`]: portfolioResponse(),
    [`/api/analysts/${ANALYST_ID}`]: analystProfile(),
    ...overrides,
  };
}

describe("AnalystDetailPage", () => {
  it("renders composite, family scores, metrics, spells, portfolio and ratings", async () => {
    stubApi(detailRoutes());
    renderRoute(`/analysts/${ANALYST_ID}?run_id=${RUN_A}`);

    // Header + composite.
    expect(await screen.findByText("Alice Analyst")).toBeTruthy();
    expect(screen.getByText("Acme Research")).toBeTruthy();
    expect(screen.getByText("Composite score")).toBeTruthy();
    expect(screen.getAllByTestId("score-badge").length).toBeGreaterThan(0);
    expect(screen.getByText("empirical_bayes")).toBeTruthy();

    // Family score cards.
    expect(screen.getAllByText("Return skill").length).toBeGreaterThan(0);
    expect(screen.getByText("Consistency")).toBeTruthy();

    // Metrics grouped by family; unavailable metric shows reason.
    expect(screen.getByText("hit_rate")).toBeTruthy();
    expect(screen.getByText("max_drawdown")).toBeTruthy();
    expect(screen.getByText(/insufficient_obs/)).toBeTruthy();
    expect(screen.getByText("82%")).toBeTruthy(); // peer percentile

    // Spells.
    expect(screen.getAllByText("Employment spells").length).toBeGreaterThan(0);
    expect(screen.getAllByText("Coverage spells").length).toBeGreaterThan(0);
    expect(screen.getAllByText("SEC_1").length).toBeGreaterThan(0);

    // Portfolio charts render (svg role=img) and positions snapshot.
    expect(await screen.findAllByRole("img")).not.toHaveLength(0);
    expect(screen.getByText(/Positions snapshot/)).toBeTruthy();
    expect(screen.getByText("market_value")).toBeTruthy();

    // Ratings table with link to rating detail preserving run_id.
    const detailLink = screen.getByRole("link", { name: /detail/ });
    expect(detailLink.getAttribute("href")).toContain(
      `/analysts/${ANALYST_ID}/ratings/${INTERVAL_ID}`,
    );
    expect(detailLink.getAttribute("href")).toContain(`run_id=${RUN_A}`);
    expect(screen.getByText("+5.0%")).toBeTruthy(); // excess return
  });

  it("renders empty states when metrics/portfolio/ratings are absent", async () => {
    stubApi(
      detailRoutes({
        [`/api/analysts/${ANALYST_ID}`]: analystProfile({
          metrics: [],
          composite: null,
          employment_spells: [],
          coverage_spells: [],
        }),
        [`/api/analysts/${ANALYST_ID}/ratings`]: {
          items: [],
          total: 0,
          limit: 20,
          offset: 0,
          provenance: {
            run_id: RUN_A,
            methodology_fingerprint: "f".repeat(64),
            as_of_date: "2024-06-28",
            run_status: "succeeded",
          },
        },
        [`/api/analysts/${ANALYST_ID}/portfolio`]: portfolioResponse({
          points: [],
          total_points: 0,
          positions: { session_date: null, rows: [] },
        }),
      }),
    );
    renderRoute(`/analysts/${ANALYST_ID}?run_id=${RUN_A}`);

    expect(await screen.findByText("No metrics for this run")).toBeTruthy();
    expect(screen.getByText("No NAV history")).toBeTruthy();
    expect(screen.getByText("No ratings in this run")).toBeTruthy();
    expect(screen.getByText("No employment spells")).toBeTruthy();
  });
});
