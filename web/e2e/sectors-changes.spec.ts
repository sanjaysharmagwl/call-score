import { expect, test } from "@playwright/test";
import { tableRows, waitForRunIdInUrl } from "./helpers";

test.describe("sectors", () => {
  test("list renders and drills into a sector detail", async ({ page }) => {
    await page.goto("/sectors");
    await waitForRunIdInUrl(page);
    await expect(page.getByRole("heading", { name: "Sectors" })).toBeVisible();

    const rows = tableRows(page);
    await expect(rows.first()).toBeVisible();
    expect(await rows.count()).toBeGreaterThan(3);

    await rows.first().getByRole("link").first().click();
    await expect(page).toHaveURL(/\/sectors\//);
    await expect(page.getByRole("heading", { level: 1 })).toBeVisible();
    // Detail page shows coverage + run performance sections.
    await expect(page.getByText("Run performance")).toBeVisible();
  });
});

test.describe("changes", () => {
  test("audit log shows rating events and run lifecycle rows", async ({ page }) => {
    await page.goto("/changes");
    await waitForRunIdInUrl(page);
    await expect(page.getByRole("heading", { name: "Change log" })).toBeVisible();

    const kinds = page.locator('[data-testid="change-kind"]');
    await expect(kinds.first()).toBeVisible();
    await expect(kinds.filter({ hasText: "rating" }).first()).toBeVisible();
    // The seeded calc run contributes run lifecycle entries.
    await expect(kinds.filter({ hasText: "run" }).first()).toBeVisible();
    await expect(tableRows(page).first()).toBeVisible();
  });

  test("CSV export button is offered", async ({ page }) => {
    await page.goto("/changes");
    await expect(page.getByRole("button", { name: "Download CSV" })).toBeVisible();
  });
});
