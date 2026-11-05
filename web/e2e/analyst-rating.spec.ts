import { expect, test } from "@playwright/test";
import { tableRows, waitForRunIdInUrl } from "./helpers";

test.describe("analyst drill-down", () => {
  test("leaderboard -> analyst -> rating detail shows explanation", async ({ page }) => {
    await page.goto("/leaderboard");
    await waitForRunIdInUrl(page);
    await expect(tableRows(page).first()).toBeVisible();

    // Open the top-ranked analyst's profile.
    await tableRows(page).first().getByRole("link").first().click();
    await expect(page).toHaveURL(/\/analysts\/AN_[^/?]+/);

    // The ratings table lists intervals with a "detail →" link.
    const detailLink = page.getByRole("link", { name: "detail →" }).first();
    await expect(detailLink).toBeVisible({ timeout: 30_000 });
    await detailLink.click();

    await expect(page).toHaveURL(/\/analysts\/AN_[^/?]+\/ratings\/[^/?]+/);
    const explanation = page.getByTestId("rating-explanation");
    await expect(explanation).toBeVisible();
    await expect(explanation).not.toBeEmpty();
  });
});
