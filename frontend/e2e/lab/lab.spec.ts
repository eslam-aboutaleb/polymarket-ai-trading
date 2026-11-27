import { test, expect } from "@playwright/test";

const API = "http://127.0.0.1:8002";

test.beforeEach(async ({ page }) => {
  await page.route("**/api/**", (route) => {
    const path = new URL(route.request().url()).pathname;
    if (path.startsWith("/api/backtesting/lab/")) return route.continue();
    if (path === "/api/auth/me")
      return route.fulfill({
        json: { id: 1, wallet_address: "0x" + "11".repeat(20), is_admin: false },
      });
    if (path === "/api/accounts") return route.fulfill({ json: [] });
    if (path === "/api/backtesting/runs") return route.fulfill({ json: { runs: [] } });
    if (path === "/api/backtesting/strategies") return route.fulfill({ json: { strategies: [] } });
    return route.fulfill({ json: {} });
  });
});

test("replay shows watermark, delayed fill cost, baselines and verifies reproducibly", async ({
  page,
  request,
}, testInfo) => {
  const recipe = await (await request.post(`${API}/__test/recipe`)).json();
  await page.goto("/backtesting");
  const panel = page.getByRole("region", { name: "Event replay laboratory" });
  await panel.getByLabel("Event replay recipe JSON").fill(JSON.stringify(recipe));
  await panel.getByRole("button", { name: "Run replay" }).click();
  await expect(panel.getByRole("note")).toContainText("SIMULATED REPLAY");
  await expect(panel.getByRole("table", { name: "Benchmark comparison" })).toContainText(
    "passive_holding",
  );
  await expect(panel.getByText("Ledger conserves value").locator("xpath=..")).toContainText("Yes");
  await panel.getByText("Fills and full report").click();
  // 1500 ms decision latency: the book moved to 0.60 before the order arrived.
  await expect(panel.locator("pre")).toContainText('"price": "0.60"');
  await panel.getByRole("button", { name: "Verify lab replay" }).click();
  await expect(panel.getByRole("status")).toContainText("identical result");
  await panel.screenshot({ path: testInfo.outputPath("lab-replay.png") });
});

test("unknown fees render as Unknown, never zero, and shadow runs are labeled", async ({
  page,
  request,
}) => {
  const recipe = await (await request.post(`${API}/__test/recipe?known_fees=false`)).json();
  await page.goto("/backtesting");
  const panel = page.getByRole("region", { name: "Event replay laboratory" });
  await panel.getByLabel("Event replay recipe JSON").fill(JSON.stringify(recipe));
  await panel.getByRole("button", { name: "Run replay" }).click();
  await expect(panel.locator("dt", { hasText: /^Net P&L$/ }).locator("xpath=..")).toContainText(
    "Unknown",
  );
  await expect(panel.locator("dt", { hasText: /^Fees$/ }).locator("xpath=..")).toContainText(
    "Unknown",
  );
  await expect(panel.getByLabel("Uncertainty")).toContainText("fees_unknown");
  await panel.getByRole("button", { name: "Run shadow" }).click();
  await expect(panel.getByRole("note")).toContainText("No order was sent or signed");
  await expect(panel.getByRole("button", { name: "Verify lab replay" })).toBeDisabled();
});
