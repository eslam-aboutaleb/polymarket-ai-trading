import { test, expect } from "../fixtures/base";

/**
 * AI Analysis (public): the SSE market-analysis page driven by query params.
 */

test.describe("AI Analysis", () => {
  test("renders market header from query params", async ({ page }) => {
    await page.goto(
      "/analysis?market_title=Will+BTC+hit+100k&market_description=Bitcoin+price+market" +
        "&yes_price=0.62&no_price=0.38&volume_24h=1250000",
    );
    await expect(page.getByRole("heading", { name: "Will BTC hit 100k" })).toBeVisible();
    await expect(page.getByText("YES 62¢")).toBeVisible();
    await expect(page.getByText("NO 38¢")).toBeVisible();
    await expect(page.getByText("Vol $1,250,000")).toBeVisible();
  });

  test("analysis failure degrades to an inline error, not a crash", async ({ page }) => {
    // Without the AI backend running the SSE stream fails; the page must show
    // the error state and keep the shell intact.
    await page.goto("/analysis?market_title=Error+Path+Test&yes_price=0.5&no_price=0.5");
    await expect(
      page
        .getByText(/Analysis error:|Analysis complete|Running independent analysis|📊|Analysis/)
        .first(),
    ).toBeVisible({ timeout: 20_000 });
    // Shell intact.
    await expect(page.getByRole("heading", { name: "Error Path Test" })).toBeVisible();
  });
});
