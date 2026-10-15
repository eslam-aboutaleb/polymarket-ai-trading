import { test, expect, gotoMarketsBoard } from "../fixtures/base";

/**
 * Markets (public): browse, category filter, search, sort, market detail drawer
 * and the auth gate on trading actions.
 */

test.describe("Markets — public browsing", () => {
  test.beforeEach(async ({ page }) => {
    await gotoMarketsBoard(page, { waitForContent: true });
  });

  test("renders the market board with live Polymarket data", async ({ page }) => {
    // Category strip from the live categories endpoint.
    for (const category of ["All", "Sports", "Politics", "Crypto"]) {
      await expect(page.getByRole("button", { name: category, exact: true })).toBeVisible();
    }
    // Sort control is a labelled select fed by the markets API.
    await expect(page.getByLabel("Sort markets")).toBeVisible();
    // The board reports how many events/options it loaded.
    await expect(page.getByText(/Showing \d+ events \/ \d+ options/)).toBeVisible();
    // Per-row Yes/No trading pills exist on multi-option markets.
    await expect(page.getByRole("button", { name: /^Trade .+ Yes$/ }).first()).toBeVisible();
  });

  test("search filters the board and can be cleared", async ({ page }) => {
    const search = page.getByPlaceholder(/Search markets/);
    await search.fill("bitcoin");
    await expect(page.getByText(/Showing \d+ events \/ \d+ options/)).toBeVisible();
    await expect(page.getByText(/Bitcoin hit in October/)).toBeVisible();

    await page.getByRole("button", { name: "Clear search" }).click();
    await expect(search).toHaveValue("");
  });

  test("category filter narrows the board", async ({ page }) => {
    const summary = page.getByText(/Showing \d+ events \/ \d+ options/);
    const before = await summary.textContent();

    await page.getByRole("button", { name: "Politics", exact: true }).click();
    await expect(summary).not.toHaveText(before ?? "", { timeout: 15_000 });
  });

  test("sorting can be switched to Newest", async ({ page }) => {
    const sort = page.getByLabel("Sort markets");
    await expect(sort).toHaveValue("volume24hr");
    await sort.selectOption({ label: "Newest" });
    await expect(sort).toHaveValue("startDate");
    // The board still renders after the re-sort.
    await expect(page.getByText(/Showing \d+ events \/ \d+ options/)).toBeVisible();
  });

  test("watchlist tab shows the empty state for a new visitor", async ({ page }) => {
    await page.getByRole("button", { name: /Watchlist/ }).click();
    await expect(
      page.getByText(/No markets found|Try a different category|watchlist/i).first(),
    ).toBeVisible();
  });

  test("market detail drawer opens from a card", async ({ page }) => {
    await page.getByRole("button", { name: "Open market details" }).first().click();
    // The drawer is a fixed side panel (mobile: centered modal) closed via its
    // own close button.
    const closeDrawer = page.getByRole("button", { name: "Close drawer" });
    await expect(closeDrawer).toBeVisible();
    await expect(page.getByRole("heading", { level: 2 }).first()).toBeVisible();
    await closeDrawer.click();
    await expect(closeDrawer).toBeHidden();
  });

  test("trading requires auth: visitors get the login modal", async ({ page }) => {
    await page
      .getByRole("button", { name: /^Trade .+ Yes$/ })
      .first()
      .click();
    // LoginModal opens with the connect flow.
    await expect(page.getByRole("heading", { name: "Connect Wallet" })).toBeVisible();
    await expect(page.getByText("Sign in to access trading features")).toBeVisible();
  });
});

test.describe("Markets — navigation entry points", () => {
  test("the nav menu links every public page", async ({ page }) => {
    await page.goto("/markets");
    await page.getByRole("button", { name: "Open navigation menu" }).click();
    const menu = page.getByRole("menu", { name: "Primary navigation" });
    for (const label of [
      "Markets",
      "Opportunities",
      "Leaderboard",
      "Copy Trading",
      "📖 Your Guide",
    ]) {
      await expect(menu.getByRole("menuitem", { name: label })).toBeVisible();
    }
    // Authenticated-only entries are hidden from visitors.
    await expect(menu.getByRole("menuitem", { name: "Dashboard" })).toHaveCount(0);
    await expect(menu.getByRole("menuitem", { name: "Settings" })).toHaveCount(0);
  });

  test("brand button navigates back to Markets for visitors", async ({ page }) => {
    await page.goto("/leaderboard");
    await page.getByRole("button", { name: "Go to markets" }).click();
    await expect(page).toHaveURL(/\/markets$/);
  });

  test("unknown routes render the 404 page", async ({ page }) => {
    await page.goto("/this-route-does-not-exist");
    await expect(page).toHaveURL(/\/this-route-does-not-exist$/);
    await expect(page.getByRole("heading", { name: "Page not found" })).toBeVisible();
  });
});
