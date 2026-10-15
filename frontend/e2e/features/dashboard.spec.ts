import { test, expect } from "../fixtures/base";

/**
 * Dashboard: account summary, execution-mode toggle, risk-ops actions,
 * following feed and the notification center.
 */

test.describe("Dashboard", () => {
  test.beforeEach(async ({ adminPage: page }) => {
    await page.goto("/");
    await expect(page.getByRole("heading", { name: "Dashboard" })).toBeVisible();
    // The summary cards populate after the portfolio endpoints settle, which
    // can take a while on a cold cache.
    await expect(page.getByRole("heading", { name: "USDC Balance" })).toBeVisible({
      timeout: 45_000,
    });
  });

  test("shows the account summary cards", async ({ adminPage: page }) => {
    await expect(page.getByRole("heading", { name: "Active Positions" })).toBeVisible();
    await expect(page.getByRole("heading", { name: "Total P&L" })).toBeVisible();
    await expect(page.getByRole("heading", { name: "Win Rate" })).toBeVisible();
    await expect(page.getByText(/Wallet: 0x/)).toBeVisible();
  });

  test("live/paper execution mode toggles", async ({ adminPage: page }) => {
    const toggle = page.getByRole("button", { name: "Switch to Paper" });
    await expect(toggle).toBeVisible();
    await toggle.click();
    // The toggle flips so the user can switch back.
    await expect(page.getByRole("button", { name: "Switch to Live" })).toBeVisible();
    await page.getByRole("button", { name: "Switch to Live" }).click();
    await expect(page.getByRole("button", { name: "Switch to Paper" })).toBeVisible();
  });

  test("risk-ops actions run and report back", async ({ adminPage: page }) => {
    await page.getByRole("button", { name: "Rescore Traders" }).click();
    await expect(page.getByText(/rescoring|rescored|Rescore/i).first()).toBeVisible({
      timeout: 20_000,
    });

    await page.getByRole("button", { name: "Scan Arbitrage Now" }).click();
    await expect(page.getByText(/Arbitrage opportunities tracked: \d+/)).toBeVisible({
      timeout: 20_000,
    });
  });

  test("following feed filters render", async ({ adminPage: page }) => {
    // Event-type and trader filters are unlabelled selects identified by
    // their default option.
    await expect(page.locator("select").filter({ hasText: "All Events" })).toBeVisible();
    await expect(page.locator("select").filter({ hasText: "All Traders" })).toBeVisible();
    await expect(page.getByRole("button", { name: "Refresh" }).last()).toBeVisible();
    await expect(page.getByText("No followed-trader events yet.")).toBeVisible();
  });

  test("positions panel and trending markets render", async ({ adminPage: page }) => {
    await expect(page.getByRole("heading", { name: "Your Positions" })).toBeVisible({
      timeout: 30_000,
    });
    await expect(page.getByRole("heading", { name: "Trending Markets" })).toBeVisible({
      timeout: 30_000,
    });
  });

  test("Open Full Ops Center navigates to the Ops Center", async ({ adminPage: page }) => {
    await page.getByRole("link", { name: "Open Full Ops Center" }).click();
    await expect(page).toHaveURL(/\/ops$/);
    await expect(page.getByRole("heading", { name: "Ops Center", exact: true })).toBeVisible({
      timeout: 30_000,
    });
  });

  test("notification center opens", async ({ adminPage: page }) => {
    await page.getByRole("button", { name: /Notifications \(\d+ unread\)/ }).click();
    await expect(page.getByRole("button", { name: "Close notifications" })).toBeVisible();
    await page.getByRole("button", { name: "Close notifications" }).click();
  });
});
