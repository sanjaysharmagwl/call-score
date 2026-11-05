import { expect, test } from "@playwright/test";
import { DELISTED_NAME, tableRows } from "./helpers";

test.describe("security drill-down", () => {
  test("search -> delisted security detail shows delisted badge", async ({ page }) => {
    await page.goto("/securities");
    await expect(page.getByRole("heading", { name: "Securities" })).toBeVisible();

    // Search by name; the synth universe contains deliberate delistings.
    await page.getByLabel("Search").fill(DELISTED_NAME);
    await page.getByRole("button", { name: "Apply" }).click();

    const rows = tableRows(page);
    await expect(rows.first()).toBeVisible();
    await rows.first().getByRole("link").first().click();

    await expect(page).toHaveURL(/\/securities\/SEC_/);
    await expect(page.getByTestId("delisted-badge")).toBeVisible();
    await expect(page.getByTestId("delisting-banner")).toContainText("Delisted");
  });

  test("sector deep link from security detail reaches the sector page", async ({ page }) => {
    await page.goto("/securities");
    await expect(tableRows(page).first()).toBeVisible();
    await tableRows(page).first().getByRole("link").first().click();
    await expect(page).toHaveURL(/\/securities\/SEC_/);

    // Not every security has a sector link; only assert when present.
    const sectorLink = page.locator('a[href^="/sectors/"]').first();
    if (await sectorLink.count()) {
      await sectorLink.click();
      await expect(page).toHaveURL(/\/sectors\//);
      await expect(page.getByRole("heading", { level: 1 })).toBeVisible();
    }
  });
});
