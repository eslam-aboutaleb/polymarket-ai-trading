import { test, expect } from "../fixtures/base";

/**
 * Market making and backtesting workspaces: empty states and the
 * create-config / create-run forms.
 */

test.describe("Market Making", () => {
  test.beforeEach(async ({ adminPage: page }) => {
    await page.goto("/market-making");
    await expect(page.getByRole("heading", { name: "Market Making" })).toBeVisible();
  });

  test("shows the empty state and opens the config form", async ({ adminPage: page }) => {
    await expect(page.getByText(/No market maker configs yet/)).toBeVisible();

    await page.getByRole("button", { name: "+ New Config" }).click();
    await expect(page.getByRole("heading", { name: "New Market Maker Config" })).toBeVisible();
    await expect(page.getByPlaceholder("0x...")).toBeVisible();
    await expect(page.getByPlaceholder("Market name")).toBeVisible();

    // Strategy switch between Bands and AMM.
    await page.locator("select").filter({ hasText: "Bands" }).first().selectOption("amm");
    await expect(page.getByText(/AMM Liquidity/)).toBeVisible();
  });
});

test.describe("Backtesting", () => {
  test.beforeEach(async ({ adminPage: page }) => {
    await page.goto("/backtesting");
    await expect(page.getByRole("heading", { name: "Backtesting" })).toBeVisible();
  });

  test("shows the empty state and opens the run form", async ({ adminPage: page }) => {
    await expect(page.getByText(/No backtest runs yet/)).toBeVisible();

    await page.getByRole("button", { name: "+ New Backtest" }).click();
    await expect(page.getByRole("heading", { name: "New Backtest" })).toBeVisible();
    await expect(page.getByPlaceholder("My Strategy")).toBeVisible();
    await expect(page.getByPlaceholder("Market condition ID")).toBeVisible();
  });
});
