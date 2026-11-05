import { expect, test } from "@playwright/test";

/**
 * What-if scenario journey: create -> execute (real second pipeline run) ->
 * compare tab shows baseline-vs-what-if deltas.
 *
 * The scenario overrides `transaction_cost_bps` so the effective methodology
 * (and therefore the what-if run) differs from the baseline run — an empty
 * override set would be idempotent with the baseline run.
 */
test.describe("scenario", () => {
  test("create, execute and compare a what-if run", async ({ page }) => {
    test.setTimeout(240_000);

    await page.goto("/scenarios");
    await page.getByRole("button", { name: "New scenario" }).click();

    const form = page.getByTestId("scenario-form");
    await form.getByLabel("Scenario name").fill("E2E cost sensitivity");

    // Wait for the base methodology defaults to load, then override one leaf.
    const costField = form.getByLabel("Transaction cost (bps)");
    await expect(costField).toBeVisible({ timeout: 30_000 });
    await costField.fill("25");
    await expect(form.getByTestId("overrides-preview")).toContainText("transaction_cost_bps");

    await form.getByRole("button", { name: "Create scenario" }).click();

    // Navigates to the scenario detail page.
    await expect(page).toHaveURL(/\/scenarios\/[^/?]+/);
    await expect(page.getByTestId("whatif-tag")).toBeVisible();

    // Execute the what-if run (as_of defaults to the latest succeeded run).
    await page.getByRole("button", { name: "Execute scenario" }).click();
    const tracked = page.getByTestId("tracked-run");
    await expect(tracked).toBeVisible();
    await expect(tracked).toContainText("succeeded", { timeout: 180_000 });

    // Discover the what-if run id via the API for an unambiguous compare.
    const scenarioId = new URL(page.url()).pathname.split("/").pop() as string;
    const detail = await page.request.get(`/api/scenarios/${scenarioId}`);
    expect(detail.ok()).toBeTruthy();
    const scenario = (await detail.json()) as { run_ids?: string[] };
    const whatIfRun = scenario.run_ids?.at(-1);
    expect(whatIfRun).toBeTruthy();

    // Compare tab: pick the seeded baseline run explicitly as side A.
    await page.getByRole("button", { name: "Compare" }).click();
    await page.getByLabel("Baseline run").selectOption("2025-06-30-fee61dc02088");
    await page.getByLabel("What-if run").selectOption(whatIfRun as string);

    await expect(page.getByTestId("compare-panel")).toBeVisible({ timeout: 60_000 });
    await expect(page.getByTestId("spearman-value")).toBeVisible();
    await expect(page.getByTestId("transition-counts")).toBeVisible();
  });
});
