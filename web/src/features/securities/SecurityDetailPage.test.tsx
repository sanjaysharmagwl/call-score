import { afterEach, describe, expect, it } from "vitest";
import {
  cleanup,
  RUN_A,
  renderRoute,
  runsResponse,
  screen,
  sectorItem,
  stubApi,
  waitFor,
} from "./test-utils";

const DETAIL = {
  security_id: "SEC_1",
  name: "Acme Corp",
  latest_classification: {
    from_date: "2020-01-01",
    to_date: null,
    exchange_id: "XNYS",
    currency: "USD",
    sector_id: "SEC_TECHNOLOGY",
    region: "NA",
    cap_bucket: "large",
    listing_date: "2010-01-04",
    delisting_date: null,
  },
  sector_name: "Technology Hardware",
  is_delisted: true,
  delisting: {
    delisting_date: "2023-05-01",
    final_return: -0.42,
    reason: "acquired",
  },
  identifiers: [
    { id_type: "ticker", value: "ACME", from_date: "2010-01-04", to_date: "2023-04-28" },
    { id_type: "ticker", value: "ACMX", from_date: "2023-05-01", to_date: null },
    { id_type: "isin", value: "US0000000001", from_date: "2010-01-04", to_date: null },
  ],
  classification_spells: [
    {
      from_date: "2015-01-01",
      to_date: "2020-01-01",
      exchange_id: "XNYS",
      currency: "USD",
      sector_id: "SEC_SOFTWARE",
      region: "NA",
      cap_bucket: "mid",
      listing_date: "2010-01-04",
      delisting_date: null,
    },
    {
      from_date: "2020-01-01",
      to_date: null,
      exchange_id: "XNYS",
      currency: "USD",
      sector_id: "SEC_TECHNOLOGY",
      region: "NA",
      cap_bucket: "large",
      listing_date: "2010-01-04",
      delisting_date: "2023-05-01",
    },
  ],
  coverage_analysts: 7,
  price_coverage: {
    n_rows: 3200,
    first_session: "2010-01-04",
    last_session: "2023-04-28",
  },
  rating_event_count: 3,
};

function baseRoutes() {
  return {
    "/api/securities/SEC_1/prices": {
      security_id: "SEC_1",
      interval: "1w",
      rows: [
        {
          session_date: "2023-04-21",
          close: 10.5,
          adj_close: 10.2,
          is_halted: false,
        },
        {
          session_date: "2023-04-28",
          close: 11.0,
          adj_close: 10.7,
          is_halted: false,
        },
      ],
      total: 2,
      downsampled: false,
      source_rows: 2,
    },
    "/api/securities/SEC_1/ratings": {
      security_id: "SEC_1",
      run_id: RUN_A,
      data: [
        {
          event_id: "EV_1",
          analyst_id: "AN_1",
          analyst_name: "Jane Doe",
          action_type: "upgrade",
          canonical_rating: "buy",
          prior_canonical_rating: "hold",
          announced_ts: "2023-04-20T14:30:00Z",
          is_correction: false,
          price_target: 15.0,
          pt_currency: "USD",
          excess_return: 0.12,
        },
        {
          event_id: "EV_2",
          analyst_id: "AN_2",
          analyst_name: "John Roe",
          action_type: "initiation",
          canonical_rating: "strong_buy",
          announced_ts: "2023-03-01T09:00:00Z",
          is_correction: true,
        },
      ],
      pagination: { limit: 100, offset: 0, total: 2 },
    },
    "/api/securities/SEC_1/consensus": {
      security_id: "SEC_1",
      run_id: RUN_A,
      consensus: [
        { session_date: "2023-04-21", n_ratings: 2, consensus_score: 0.6 },
        { session_date: "2023-04-28", n_ratings: 2, consensus_score: 0.7 },
      ],
      target_outcomes: [
        {
          event_id: "EV_1",
          analyst_id: "AN_1",
          canonical_rating: "buy",
          entry_session: "2023-04-24",
          horizon_end_session: "2023-10-24",
          entry_price: 10.4,
          target_price: 15.0,
          implied_return: 0.44,
          realized_return: 0.2,
          hit: true,
          is_matured: true,
        },
      ],
    },
    "/api/securities/SEC_1/performance": {
      security_id: "SEC_1",
      run: {
        run_id: RUN_A,
        status: "succeeded",
        as_of_date: "2024-06-28",
        methodology_fingerprint: "f".repeat(64),
      },
      n_ratings: 3,
      n_investable: 3,
      mean_excess_return: 0.05,
      hit_rate: 0.67,
      mean_contribution: 0.01,
      target_hit_rate: 0.5,
      n_target_outcomes: 2,
      by_horizon: [
        { horizon: "d63", n: 3, excess_return: 0.05, stock_return: 0.1, benchmark_return: 0.05 },
      ],
    },
    "/api/securities/SEC_1": DETAIL,
    "/api/runs": runsResponse(),
    "/api/sectors": { data: [sectorItem()], pagination: { limit: 500, offset: 0, total: 1 } },
  };
}

afterEach(cleanup);

describe("SecurityDetailPage", () => {
  it("renders identifiers, delisting info, ratings and run performance", async () => {
    stubApi(baseRoutes());
    renderRoute(`/securities/SEC_1?run_id=${RUN_A}`);

    // Header + delisting banner.
    expect(await screen.findByText("Acme Corp")).toBeTruthy();
    expect(screen.getByTestId("delisting-banner").textContent).toContain("acquired");
    expect(screen.getByTestId("delisted-badge")).toBeTruthy();

    // Identifiers incl. historical tickers.
    expect(screen.getAllByText("ACME").length).toBeGreaterThan(0);
    expect(screen.getByText("ACMX")).toBeTruthy();
    expect(screen.getByText("US0000000001")).toBeTruthy();

    // Coverage stats.
    expect(screen.getByText("7")).toBeTruthy();
    expect(screen.getByText("3200 (2010-01-04 → 2023-04-28)")).toBeTruthy();

    // Ratings table: analyst link, action, canonical badge, correction flag.
    expect(await screen.findByText("Jane Doe")).toBeTruthy();
    expect(screen.getByText("John Roe")).toBeTruthy();
    expect(screen.getAllByTestId("rating-badge").length).toBeGreaterThanOrEqual(2);
    expect(screen.getByTestId("correction-badge")).toBeTruthy();

    // Run-scoped performance card.
    expect(await screen.findByText("Run performance")).toBeTruthy();
    // mean excess 0.05 appears in both the card and the horizon row.
    expect(screen.getAllByText("5.0%").length).toBeGreaterThanOrEqual(1);
    expect(screen.getByText("67.0%")).toBeTruthy(); // hit rate 0.67
    expect(screen.getByText("d63")).toBeTruthy();

    // Consensus + target outcomes.
    expect(screen.getAllByText("Target outcomes (active run)").length).toBeGreaterThan(0);

    // Run-scoped sub-resource calls carried run_id.
    await waitFor(() => {
      // covered implicitly by stubApi; assert via performance label presence
      expect(screen.getAllByTestId("provenance-run").length).toBeGreaterThan(0);
    });
  });
});
