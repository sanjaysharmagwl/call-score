import { afterEach, describe, expect, it } from "vitest";
import {
  cleanup,
  RUN_A,
  renderRoute,
  runsResponse,
  screen,
  sectorItem,
  stubApi,
} from "../securities/test-utils";

function baseRoutes() {
  return {
    "/api/runs": runsResponse(),
    "/api/sectors/SEC_TECHNOLOGY/performance": {
      sector_id: "SEC_TECHNOLOGY",
      sector_name: "Technology Hardware",
      parent_sector_id: "GRP_TECH",
      run: {
        run_id: RUN_A,
        status: "succeeded",
        as_of_date: "2024-06-28",
        methodology_fingerprint: "f".repeat(64),
      },
      n_securities: 12,
      n_delisted: 1,
      n_with_prices: 11,
      n_analysts: 5,
      n_ranked: 4,
      mean_composite_score: 0.42,
      composite_score_distribution: [
        { bucket: "low", low: 0, high: 0.3, count: 1 },
        { bucket: "mid", low: 0.3, high: 0.6, count: 3 },
      ],
    },
    "/api/sectors": {
      data: [
        sectorItem(),
        sectorItem({
          sector_id: "SEC_ENERGY",
          name: "Energy",
          parent_sector_id: "GRP_CYCLICAL",
          security_count: 8,
          analyst_count: 3,
          delisted_count: 0,
        }),
      ],
      pagination: { limit: 500, offset: 0, total: 2 },
    },
  };
}

afterEach(cleanup);

describe("SectorsPage", () => {
  it("renders the sector table with counts", async () => {
    stubApi(baseRoutes());
    renderRoute("/sectors");

    expect(await screen.findByText("Technology Hardware")).toBeTruthy();
    expect(screen.getByText("Energy")).toBeTruthy();
    expect(screen.getByText("SEC_TECHNOLOGY")).toBeTruthy();
  });
});

describe("SectorDetailPage", () => {
  it("renders sector detail with run-scoped performance", async () => {
    stubApi(baseRoutes());
    renderRoute(`/sectors/SEC_TECHNOLOGY?run_id=${RUN_A}`);

    expect(await screen.findByText("Technology Hardware")).toBeTruthy();
    expect(screen.getByText("SEC_TECHNOLOGY")).toBeTruthy();

    // Coverage stats.
    expect(screen.getByText("Covering analysts")).toBeTruthy();
    expect(screen.getAllByText("12").length).toBeGreaterThan(0);

    // Run performance section.
    expect(await screen.findByText("Run performance")).toBeTruthy();
    expect(screen.getByText("0.42")).toBeTruthy(); // mean composite
    expect(screen.getByText("Composite score distribution")).toBeTruthy();
    expect(screen.getByTestId("provenance-run")).toBeTruthy();

    // Link to the filtered securities list.
    const link = screen.getByText(/View securities in this sector/);
    expect(link.getAttribute("href")).toContain("/securities");
  });

  it("shows an unknown-sector empty state for bad ids", async () => {
    stubApi(baseRoutes());
    renderRoute("/sectors/SEC_NOPE");
    expect(await screen.findByText("Unknown sector")).toBeTruthy();
  });
});
