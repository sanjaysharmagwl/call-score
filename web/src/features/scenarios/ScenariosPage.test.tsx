import { afterEach, describe, expect, it, vi } from "vitest";
import {
  cleanup,
  compareResponse,
  FP_BASE,
  fireEvent,
  jsonResponse,
  methodologiesListResponse,
  methodologyDefaults,
  methodologyFixture,
  methodologyVersion,
  RUN_BASE,
  RUN_WHATIF,
  renderRoute,
  runSummary,
  runsResponse,
  SCENARIO_ID,
  scenarioDetail,
  scenarioSummary,
  screen,
  stubApi,
  waitFor,
} from "../methodologies/test-utils";

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

function baseRoutes(overrides: Record<string, unknown> = {}) {
  return {
    "/api/runs/": (_url: string) => jsonResponse(runSummary(RUN_BASE)),
    "/api/runs": runsResponse([runSummary(RUN_BASE), runSummary(RUN_WHATIF)]),
    "/api/scenarios/compare": compareResponse(),
    "/api/scenarios/sensitivity": {
      sweep_id: "sweep-abc123",
      count: 2,
      as_of_date: "2024-06-28",
      knowledge_cutoff: "2024-06-28T00:00:00Z",
      scenario_id: SCENARIO_ID,
      jobs: [
        {
          index: 0,
          spec: { param_path: "family_weights.return_skill", value: 0.2 },
          methodology_fingerprint: FP_BASE,
          run_id: RUN_BASE,
          status: "queued",
          created: true,
        },
        {
          index: 1,
          spec: { param_path: "family_weights.return_skill", value: 0.6 },
          methodology_fingerprint: FP_BASE,
          run_id: RUN_WHATIF,
          status: "queued",
          created: true,
        },
      ],
    },
    "/api/scenarios/": (url: string) => {
      if (url.includes(`/api/scenarios/${SCENARIO_ID}`)) {
        return jsonResponse(scenarioDetail());
      }
      return jsonResponse({ detail: "not found" }, 404);
    },
    "/api/scenarios": (_url: string, init?: RequestInit) => {
      if (init?.method === "POST") {
        return jsonResponse(scenarioDetail(), 201);
      }
      return jsonResponse({
        data: [scenarioSummary()],
        pagination: { limit: 200, offset: 0, count: 1, total: 1 },
      });
    },
    "/api/methodologies": methodologiesListResponse(),
    "/api/meta/methodology-defaults": methodologyDefaults(),
    "/api/methodologies/": (url: string) => {
      const fp = decodeURIComponent(url.split("/api/methodologies/")[1] ?? "");
      return jsonResponse(methodologyVersion({ fingerprint: fp, params: methodologyFixture() }));
    },
    ...overrides,
  };
}

describe("ScenariosPage", () => {
  it("lists scenarios with what-if tagging and links to detail", async () => {
    stubApi(baseRoutes());
    renderRoute("/scenarios");
    const link = await screen.findByRole("link", { name: "Cost stress" });
    expect(link.getAttribute("href")).toContain(`/scenarios/${SCENARIO_ID}`);
    expect(screen.getByTestId("whatif-tag")).toBeTruthy();
    expect(screen.getByText(/transaction_cost_bps=25/)).toBeTruthy();
  });

  it("creates a scenario and posts name/base/overrides", async () => {
    const calls = stubApi(baseRoutes());
    renderRoute("/scenarios");
    fireEvent.click(await screen.findByRole("button", { name: "New scenario" }));

    fireEvent.change(await screen.findByLabelText("Scenario name"), {
      target: { value: "My what-if" },
    });
    // Wait for the parameter form to hydrate from defaults, then edit a leaf.
    const cost = await screen.findByLabelText("Transaction cost (bps)");
    await waitFor(() => expect((cost as HTMLInputElement).value).toBe("0"));
    fireEvent.change(cost, { target: { value: "25" } });
    fireEvent.click(screen.getByRole("button", { name: "Create scenario" }));

    await waitFor(() => {
      const post = calls.find((c) => c.url.includes("/api/scenarios") && c.method === "POST");
      expect(post).toBeTruthy();
      const body = post?.body as {
        name: string;
        base_methodology: string;
        overrides: Record<string, unknown>;
      };
      expect(body.name).toBe("My what-if");
      expect(body.base_methodology).toBe("default");
      expect(body.overrides).toEqual({ transaction_cost_bps: 25 });
    });
  });
});

