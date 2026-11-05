import { expect, test } from "@playwright/test";
import { waitForRunIdInUrl } from "./helpers";

test.describe("backtests", () => {
  test("builder validation catches dynamic selection without lookback", async ({ page }) => {
    await page.goto("/backtests");
    await waitForRunIdInUrl(page);
    await page.getByRole("button", { name: "New backtest" }).click();

    const builder = page.getByTestId("backtest-builder");
    await expect(builder).toBeVisible();

    // top_n is a dynamic selection: score_lookback_sessions is required.
    await builder.getByLabel("Selection").selectOption("top_n");
    await builder.getByRole("button", { name: "Submit backtest" }).click();

    await expect(builder.locator("ul[role='alert']")).toContainText("score_lookback_sessions");
    // Client-side validation stops the submit — no navigation happened.
    await expect(page).toHaveURL(/\/backtests(\?|$)/);
  });

  test("submit a minimal backtest and watch stats render", async ({ page }) => {
    test.setTimeout(300_000);

    await page.goto("/backtests");
    await waitForRunIdInUrl(page);
    await page.getByRole("button", { name: "New backtest" }).click();

    const builder = page.getByTestId("backtest-builder");
    await expect(builder).toBeVisible();
    await builder.getByLabel("Backtest name").fill("E2E smoke backtest");
    // Bound the simulation window so the small-scale run finishes quickly.
    await builder.getByLabel("Start date").fill("2025-01-02");
    await builder.getByLabel("End date").fill("2025-03-31");

    await builder.getByRole("button", { name: "Submit backtest" }).click();

    // Submission navigates to the detail page, which polls until terminal.
    await expect(page).toHaveURL(/\/backtests\/bt-/);
    await expect(page.getByTestId("backtest-results")).toBeVisible({ timeout: 240_000 });
    await expect(page.getByTestId("stat-cards")).toBeVisible();
    await expect(page.getByTestId("nav-section")).toBeVisible();
  });
});
