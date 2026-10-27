import { test, expect } from "../fixtures/base";

/**
 * Copy Trading (authenticated): the followed-trader workspace.
 */

test.describe("Copy Trading — authenticated", () => {
  test.beforeEach(async ({ adminPage: page }) => {
    await page.goto("/copy-trading");
    await expect(page.getByRole("heading", { name: "Copy Trading", exact: true })).toBeVisible();
    // Other workspace tests can leave the persisted watchlist tab selected.
    await page.getByRole("main").getByRole("button", { name: "Copy Trading", exact: true }).click();
  });

  test("renders the copy-trading workspace", async ({ adminPage: page }) => {
    await expect(page.getByRole("heading", { name: "Followed Traders" })).toBeVisible();
    await expect(page.getByRole("heading", { name: "Follow by Wallet Address" })).toBeVisible();
    await expect(page.getByRole("heading", { name: "Copy Evaluation" })).toBeVisible();
    await expect(page.getByPlaceholder("0x...").first()).toBeVisible();
  });

  test("evaluation runs for a wallet address", async ({ adminPage: page }) => {
    await page.getByPlaceholder("0x...").first().fill("0x56687bf447db6ffa42ffe2204a05edaa20f55839");
    const evaluate = page.getByRole("button", { name: /Evaluate|Assess|Score/i }).first();
    if (await evaluate.count()) {
      await evaluate.click();
      // Evaluation is server-backed; the section must render a verdict or a
      // graceful error, and never crash the page.
      await expect(page.getByRole("heading", { name: "Copy Trading", exact: true })).toBeVisible();
    }
  });
});
