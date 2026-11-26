import { test, expect } from "@playwright/test";

test.beforeEach(async ({ page, request }) => {
  await request.post("http://127.0.0.1:8002/__test/reset");
  await page.route("**/api/**", (route) => {
    const path = new URL(route.request().url()).pathname;
    if (path.startsWith("/api/v1/arbitrage/") || path === "/api/trades/arbitrage/opportunities")
      return route.continue();
    if (path === "/api/auth/me")
      return route.fulfill({
        json: { id: 1, wallet_address: "0x" + "11".repeat(20), is_admin: false },
      });
    if (path === "/api/accounts") return route.fulfill({ json: [] });
    if (path === "/api/trades/emergency-stop") return route.fulfill({ json: null });
    return route.fulfill({ json: [] });
  });
  await page.goto("/");
});

test("scanner card says indicative, wide spread is not arbitrage, feasibility shows waterfall", async ({
  page,
}, testInfo) => {
  const panel = page.getByRole("region", { name: "Arbitrage scanner" });
  await expect(panel.getByText("Indicative price gap (not guaranteed profit)")).toBeVisible();
  await expect(panel.getByText("Wide spread (liquidity signal, not arbitrage)")).toBeVisible();
  await expect(panel.getByText("INDICATIVE_ONLY").first()).toBeVisible();
  await expect(panel.getByText("NOT_EXECUTABLE").first()).toBeVisible();
  await expect(panel.getByText("UNKNOWN_FEES").first()).toBeVisible();
  // The wide-spread card is never labeled as an arbitrage gap.
  await expect(panel.getByText(/Gross gap/)).toHaveCount(1);

  await panel.getByLabel("Fee (bps)").fill("100");
  await panel.getByLabel("Fee version").fill("flat-test");
  await panel.getByLabel("Gas (USDC)").fill("0");
  await panel.getByLabel("Settlement cost (USDC)").fill("0");
  await panel.getByRole("button", { name: "Check feasibility" }).click();
  const detail = panel.getByRole("group", { name: "Feasibility detail" });
  await expect(detail.getByText("EXECUTION_READY")).toBeVisible();
  await expect(detail.getByRole("table", { name: "Cost waterfall" })).toBeVisible();
  await expect(detail.getByText(/not atomic/).first()).toBeVisible();
  await expect(detail.getByText(/no profit is assured/i)).toBeVisible();
  await panel.screenshot({ path: testInfo.outputPath("arbitrage-panel.png") });
});

test("missing fees keep the candidate non-executable", async ({ page }) => {
  const panel = page.getByRole("region", { name: "Arbitrage scanner" });
  await panel.getByRole("button", { name: "Check feasibility" }).click();
  const detail = panel.getByRole("group", { name: "Feasibility detail" });
  await expect(detail.getByText("INVALID")).toBeVisible();
  await expect(detail.getByText(/UNKNOWN_FEES/)).toBeVisible();
});
