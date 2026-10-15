import { test, expect } from "../fixtures/base";

/**
 * Whale monitoring: alert settings persistence and configuration controls.
 */

test.describe("Whale monitoring", () => {
  test.beforeEach(async ({ adminPage: page }) => {
    await page.goto("/whales");
    await expect(page.getByRole("heading", { name: "Whales" })).toBeVisible();
  });

  test("renders the alert settings form", async ({ adminPage: page }) => {
    await expect(page.getByText("Min notional (USD)")).toBeVisible();
    await expect(page.getByText("Leaderboard whale set")).toBeVisible();
    await expect(page.getByText("Auto-copy whale trades")).toBeVisible();
    await expect(page.getByRole("button", { name: "Save Settings" })).toBeVisible();
  });

  test("saves the minimum-notional threshold", async ({ adminPage: page }) => {
    const input = page.getByRole("spinbutton").first();
    await input.fill("2500");
    await page.getByRole("button", { name: "Save Settings" }).click();
    await expect(page.getByText(/saved|Settings saved/i).first()).toBeVisible({ timeout: 15_000 });
  });
});
