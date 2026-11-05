import { expect, test } from "@playwright/test";
import { waitForRunIdInUrl } from "./helpers";

test.describe("navigation", () => {
  test("refresh preserves run_id and back button works", async ({ page }) => {
    await page.goto("/leaderboard");
    const runId = await waitForRunIdInUrl(page);

    // Hard refresh keeps the deep-linked run selection.
    await page.reload();
    await expect(page).toHaveURL(new RegExp(`run_id=${runId}`));
    await expect(page.getByRole("heading", { name: "Leaderboard" })).toBeVisible();

    // Navigate away via the sidebar, then browser-back restores the page.
    await page.getByRole("link", { name: "Analysts" }).click();
    await expect(page).toHaveURL(/\/analysts/);
    await expect(page).toHaveURL(new RegExp(`run_id=${runId}`));

    await page.goBack();
    await expect(page).toHaveURL(/\/leaderboard/);
    await expect(page).toHaveURL(new RegExp(`run_id=${runId}`));
    await expect(page.getByRole("heading", { name: "Leaderboard" })).toBeVisible();
  });
});