describe("ScenarioDetailPage", () => {
  it("deep-links to a scenario and shows config + run history", async () => {
    const calls = stubApi(baseRoutes());
    renderRoute(`/scenarios/${SCENARIO_ID}`);
    expect(await screen.findByText("Cost stress")).toBeTruthy();
    expect(screen.getByTestId("execute-panel")).toBeTruthy();
    expect(screen.getByTestId("run-history").textContent).toContain("succeeded");
    expect(calls.some((c) => c.url.includes(`/api/scenarios/${SCENARIO_ID}`))).toBe(true);
  });

  it("executes a what-if run and polls it to completion", async () => {
    let runStatus = "running";
    const calls = stubApi(
      baseRoutes({
        "/api/scenarios/": (url: string, init?: RequestInit) => {
          if (url.includes(`/api/scenarios/${SCENARIO_ID}/execute`) && init?.method === "POST") {
            return jsonResponse(
              {
                scenario_id: SCENARIO_ID,
                name: "Cost stress",
                run_id: RUN_WHATIF,
                status: "queued",
                created: true,
                methodology_fingerprint: FP_BASE,
                as_of_date: "2024-06-28",
                knowledge_cutoff: "2024-06-28T00:00:00Z",
                invalidation: { stages: [] },
              },
              202,
            );
          }
          if (url.includes(`/api/scenarios/${SCENARIO_ID}`)) {
            return jsonResponse(scenarioDetail());
          }
          return jsonResponse({ detail: "not found" }, 404);
        },
        "/api/runs/": (url: string) => {
          if (url.includes(`/api/runs/${RUN_WHATIF}`)) {
            return jsonResponse({
              ...runSummary(RUN_WHATIF, {
                status: runStatus,
                progress: runStatus === "succeeded" ? 1 : 0.4,
              }),
              row_counts: { analyst_metric: 1200 },
            });
          }
          return jsonResponse(runSummary(RUN_BASE));
        },
      }),
    );
    // Flip to succeeded after the first poll.
    const origFetch = globalThis.fetch;
    let polls = 0;
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url =
          typeof input === "string" ? input : input instanceof URL ? input.toString() : input.url;
        if (url.includes(`/api/runs/${RUN_WHATIF}`)) {
          polls += 1;
          runStatus = polls > 1 ? "succeeded" : "running";
        }
        return origFetch(input, init);
      }),
    );

    renderRoute(`/scenarios/${SCENARIO_ID}`);
    fireEvent.click(await screen.findByRole("button", { name: "Execute scenario" }));

    await waitFor(() => {
      const post = calls.find((c) => c.url.includes("/execute") && c.method === "POST");
      expect(post).toBeTruthy();
      const body = post?.body as { as_of_date: string };
      expect(body.as_of_date).toBe("2024-06-28");
    });
    // Polls until terminal, then shows the finished what-if run.
    expect(await screen.findByTestId("tracked-run-rows", {}, { timeout: 6000 })).toBeTruthy();
    expect(screen.getByTestId("tracked-run").textContent).toContain("what-if");
  });

  it("renders compare deltas with a low-agreement warning", async () => {
    stubApi(baseRoutes());
    renderRoute(`/scenarios/${SCENARIO_ID}?tab=compare`);
    expect(await screen.findByTestId("compare-panel")).toBeTruthy();
    expect(screen.getByTestId("low-agreement-warning").textContent).toContain("0.500");
    expect(screen.getByTestId("spearman-value").textContent).toBe("0.500");
    expect(screen.getByTestId("universe-note").textContent).toContain("compatible");
    expect(screen.getByTestId("transition-counts").textContent).toContain("ranked→ranked: 2");
    // Per-analyst delta row
    expect(screen.getByText("AN_1")).toBeTruthy();
    expect(screen.getByText("+0.050")).toBeTruthy();
  });

  it("submits a sensitivity sweep with the correct payload", async () => {
    const calls = stubApi(baseRoutes());
    renderRoute(`/scenarios/${SCENARIO_ID}?tab=sweep`);
    fireEvent.change(await screen.findByLabelText("Sweep parameter path"), {
      target: { value: "family_weights.return_skill" },
    });
    fireEvent.change(screen.getByLabelText(/Sweep values/), {
      target: { value: "0.2, 0.6" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Run sweep" }));

    await waitFor(() => {
      const post = calls.find((c) => c.url.includes("/sensitivity") && c.method === "POST");
      expect(post).toBeTruthy();
      const body = post?.body as {
        scenario_id: string;
        param_path: string;
        values: unknown[];
        as_of_date: string;
      };
      expect(body.scenario_id).toBe(SCENARIO_ID);
      expect(body.param_path).toBe("family_weights.return_skill");
      expect(body.values).toEqual([0.2, 0.6]);
      expect(body.as_of_date).toBe("2024-06-28");
    });
    expect(await screen.findByTestId("sweep-submitted")).toBeTruthy();
    expect(screen.getByTestId("sweep-submitted").textContent).toContain("2 combinations");
  });

  it("blocks sweeps over the 25-combination cap client-side", async () => {
    const calls = stubApi(baseRoutes());
    renderRoute(`/scenarios/${SCENARIO_ID}?tab=sweep`);
    fireEvent.change(await screen.findByLabelText("Sweep parameter path"), {
      target: { value: "transaction_cost_bps" },
    });
    fireEvent.change(screen.getByLabelText(/Sweep values/), {
      target: { value: Array.from({ length: 26 }, (_, i) => String(i)).join(",") },
    });
    fireEvent.click(screen.getByRole("button", { name: "Run sweep" }));

    expect(await screen.findByTestId("sweep-form-error")).toBeTruthy();
    expect(screen.getByTestId("sweep-form-error").textContent).toContain("25");
    expect(calls.some((c) => c.url.includes("/sensitivity") && c.method === "POST")).toBe(false);
  });
});
