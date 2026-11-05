import { afterEach, describe, expect, it } from "vitest";
import {
  cleanup,
  fireEvent,
  RUN_A,
  renderRoute,
  runsResponse,
  screen,
  stubApi,
  waitFor,
} from "../securities/test-utils";

const CHANGES = {
  data: [
    {
      change_id: "CH_1",
      kind: "rating",
      type: "upgrade",
      ts: "2023-04-20T14:30:00Z",
      run_id: RUN_A,
      analyst_id: "AN_1",
      analyst_name: "Jane Doe",
      security_id: "SEC_1",
      security_name: "Acme Corp",
      old_rating: "hold",
      new_rating: "buy",
      is_correction: false,
    },
    {
      change_id: "CH_2",
      kind: "rating",
      type: "initiation",
      ts: "2023-03-01T09:00:00Z",
      run_id: RUN_A,
      analyst_id: "AN_2",
      analyst_name: "John Roe",
      security_id: "SEC_1",
      security_name: "Acme Corp",
      old_rating: null,
      new_rating: "strong_buy",
      is_correction: true,
    },
    {
      change_id: "CH_3",
      kind: "run",
      type: "run_succeeded",
      ts: "2024-06-28T01:05:00Z",
      run_id: RUN_A,
      status: "succeeded",
      detail: null,
    },
  ],
  pagination: { limit: 50, offset: 0, total: 3 },
};

function baseRoutes(changes: unknown = CHANGES) {
  return {
    "/api/runs": runsResponse(),
    "/api/changes": changes,
  };
}

afterEach(cleanup);

describe("ChangesPage", () => {
  it("renders rating and run audit rows with correction badge", async () => {
    stubApi(baseRoutes());
    renderRoute("/changes");

    expect(await screen.findByText("Jane Doe")).toBeTruthy();
    expect(screen.getByText("John Roe")).toBeTruthy();
    expect(screen.getAllByTestId("rating-badge").length).toBeGreaterThanOrEqual(3);
    expect(screen.getByTestId("correction-badge")).toBeTruthy();
    // run lifecycle row rendered distinctly
    expect(screen.getAllByTestId("change-kind").map((el) => el.textContent)).toContain("run");
    expect(screen.getByText("status: succeeded")).toBeTruthy();
  });

  it("emits type/date/run-scope filters to the API", async () => {
    const calls = stubApi(baseRoutes());
    renderRoute(`/changes?run_id=${RUN_A}`);
    await screen.findByText("Jane Doe");

    fireEvent.change(screen.getByLabelText("Type"), { target: { value: "correction" } });
    fireEvent.change(screen.getByLabelText("From"), { target: { value: "2023-01-01" } });
    fireEvent.change(screen.getByLabelText("To"), { target: { value: "2023-12-31" } });
    fireEvent.click(screen.getByRole("button", { name: "Apply" }));

    await waitFor(() => {
      const hit = calls.find(
        (c) =>
          c.url.includes("/api/changes") &&
          c.url.includes("type=correction") &&
          c.url.includes("from=2023-01-01") &&
          c.url.includes("to=2023-12-31") &&
          c.url.includes(`run_id=${RUN_A}`),
      );
      expect(hit).toBeTruthy();
    });
  });

  it("shows an empty state when nothing matches", async () => {
    stubApi(
      baseRoutes({
        data: [],
        pagination: { limit: 50, offset: 0, total: 0 },
      }),
    );
    renderRoute("/changes");
    expect(await screen.findByText("No changes")).toBeTruthy();
  });
});
