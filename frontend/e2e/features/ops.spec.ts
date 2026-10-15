import { test, expect } from "../fixtures/base";

/**
 * Ops Center: risk controls and Binance intelligence sections.
 */

test.describe("Ops Center", () => {
  test.beforeEach(async ({ adminPage: page }) => {
    await page.goto("/ops");
    await expect(page.getByRole("heading", { name: "Ops Center", exact: true })).toBeVisible();
  });

  test("renders the operational sections", async ({ adminPage: page }) => {
    await expect(page.getByText("How To Use Ops Center")).toBeVisible();
    await expect(page.getByText("1. Risk Controls")).toBeVisible();
    await expect(page.getByText("2. Binance Signals")).toBeVisible();
  });

  test("opens the full guide", async ({ adminPage: page }) => {
    // A plain anchor to the guide page.
    await page.getByRole("link", { name: "Open Full Guide" }).click();
    await expect(page.getByRole("heading", { name: "📖 Your Guide" })).toBeVisible();
  });

  test("risk-control actions are reachable", async ({ adminPage: page }) => {
    await expect(
      page.getByRole("button", { name: /Emergency Stop|Resume Trading/ }).first(),
    ).toBeVisible();
  });
});
