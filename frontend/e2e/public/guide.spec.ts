import { test, expect } from "../fixtures/base";

/**
 * User guide (public): contents navigation and the interactive tutorial overlay.
 */

test.describe("Your Guide", () => {
  test.beforeEach(async ({ page }) => {
    await page.goto("/guide");
    await expect(page.getByRole("heading", { name: "📖 Your Guide" })).toBeVisible();
  });

  test("renders the guide contents", async ({ page }) => {
    await expect(
      page.getByText("Everything you need to know about using the Polymarket AI Trading Bot"),
    ).toBeVisible();
    await expect(page.getByRole("button", { name: /Getting Started/ }).first()).toBeVisible();
    await expect(
      page.getByRole("button", { name: /Multi-Layer Risk Protection/ }).first(),
    ).toBeVisible();
    await expect(page.getByRole("button", { name: /Arbitrage Detection/ }).first()).toBeVisible();
  });

  test("selecting a contents entry shows its section", async ({ page }) => {
    // The sidebar entry (no space after the emoji) is distinct from the
    // inline section accordion, so match it precisely.
    await page.getByRole("button", { name: "🛡️Stop Loss & Take Profit" }).click();
    await expect(
      page.getByRole("heading", { name: /Stop Loss & Take Profit/ }).first(),
    ).toBeVisible();
  });

  test("tutorial overlay opens, advances and can be dismissed", async ({ page }) => {
    await page.getByRole("button", { name: "Start Tutorial" }).click();

    // The overlay shows a step counter and its own close button.
    await expect(page.getByText(/^1 \/ \d+$/)).toBeVisible();
    const closeTutorial = page.getByRole("button", { name: "Close tutorial" });
    await expect(closeTutorial).toBeVisible();

    // Advancing to the next step updates the counter.
    const next = page.getByRole("button", { name: /Next|→/ }).first();
    if (await next.count()) {
      await next.click();
      await expect(page.getByText(/^2 \/ \d+$/)).toBeVisible();
    }

    await closeTutorial.click();
    await expect(closeTutorial).toBeHidden();
  });
});
