import { afterEach, describe, expect, it, vi } from "vitest";
import {
  ANALYST_ID,
  analystListItem,
  analystListResponse,
  cleanup,
  fireEvent,
  RUN_A,
  renderRoute,
  runsResponse,
  screen,
  stubApi,
  waitFor,
} from "./test-utils";

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

function baseRoutes(overrides: Record<string, unknown> = {}) {
  return {
    "/api/runs": runsResponse(),
    "/api/analysts": analystListResponse(),
    ...overrides,
  };
}

describe("AnalystsPage", () => {
  it("renders analyst rows linking to detail with run_id", async () => {
    const calls = stubApi(baseRoutes());
    renderRoute(`/analysts?run_id=${RUN_A}`);

    const link = await screen.findByRole("link", { name: "Alice Analyst" });
    expect(link.getAttribute("href")).toContain(`/analysts/${ANALYST_ID}`);
    expect(link.getAttribute("href")).toContain(`run_id=${RUN_A}`);
    expect(screen.getByText("Acme Research")).toBeTruthy();
    expect(screen.getByTestId("score-badge")).toBeTruthy();
    expect(screen.getByTestId("eligibility-badge")).toBeTruthy();

    const call = calls.find((c) => c.url.includes("/api/analysts?"));
    expect(call?.url).toContain(`run_id=${RUN_A}`);
    expect(call?.url).toContain("eligible=all");
  });

  it("emits q/peer_group/eligible params to the analysts API", async () => {
    const calls = stubApi(baseRoutes());
    renderRoute(`/analysts?run_id=${RUN_A}`);
    await screen.findByText("Alice Analyst");

    fireEvent.change(screen.getByLabelText("Search"), { target: { value: "alice" } });
    fireEvent.change(screen.getByLabelText("Peer group"), {
      target: { value: "tech-large" },
    });
    fireEvent.change(screen.getByLabelText("Eligibility filter"), {
      target: { value: "ranked" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Apply" }));

    await waitFor(() => {
      const listCalls = calls.filter((c) => c.url.includes("/api/analysts?"));
      const last = listCalls[listCalls.length - 1];
      expect(last?.url).toContain("q=alice");
      expect(last?.url).toContain("peer_group=tech-large");
      expect(last?.url).toContain("eligible=ranked");
      expect(last?.url).toContain(`run_id=${RUN_A}`);
    });
  });

  it("shows the unranked reason on ineligible rows", async () => {
    stubApi(
      baseRoutes({
        "/api/analysts": analystListResponse([
          analystListItem({
            analyst_id: "AN_009",
            name: "Carol Cold",
            is_ranked: false,
            unranked_reason: "below_min_obs",
            composite_score: null,
            rank_overall: null,
          }),
        ]),
      }),
    );
    renderRoute(`/analysts?run_id=${RUN_A}`);
    await screen.findByText("Carol Cold");
    expect(screen.getByText(/below_min_obs/)).toBeTruthy();
  });
});
