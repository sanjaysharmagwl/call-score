import { expect, test } from "@playwright/test";
import { AS_OF, latestSucceededRunId, RUN_ID, waitForRunIdInUrl } from "./helpers";

test.describe("overview", () => {
  test("loads dashboard and auto-selects the seeded run", async ({ page }) => {
    await page.goto("/");

    await expect(page.getByRole("heading", { name: "Overview" })).toBeVisible();

    // The header run selector lists the seeded run once runs load. Other
    // specs may add runs, so assert on the fixture run's own option.
    const selector = page.getByLabel("Active calc run");
    await expect(selector).toBeVisible();
    const runOption = selector.locator(`option[value="${RUN_ID}"]`);
    await expect(runOption).toHaveCount(1);
    await expect(runOption).toContainText(AS_OF);
    await expect(runOption).toContainText("succeeded");

    // useActiveRun writes the newest succeeded run into the URL.
    const runId = await waitForRunIdInUrl(page);
    expect(runId).toBe(await latestSucceededRunId(page));

    // The dashboard renders for the active run.
    await expect(page.getByTestId("overview-dashboard")).toBeVisible();
    await expect(page.getByTestId("methodology-fingerprint")).toBeVisible();
  });

  test("run selector switches the active run via the URL", async ({ page }) => {
    await page.goto("/leaderboard");
    const runId = await waitForRunIdInUrl(page);

    // Only one succeeded run exists in the seeded fixture; picking it
    // explicitly keeps run_id stable in the URL.
    await page.getByLabel("Active calc run").selectOption(runId);
    await expect(page).toHaveURL(new RegExp(`run_id=${runId}`));
  });
});
