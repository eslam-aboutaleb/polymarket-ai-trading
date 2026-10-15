import { test, expect } from "../fixtures/base";

/**
 * Leaderboard (public): top traders table, periods, table sizes, follow gating.
 */

test.describe("Leaderboard", () => {
  test.beforeEach(async ({ page }) => {
    await page.goto("/leaderboard");
    await expect(page.getByRole("heading", { name: "Leaderboard" })).toBeVisible();
  });

  test("shows the top traders table with live data", async ({ page }) => {
    await expect(page.getByRole("button", { name: "Top traders" })).toBeVisible();
    await expect(page.getByRole("button", { name: "Forecasting accuracy" })).toBeVisible();
    await expect(page.getByText(/Top \d+ traders on Polymarket/)).toBeVisible();
    // Podium + rows render.
    await expect(page.getByText(/Vol: \$/).first()).toBeVisible();
    await expect(page.getByRole("button", { name: "Follow" }).first()).toBeVisible();
  });

  test("period filters switch the ranking window", async ({ page }) => {
    await page.getByRole("button", { name: "7 D", exact: true }).click();
    await expect(page.getByText(/Top \d+ traders on Polymarket/)).toBeVisible();
    await page.getByRole("button", { name: "24 H", exact: true }).click();
    await expect(page.getByText(/Top \d+ traders on Polymarket/)).toBeVisible();
  });

  test("table size select changes the row count", async ({ page }) => {
    const sizeSelect = page.locator("select").filter({ hasText: "Top 100" }).first();
    await expect(sizeSelect).toBeVisible();
    await sizeSelect.selectOption({ label: "Top 25" });
    await expect(page.getByText(/Top 25 traders on Polymarket/)).toBeVisible({ timeout: 15_000 });
  });

  test("following a trader requires auth for visitors", async ({ page }) => {
    await page.getByRole("button", { name: "Follow" }).first().click();
    await expect(page.getByRole("heading", { name: "Connect Wallet" })).toBeVisible();
  });

  test("refresh re-fetches the leaderboard", async ({ page }) => {
    await page.getByRole("button", { name: "Refresh" }).click();
    await expect(page.getByText(/Top \d+ traders on Polymarket/)).toBeVisible();
  });
});
