import { test, expect } from "../fixtures/base";

/**
 * Copy Trading landing for visitors: the public pitch plus the auth gate.
 */

test.describe("Copy Trading — public", () => {
  test("shows the pitch and gates the dashboard behind login", async ({ page }) => {
    await page.goto("/copy-trading");
    await expect(page.getByRole("heading", { name: "Copy Trading" })).toBeVisible();
    await expect(
      page.getByText(/Automatically mirror the trades of top Polymarket traders/),
    ).toBeVisible();

    await page.getByRole("button", { name: "Connect Wallet to Get Started" }).click();
    await expect(page.getByRole("heading", { name: "Connect Wallet" })).toBeVisible();
  });
});
