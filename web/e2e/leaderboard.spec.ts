import { expect, test } from "@playwright/test";
import { BUSY_PEER_GROUP, RUN_ID, tableRows, waitForRunIdInUrl } from "./helpers";

test.describe("leaderboard", () => {
  test("renders ranked rows with rank and score for the active run", async ({ page }) => {
    await page.goto("/leaderboard");
    await waitForRunIdInUrl(page);

    const table = page.getByRole("table");
    await expect(table).toBeVisible();
    const rows = tableRows(page);
    await expect(rows.first()).toBeVisible();
    expect(await rows.count()).toBeGreaterThan(5);

    // First column is Rank ("#n" for ranked analysts), Score column shows a
    // score badge with a numeric value.
    const firstRow = rows.first();
    await expect(firstRow.locator("td").nth(0)).toContainText(/#\d+|—/);
    await expect(firstRow.locator("td").nth(3)).not.toBeEmpty();
  });

  test("peer_group filter narrows rows and deep-links via URL", async ({ page }) => {
    const peerGroup = BUSY_PEER_GROUP;
    await page.goto(`/leaderboard?run_id=${RUN_ID}&peer_group=${encodeURIComponent(peerGroup)}`);

    // The linked filter is applied automatically (draft + applied sync from URL).
    const rows = tableRows(page);
    await expect(rows.first()).toBeVisible();
    const count = await rows.count();
    expect(count).toBeGreaterThan(0);
    expect(count).toBeLessThanOrEqual(10);

    // Every visible row belongs to the filtered peer group (column index 2).
    for (let i = 0; i < count; i++) {
      await expect(rows.nth(i).locator("td").nth(2)).toHaveText(peerGroup);
    }
    await expect(page).toHaveURL(/peer_group=/);
  });

  test("sort by score issues a sorted API request", async ({ page }) => {
    await page.goto("/leaderboard");
    await waitForRunIdInUrl(page);
    await expect(tableRows(page).first()).toBeVisible();

    const [request] = await Promise.all([
      page.waitForRequest(
        (req) => req.url().includes("/api/leaderboard") && req.url().includes("sort=score"),
      ),
      page
        .getByRole("button", { name: /^Score/ })
        .first()
        .click(),
    ]);
    const url = new URL(request.url());
    // Default sort is score desc; one click toggles to asc.
    expect(url.searchParams.get("order")).toBe("asc");
    await expect(tableRows(page).first()).toBeVisible();
  });

  test("analyst name links through to the analyst profile", async ({ page }) => {
    await page.goto("/leaderboard");
    await waitForRunIdInUrl(page);
    await expect(tableRows(page).first()).toBeVisible();

    await tableRows(page).first().getByRole("link").first().click();
    await expect(page).toHaveURL(/\/analysts\/AN_/);
    await expect(page).toHaveURL(/run_id=/);
    await expect(page.getByRole("heading", { level: 1 }).first()).toBeVisible();
  });
});
