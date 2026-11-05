import { expect, test } from "@playwright/test";

/**
 * First-use guidance against a completely empty database (the `empty`
 * Playwright project points at a second webServer with a fresh bootstrapped
 * DB — zero calc runs). This is the only spec in that project.
 */
test.describe("empty first-use state", () => {
  test("overview shows seed-and-run guidance when no runs exist", async ({ page }) => {
    await page.goto("/");
    await expect(page.getByRole("heading", { name: "Overview" })).toBeVisible();

    // Guidance empty state, not the dashboard.
    await expect(page.getByRole("heading", { name: "No scoring runs yet" })).toBeVisible();
    await expect(page.getByText("just seed small")).toBeVisible();
    await expect(page.getByTestId("overview-dashboard")).toHaveCount(0);

    // The header selector reports the empty runs list.
    await expect(page.getByRole("heading", { name: "No calc runs" })).toBeVisible();
  });

  test("leaderboard renders its shell without a run", async ({ page }) => {
    await page.goto("/leaderboard");
    await expect(page.getByRole("heading", { name: "Leaderboard", exact: true })).toBeVisible();
    // NOTE: with zero runs the table stays in its loading state — the
    // leaderboard query is disabled (no run_id) so `isPending` never
    // resolves. Reported as a product bug; the page shell still renders.
    await expect(page.getByLabel("Metric filter")).toBeVisible();
  });
});
