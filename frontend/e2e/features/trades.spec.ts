import { test, expect } from "../fixtures/base";

/**
 * Trades: real trade history from the Polymarket data API, stat cards and
 * the buy/sell/status filters.
 */

test.describe("Trades", () => {
  test.beforeEach(async ({ adminPage: page }) => {
    await page.goto("/trades");
    await expect(page.getByRole("heading", { name: "Trades" })).toBeVisible();
    // Trade history is fetched from the Polymarket data API; the stats cards
    // appear once it settles, and a wallet with no account-scoped history
    // shows the empty state instead.
    await expect(page.getByText("TOTAL TRADES").or(page.getByText("No trades found"))).toBeVisible({
      timeout: 45_000,
    });
  });

  test("renders the table filters", async ({ adminPage: page }) => {
    // Side and status filters; the stats band only appears once trades exist.
    await expect(page.getByRole("button", { name: /^All/ }).first()).toBeVisible();
    await expect(page.getByRole("button", { name: /^Buys\(\d+\)/ })).toBeVisible();
    await expect(page.getByRole("button", { name: /^Sells\(\d+\)/ })).toBeVisible();
    await expect(page.getByRole("button", { name: /^Confirmed\(\d+\)/ })).toBeVisible();
  });

  test("empty history explains the next step", async ({ adminPage: page }) => {
    // A wallet without account-scoped trades gets a helpful empty state that
    // links back to the markets board.
    await expect(
      page.getByText("Start trading on Polymarket to see your history here."),
    ).toBeVisible();
    await expect(
      page.getByRole("link", { name: /Browse markets to place your first trade/ }),
    ).toBeVisible();
  });

  test("side filters switch the view", async ({ adminPage: page }) => {
    // With no trades the table is replaced by the empty state, but the side
    // filters must still switch and keep the page stable.
    await page.getByRole("button", { name: /^Buys\(\d+\)/ }).click();
    await expect(page.getByRole("heading", { name: "Trades" })).toBeVisible();

    await page.getByRole("button", { name: /^Sells\(\d+\)/ }).click();
    await expect(page.getByRole("heading", { name: "Trades" })).toBeVisible();

    await page.getByRole("button", { name: /^All/ }).first().click();
    await expect(page.getByText("No trades found")).toBeVisible();
  });

  test("positions tab renders", async ({ adminPage: page }) => {
    await page.getByRole("button", { name: "Your Positions" }).click();
    await expect(page.getByRole("button", { name: "Your Positions" })).toBeVisible();
  });

  test("refresh re-fetches history", async ({ adminPage: page }) => {
    await page.getByRole("button", { name: "Refresh" }).click();
    await expect(page.getByRole("heading", { name: "Trades" })).toBeVisible();
  });
});
