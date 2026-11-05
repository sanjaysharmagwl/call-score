import { afterEach, describe, expect, it } from "vitest";
import {
  cleanup,
  fireEvent,
  renderRoute,
  runsResponse,
  screen,
  sectorItem,
  securityListItem,
  stubApi,
  waitFor,
} from "./test-utils";

afterEach(cleanup);

function baseRoutes() {
  return {
    "/api/runs": runsResponse(),
    "/api/sectors": {
      data: [sectorItem(), sectorItem({ sector_id: "SEC_ENERGY", name: "Energy" })],
      pagination: { limit: 500, offset: 0, total: 2 },
    },
    "/api/securities": {
      data: [
        securityListItem(),
        securityListItem({
          security_id: "SEC_2",
          name: "Dead Co",
          ticker: "DEAD",
          is_delisted: true,
          classification_to_date: "2023-05-01",
        }),
      ],
      pagination: { limit: 50, offset: 0, total: 2 },
    },
  };
}

describe("SecuritiesPage", () => {
  it("lists securities and shows the delisted badge without hiding rows", async () => {
    stubApi(baseRoutes());
    renderRoute("/securities");

    expect(await screen.findByText("ACME")).toBeTruthy();
    expect(screen.getByText("Dead Co")).toBeTruthy();
    expect(screen.getByText("DEAD")).toBeTruthy();
    expect(screen.getAllByTestId("delisted-badge")).toHaveLength(1);
  });

  it("emits q/sector/cap_bucket/as_of filters to the API", async () => {
    const calls = stubApi(baseRoutes());
    renderRoute("/securities");
    await screen.findByText("ACME");

    fireEvent.change(screen.getByLabelText(/search/i), { target: { value: "acme" } });
    fireEvent.change(screen.getByLabelText("Sector"), {
      target: { value: "SEC_ENERGY" },
    });
    fireEvent.change(screen.getByLabelText("Cap bucket"), { target: { value: "micro" } });
    fireEvent.change(screen.getByLabelText("As of"), { target: { value: "2024-01-31" } });
    fireEvent.click(screen.getByRole("button", { name: "Apply" }));

    await waitFor(() => {
      const hit = calls.find(
        (c) =>
          c.url.includes("/api/securities") &&
          c.url.includes("q=acme") &&
          c.url.includes("sector=SEC_ENERGY") &&
          c.url.includes("cap_bucket=micro") &&
          c.url.includes("as_of=2024-01-31"),
      );
      expect(hit).toBeTruthy();
    });
  });

  it("honours deep-linked filter search params", async () => {
    const calls = stubApi(baseRoutes());
    renderRoute("/securities?sector=SEC_ENERGY&cap_bucket=small");

    await waitFor(() => {
      const hit = calls.find(
        (c) =>
          c.url.includes("/api/securities") &&
          c.url.includes("sector=SEC_ENERGY") &&
          c.url.includes("cap_bucket=small"),
      );
      expect(hit).toBeTruthy();
    });
    // The sector select reflects the deep-linked value.
    await waitFor(() => {
      expect((screen.getByLabelText("Sector") as HTMLSelectElement).value).toBe("SEC_ENERGY");
    });
  });
});
