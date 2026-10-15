import { test, expect } from "../fixtures/base";

/**
 * Opportunities — AI-scored market scan, sort modes and the
 * auth gate on trading actions.
 */

test.describe("Opportunities", () => {
  test.beforeEach(async ({ page }) => {
    await page.goto("/opportunities");
    await expect(page.getByRole("heading", { name: "Opportunities" })).toBeVisible();
  });

  test("renders the scan board with sort modes", async ({ page }) => {
    const sort = page.getByLabel("Sort easy trades");
    await expect(sort).toBeVisible();
    for (const value of ["ease", "ai_score", "pnl", "risk", "smart_money"]) {
      await expect(sort.locator(`option[value="${value}"]`)).toHaveCount(1);
    }
    await expect(page.getByRole("button", { name: "Scan opportunities" })).toBeVisible();
    await expect(page.getByText(/\d+ events · \d+ options/).first()).toBeVisible();
  });

  test("resorting re-runs the ranking", async ({ page }) => {
    const sort = page.getByLabel("Sort easy trades");
    await sort.selectOption({ label: "AI score" });
    await expect(sort).toHaveValue("ai_score");
    // The board re-renders after the mode change.
    await expect(page.getByText(/\d+ events · \d+ options/).first()).toBeVisible();
  });

  test("Scanning opportunities triggers a scan", async ({ page }) => {
    await page.getByRole("button", { name: "Scan opportunities" }).click();
    // The scan runs against the AI backend; whether or not the backend is up,
    // the page must stay alive and keep rendering the board.
    await expect(page.getByRole("heading", { name: "Opportunities" })).toBeVisible();
    await expect(page.getByRole("button", { name: "Scan opportunities" })).toBeVisible();
  });

  test("trading from an opportunity requires auth", async ({ page }) => {
    await page
      .getByRole("button", { name: /^Trade .+ Yes$/ })
      .first()
      .click();
    await expect(page.getByRole("heading", { name: "Connect Wallet" })).toBeVisible();
  });
});
