import { afterEach, describe, expect, it, vi } from "vitest";
import {
  ANALYST_ID,
  analystListItem,
  cleanup,
  fireEvent,
  leaderboardResponse,
  leaderboardRow,
  metricCatalogResponse,
  RUN_A,
  renderRoute,
  runsResponse,
  screen,
  stubApi,
  waitFor,
} from "../analysts/test-utils";

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

function baseRoutes(overrides: Record<string, unknown> = {}) {
  return {
    "/api/runs": runsResponse(),
    "/api/meta/metrics": metricCatalogResponse(),
    "/api/leaderboard": leaderboardResponse(),
    "/api/analysts": {
      items: [analystListItem()],
      total: 1,
      limit: 50,
      offset: 0,
      provenance: {
        run_id: RUN_A,
        methodology_fingerprint: "f".repeat(64),
        as_of_date: "2024-06-28",
        run_status: "succeeded",
      },
    },
    ...overrides,
  };
}

describe("LeaderboardPage", () => {
  it("renders leaderboard rows with score, families, metrics and eligibility", async () => {
    const calls = stubApi(baseRoutes());
    renderRoute(`/leaderboard?run_id=${RUN_A}`);

    expect(await screen.findByText("Alice Analyst")).toBeTruthy();
    expect(screen.getByText("tech-large")).toBeTruthy();
    expect(screen.getByTestId("score-badge")).toBeTruthy();
    expect(screen.getByTestId("eligibility-badge")).toBeTruthy();
    // Key metric value rendered via MetricValue.
    expect(screen.getByText("0.600")).toBeTruthy();
    // Unavailable metric shows n/a + reason.
    expect(screen.getByText("n/a")).toBeTruthy();
    expect(screen.getByText(/no_target_outcomes/)).toBeTruthy();
    // Family breakdown labels.
    expect(screen.getByText("Return")).toBeTruthy();
    expect(screen.getByText("Consistency")).toBeTruthy();

    const lbCall = calls.find((c) => c.url.includes("/api/leaderboard"));
    expect(lbCall).toBeTruthy();
    expect(lbCall?.url).toContain(`run_id=${RUN_A}`);
    expect(lbCall?.url).toContain("sort=score");
    expect(lbCall?.url).toContain("order=desc");
  });

  it("deep-links preserve run_id on analyst links", async () => {
    stubApi(baseRoutes());
    renderRoute(`/leaderboard?run_id=${RUN_A}`);
    const link = await screen.findByRole("link", { name: "Alice Analyst" });
    expect(link.getAttribute("href")).toContain(`/analysts/${ANALYST_ID}`);
    expect(link.getAttribute("href")).toContain(`run_id=${RUN_A}`);
  });

  it("emits filter params to the leaderboard API", async () => {
    const calls = stubApi(baseRoutes());
    renderRoute(`/leaderboard?run_id=${RUN_A}`);
    await screen.findByText("Alice Analyst");

    fireEvent.change(screen.getByLabelText("Peer group"), {
      target: { value: "tech-large" },
    });
    fireEvent.change(screen.getByLabelText("Min obs"), { target: { value: "5" } });
    fireEvent.click(screen.getByRole("button", { name: "Apply" }));

    await waitFor(() => {
      const lbCalls = calls.filter((c) => c.url.includes("/api/leaderboard"));
      const last = lbCalls[lbCalls.length - 1];
      expect(last?.url).toContain("peer_group=tech-large");
      expect(last?.url).toContain("min_obs=5");
      expect(last?.url).toContain(`run_id=${RUN_A}`);
    });
  });

  it("filters ranked/unranked client-side via the eligibility toggle", async () => {
    stubApi(
      baseRoutes({
        "/api/leaderboard": leaderboardResponse([
          leaderboardRow(),
          leaderboardRow({
            analyst_id: "AN_002",
            name: "Bob Unranked",
            rank: null,
            composite_score: null,
            is_ranked: false,
            unranked_reason: "min_ratings",
            n_investable: 3,
          }),
        ]),
      }),
    );
    renderRoute(`/leaderboard?run_id=${RUN_A}`);
    await screen.findByText("Alice Analyst");
    expect(screen.getByText("Bob Unranked")).toBeTruthy();

    fireEvent.change(screen.getByLabelText("Eligibility filter"), {
      target: { value: "unranked" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Apply" }));

    await waitFor(() => {
      expect(screen.queryByText("Alice Analyst")).toBeNull();
    });
    expect(screen.getByText("Bob Unranked")).toBeTruthy();
    expect(screen.getByText(/min_ratings/)).toBeTruthy();
  });

  it("passes column-header sorts to the API", async () => {
    const calls = stubApi(baseRoutes());
    renderRoute(`/leaderboard?run_id=${RUN_A}`);
    await screen.findByText("Alice Analyst");

    fireEvent.click(screen.getByRole("button", { name: /Analyst/ }));

    await waitFor(() => {
      const lbCalls = calls.filter((c) => c.url.includes("/api/leaderboard"));
      const last = lbCalls[lbCalls.length - 1];
      expect(last?.url).toContain("sort=name");
      expect(last?.url).toContain("order=asc");
    });
  });
});
