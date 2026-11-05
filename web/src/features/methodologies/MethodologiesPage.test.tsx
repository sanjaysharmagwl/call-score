import { afterEach, describe, expect, it, vi } from "vitest";
import {
  cleanup,
  FP_ALT,
  FP_BASE,
  fireEvent,
  jsonResponse,
  methodologiesListResponse,
  methodologyDefaults,
  methodologyDiffResponse,
  methodologyVersion,
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
    "/api/methodologies/diff": methodologyDiffResponse(),
    "/api/methodologies/": (url: string) => {
      const fp = decodeURIComponent(url.split("/api/methodologies/")[1] ?? "");
      if (fp === FP_BASE || fp === FP_ALT)
        return jsonResponse(methodologyVersion({ fingerprint: fp }));
      return jsonResponse({ detail: "not found" }, 404);
    },
    "/api/methodologies": methodologiesListResponse(),
    "/api/meta/methodology-defaults": methodologyDefaults(),
    ...overrides,
  };
}

describe("MethodologiesPage", () => {
  it("lists stored versions and marks the baseline", async () => {
    stubApi(baseRoutes());
    renderRoute("/methodologies");
    expect(await screen.findByText("Methodology lab")).toBeTruthy();
    expect(await screen.findByTestId("baseline-tag")).toBeTruthy();
    expect(screen.getAllByText("version").length).toBeGreaterThan(0);
  });

  it("opens a version detail with the full parameter tree", async () => {
    const calls = stubApi(baseRoutes());
    renderRoute("/methodologies");
    const fpButton = (await screen.findAllByRole("button", { name: /aaaa/ }))[0];
    fireEvent.click(fpButton);

    const detail = await screen.findByTestId("methodology-detail");
    expect(detail.textContent).toContain(FP_BASE);
    await waitFor(() => {
      expect(detail.querySelector('[data-field="decay_days"]')?.textContent).toBe("90");
    });
    expect(detail.querySelector('[data-field="contract_version"]')?.textContent).toBe("1");
    expect(calls.some((c) => c.url.includes(`/api/methodologies/${FP_BASE}`))).toBe(true);
  });

  it("renders a field-level diff between two fingerprints", async () => {
    stubApi(baseRoutes());
    renderRoute(`/methodologies?a=${FP_BASE}&b=${FP_ALT}`);
    const diff = await screen.findByTestId("methodology-diff");
    expect(diff.textContent).toContain("decay_days");
    expect(diff.textContent).toContain("90");
    expect(diff.textContent).toContain("60");
    expect(diff.textContent).toContain("transaction_cost_bps");
  });

  it("creates a version from defaults and posts the full methodology", async () => {
    const calls = stubApi(
      baseRoutes({
        "/api/methodologies": (_url: string, init?: RequestInit) => {
          if (init?.method === "POST") {
            return jsonResponse(methodologyVersion({ is_baseline: false }), 201);
          }
          return jsonResponse(methodologiesListResponse());
        },
      }),
    );
    renderRoute("/methodologies");
    fireEvent.click(await screen.findByRole("button", { name: "New version" }));

    const decay = await screen.findByLabelText("Decay days");
    await waitFor(() => expect((decay as HTMLInputElement).value).toBe("90"));
    fireEvent.change(decay, { target: { value: "120" } });
    fireEvent.click(screen.getByRole("button", { name: "Store methodology version" }));

    await waitFor(() => {
      const post = calls.find((c) => c.url.includes("/api/methodologies") && c.method === "POST");
      expect(post).toBeTruthy();
      const body = post?.body as {
        methodology: { decay_days: number; contract_version: number };
        is_baseline: boolean;
      };
      expect(body.methodology.decay_days).toBe(120);
      expect(body.methodology.contract_version).toBe(1);
      expect(body.is_baseline).toBe(false);
    });
    expect(await screen.findByTestId("create-success")).toBeTruthy();
  });

  it("surfaces server 422 validation errors", async () => {
    stubApi(
      baseRoutes({
        "/api/methodologies": (_url: string, init?: RequestInit) => {
          if (init?.method === "POST") {
            return jsonResponse(
              {
                detail: [
                  { loc: ["body", "methodology", "decay_days"], msg: "bad value", type: "x" },
                ],
              },
              422,
            );
          }
          return jsonResponse(methodologiesListResponse());
        },
      }),
    );
    renderRoute("/methodologies");
    fireEvent.click(await screen.findByRole("button", { name: "New version" }));
    await screen.findByLabelText("Decay days");
    fireEvent.click(screen.getByRole("button", { name: "Store methodology version" }));
    const err = await screen.findByTestId("create-error");
    expect(err.textContent).toContain("decay_days");
    expect(err.textContent).toContain("bad value");
  });
});
