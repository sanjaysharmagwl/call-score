import { expect, test } from "@playwright/test";
import { expectOk } from "./helpers";

/**
 * Methodology lab: version list, detail inspection, and two-version diff.
 *
 * The seeded fixture has exactly one methodology version (the default used
 * by the fixture run). This spec submits a second run with an overridden
 * methodology via the API so a real diff exists; the run executes on the
 * live coordinator in the background.
 */
test.describe("methodologies", () => {
  test("lists versions, shows detail, and diffs two fingerprints", async ({ page }) => {
    // Ensure a second methodology version exists (transaction_cost_bps=5).
    const submit = await page.request.post("/api/runs", {
      data: { as_of_date: "2025-06-30", methodology: { transaction_cost_bps: 5.0 } },
    });
    expect(submit.status()).toBe(202);

    await page.goto("/methodologies");
    await expect(page.getByRole("heading", { name: "Methodology lab" })).toBeVisible();

    // Fingerprint cells are buttons that select the version detail.
    const fingerprints = page.getByRole("button").filter({ hasText: /^[0-9a-f]{8}/ });
    await expect(fingerprints.first()).toBeVisible();
    // Wait until both versions are listed.
    await expect
      .poll(async () => fingerprints.count(), { timeout: 30_000 })
      .toBeGreaterThanOrEqual(2);

    // Select the first version -> detail panel with the params view.
    await fingerprints.nth(0).click();
    await expect(page.getByTestId("methodology-detail")).toBeVisible();
    await expect(page.getByTestId("methodology-params-view")).toBeVisible();

    // Mark it as diff side A, then select the other version and mark B.
    await page.getByRole("button", { name: "Diff A" }).click();
    await fingerprints.nth(1).click();
    await page.getByRole("button", { name: "Diff B" }).click();

    await expect(page.getByTestId("methodology-diff-panel")).toBeVisible();
    await expect(page.getByTestId("methodology-diff")).toBeVisible();
    await expect(page.getByTestId("methodology-diff-identical")).toHaveCount(0);
  });

  test("diff endpoint returns changed leaves for the two seeded variants", async ({ page }) => {
    const list = await expectOk(await page.request.get("/api/methodologies"));
    const versions = (list as { data: { fingerprint: string }[] }).data;
    expect(versions.length).toBeGreaterThanOrEqual(2);

    const diff = await expectOk(
      await page.request.get("/api/methodologies/diff", {
        params: { a: versions[0].fingerprint, b: versions[1].fingerprint },
      }),
    );
    const body = diff as { identical?: boolean; changes?: unknown[] };
    expect(body.identical).toBe(false);
    expect(Array.isArray(body.changes)).toBeTruthy();
  });
});
