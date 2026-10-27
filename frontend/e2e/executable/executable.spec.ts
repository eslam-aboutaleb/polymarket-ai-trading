import { test, expect } from "@playwright/test";

test("native captured depth freeze, modeled ledger, immutable repeat and account boundary", async ({
  page,
  request,
}, testInfo) => {
  const recipe = await (await request.post("http://127.0.0.1:8002/__test/recipe")).json();
  await page.route("**/api/**", (route) => {
    const path = new URL(route.request().url()).pathname;
    if (path.startsWith("/api/backtesting/executable/")) return route.continue();
    if (path === "/api/auth/me")
      return route.fulfill({
        json: { id: 1, wallet_address: "0x" + "11".repeat(20), is_admin: false },
      });
    if (path === "/api/accounts") return route.fulfill({ json: [] });
    if (path === "/api/backtesting/runs") return route.fulfill({ json: { runs: [] } });
    if (path === "/api/backtesting/strategies") return route.fulfill({ json: { strategies: [] } });
    return route.fulfill({ json: {} });
  });
  await page.goto("/backtesting");
  const panel = page.getByRole("region", { name: "Executable replay" });
  await panel.getByLabel("Replay recipe JSON").fill(JSON.stringify(recipe));
  await panel.getByRole("button", { name: "Freeze recipe", exact: true }).click();
  await expect(panel.getByText("Book evidence: native_captured.", { exact: false })).toBeVisible();
  const identity = await panel.getByLabel("Frozen replay ID").inputValue();
  await panel.getByRole("button", { name: "Evaluate frozen recipe" }).click();
  await expect(panel.getByText("native_captured_counterfactual.", { exact: false })).toBeVisible();
  await expect(panel.locator("dd").first()).toHaveText("0.50");
  await expect(panel.getByRole("button", { name: "Evaluate frozen recipe" })).toBeDisabled();
  await panel.getByRole("button", { name: "Verify replay" }).click();
  await expect(panel.getByRole("status")).toContainText("identical result");
  await panel.getByText("Full report, assumptions and ledger").click();
  await expect(panel.locator("pre")).toContainText('"fee_usdc"');
  await expect(panel.locator("pre")).toContainText('"execution_eligible": false');
  await panel.screenshot({ path: testInfo.outputPath("executable-replay.png") });
  const original = await (
    await request.get(`http://127.0.0.1:8002/api/backtesting/executable/runs/${identity}`)
  ).json();
  const repeated = await (
    await request.post(`http://127.0.0.1:8002/api/backtesting/executable/runs/${identity}/execute`)
  ).json();
  expect(repeated.result_hash).toBe(original.result_hash);
  expect(original.result.holdout.ledger.length).toBeGreaterThan(0);
  expect(original.result.holdout.statistics.sharpe_ratio).toBeNull();
  const foreign = await request.get(
    `http://127.0.0.1:8002/api/backtesting/executable/runs/${identity}`,
    { headers: { "X-Account-Id": "2" } },
  );
  expect(foreign.status()).toBe(403);
});
