import { afterEach, describe, expect, it, vi } from "vitest";
import {
  ANALYST_ID,
  cleanup,
  INTERVAL_ID,
  RUN_A,
  ratingDetail,
  renderRoute,
  runsResponse,
  screen,
  stubApi,
} from "../analysts/test-utils";

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

function detailRoutes(detail: unknown = ratingDetail()) {
  return {
    "/api/runs": runsResponse(),
    [`/api/analysts/${ANALYST_ID}/ratings/${INTERVAL_ID}`]: detail,
  };
}

describe("RatingDetailPage", () => {
  it("renders the explanation prominently plus event, legs, returns, contributions", async () => {
    const calls = stubApi(detailRoutes());
    renderRoute(`/analysts/${ANALYST_ID}/ratings/${INTERVAL_ID}?run_id=${RUN_A}`);

    // The server-built explanation is the star of the page.
    const explanation = await screen.findByTestId("rating-explanation");
    expect(explanation.textContent).toContain("Initiation to buy on SEC_1");
    expect(explanation.textContent).toContain("weight 0.5000 x excess +5.00%");
    expect(explanation.textContent).toContain("= +2.50% contribution");

    // Header: security link, action, rating, correction flag.
    const secLink = screen.getByRole("link", { name: "SEC_1" });
    expect(secLink.getAttribute("href")).toContain("/securities/SEC_1");
    expect(secLink.getAttribute("href")).toContain(`run_id=${RUN_A}`);
    expect(screen.getByTestId("rating-badge")).toBeTruthy();
    expect(screen.getByTestId("correction-badge")).toBeTruthy();

    // Event details: ratings transition, timestamps, correction/supersedes.
    expect(screen.getByText("Knowledge ts")).toBeTruthy();
    expect(screen.getByText("2024-01-10 15:00")).toBeTruthy();
    expect(screen.getByText("EV_000")).toBeTruthy(); // supersedes event id
    expect(screen.getByText("150.00 USD")).toBeTruthy(); // price target

    // Interval legs.
    expect(screen.getByText("Decay start")).toBeTruthy();
    expect(screen.getAllByText("2024-01-12 open").length).toBeGreaterThan(0);
    expect(screen.getByText("canonical")).toBeTruthy(); // weight_source

    // Returns + contributions tables.
    expect(screen.getAllByText("to_exit").length).toBeGreaterThan(0);
    expect(screen.getByTitle("MKT_US")).toBeTruthy(); // benchmark_id on benchmark cell
    expect(screen.getByText("+10.0%")).toBeTruthy(); // stock return
    expect(screen.getByText("+2.5%")).toBeTruthy(); // contribution

    // Event window + target outcome cards.
    expect(screen.getByText("Event window")).toBeTruthy();
    expect(screen.getByText("Target outcome")).toBeTruthy();
    expect(screen.getByText("Car pm1")).toBeTruthy();
    expect(screen.getByText("50.0%")).toBeTruthy(); // implied_return as pct

    const call = calls.find((c) => c.url.includes(`/ratings/${INTERVAL_ID}`));
    expect(call?.url).toContain(`run_id=${RUN_A}`);
  });

  it("renders unavailable/empty sections gracefully", async () => {
    stubApi(
      detailRoutes(
        ratingDetail({
          is_investable: false,
          non_investable_reason: "horizon_beyond_data",
          returns: [],
          contributions: [],
          exposures: [],
          event_window: null,
          target_outcome: null,
          event: null,
          explanation: "Interval is not investable (horizon_beyond_data); excluded from metrics.",
        }),
      ),
    );
    renderRoute(`/analysts/${ANALYST_ID}/ratings/${INTERVAL_ID}?run_id=${RUN_A}`);

    expect(await screen.findByTestId("rating-explanation")).toBeTruthy();
    expect(screen.getByTestId("non-investable-badge")).toBeTruthy();
    expect(screen.getAllByText(/horizon_beyond_data/).length).toBeGreaterThan(0);
    expect(screen.getAllByText("No data").length).toBeGreaterThanOrEqual(2);
    expect(screen.getByText("No return legs")).toBeTruthy();
    expect(screen.getByText("No contributions")).toBeTruthy();
  });
});
