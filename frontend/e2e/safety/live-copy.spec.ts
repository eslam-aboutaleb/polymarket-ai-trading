import { test, expect } from "@playwright/test";

test("live copying requires explicit activation and preserves unknown orders", async ({ page }) => {
  let active = false;
  let simulated = true;
  let enabled = false;
  let activations = 0;
  const hash = "0x" + "a".repeat(64);
  await page.route("**/api/**", async (route) => {
    const path = new URL(route.request().url()).pathname;
    if (path === "/api/auth/me")
      return route.fulfill({ json: { id: 1, wallet_address: "0x123", is_admin: false } });
    if (path === "/api/v1/risk/summary")
      return route.fulfill({
        json: {
          global: { state: "RUNNING" },
          wallet: { state: active ? "RUNNING" : "RECOVERY_REQUIRED" },
          live_trading_enabled: true,
          live_copy_trading_enabled: true,
          live_execution_available: active && !simulated,
          copy_trading_enabled: enabled,
          simulation_mode: simulated,
          initial_capital: 1000,
        },
      });
    if (path === "/api/v1/risk/activate") {
      activations++;
      expect(route.request().postDataJSON()).toEqual({ scope: "user", initial_capital: 1000 });
      active = true;
      simulated = false;
      return route.fulfill({ json: { state: "RUNNING" } });
    }
    if (path === "/api/settings/copy-trading") {
      const data = route.request().postDataJSON();
      if ("simulation_mode" in data) simulated = data.simulation_mode;
      if ("copy_trading_enabled" in data) enabled = data.copy_trading_enabled;
      return route.fulfill({ json: {} });
    }
    if (path === "/api/v1/risk/orders")
      return route.fulfill({ json: [{ order_hash: hash, status: "unknown", trade_size: 5 }] });
    if (path.endsWith("/reconcile"))
      return route.fulfill({ status: 409, json: { detail: "EXCHANGE_ORDER_STATE_UNVERIFIED" } });
    if (path.includes("experiments")) return route.fulfill({ json: { runs: [] } });
    if (path.includes("pnl/by-trader"))
      return route.fulfill({ json: { traders: [], totals: {}, data_quality: {} } });
    if (path.endsWith("/copy-trades/pnl")) return route.fulfill({ json: { daily_pnl: 0 } });
    if (path.includes("evaluation")) return route.fulfill({ json: { rows: [] } });
    return route.fulfill({ json: [] });
  });
  await page.goto("/copy-trading");
  await expect(page.getByRole("heading", { name: "Copy Trading Execution" })).toBeVisible();
  expect(activations).toBe(0);
  await page.getByRole("button", { name: "Activate Live Copying", exact: true }).click();
  await expect(page.getByRole("dialog")).toContainText("real funds");
  expect(activations).toBe(0);
  await page
    .getByRole("dialog")
    .getByRole("button", { name: "Activate Live", exact: true })
    .click();
  await expect(
    page.getByText("Enabled · Live account · Exchange execution available"),
  ).toBeVisible();
  expect(activations).toBe(1);
  await page.getByRole("button", { name: "Check Exchange" }).click();
  await expect(page.getByRole("alert")).toContainText("EXCHANGE_ORDER_STATE_UNVERIFIED");
  await expect(page.getByText("unknown · $5.00")).toBeVisible();
  await page.getByRole("button", { name: "Enable Simulation", exact: true }).click();
  await expect(page.getByText("Enabled · Simulation · Exchange execution blocked")).toBeVisible();
});
