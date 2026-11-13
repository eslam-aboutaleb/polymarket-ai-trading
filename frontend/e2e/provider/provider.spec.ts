import { test, expect } from "@playwright/test";

const backend = "http://127.0.0.1:18137";

test.beforeEach(async ({ page, request }) => {
  await request.post(`${backend}/__test/reset`, { data: { mode: "valid" } });
  await page.route("**/api/**", (route) => {
    const path = new URL(route.request().url()).pathname;
    if (path.startsWith("/api/market-data/")) return route.continue();
    if (path === "/api/auth/me")
      return route.fulfill({ json: { id: 1, wallet_address: "0x" + "11".repeat(20) } });
    if (path === "/api/accounts") return route.fulfill({ json: [] });
    if (path === "/api/backtesting/runs") return route.fulfill({ json: { runs: [] } });
    if (path === "/api/backtesting/strategies") return route.fulfill({ json: { strategies: [] } });
    return route.fulfill({ json: {} });
  });
  await page.goto("/backtesting");
});

test("complete hourly history retains source and immutable PostgreSQL evidence", async ({
  page,
  request,
}) => {
  const panel = page.getByRole("region", { name: "Historical feed provider" });
  await panel.getByRole("button", { name: "Inspect provider contract" }).click();
  await expect(panel.getByText("Provider: polymarket_clob_prices_history")).toBeVisible();
  await expect(panel.getByText(/research_indicative_only/)).toBeVisible();
  await panel.getByLabel("Provider token ID").fill("12345");
  await panel.getByRole("button", { name: "Check official history" }).click();
  await expect(panel.getByText("Readiness: READY", { exact: true })).toBeVisible();
  await expect(panel.getByText(/Coverage: 100.0%/)).toBeVisible();
  await expect(panel.getByText(/Response checksum: [a-f0-9]{64}/)).toBeVisible();
  await expect(panel.getByText(/Recorded observation: [a-f0-9]{64}/)).toBeVisible();
  const response = await request.get(`${backend}/api/market-data/observations?token_id=12345`);
  expect(response.ok()).toBeTruthy();
  const evidence = (await response.json()).observations;
  expect(evidence).toHaveLength(1);
  expect(evidence[0].raw.report.health_state).toBe("READY");
  expect(evidence[0].raw.raw_body_base64.length).toBeGreaterThan(20);
  expect(evidence[0].source).toBe("polymarket-native-rest");
});

for (const [mode, state, reason] of [
  ["gap", "DEGRADED", "STALE_OR_INSUFFICIENT_COVERAGE"],
  ["empty", "DEGRADED", "EMPTY_HISTORY"],
  ["malformed", "DEGRADED", "TRUNCATED_OR_INVALID_JSON"],
  ["401", "UNAUTHORIZED", "HTTP_401_UNAUTHORIZED"],
  ["403", "UNAUTHORIZED", "HTTP_403_UNAUTHORIZED"],
  ["429", "RATE_LIMITED", "HTTP_429_RATE_LIMITED"],
  ["503", "UNAVAILABLE", "HTTP_503_UNAVAILABLE"],
]) {
  test(`${mode} never appears as successful history`, async ({ page, request }) => {
    await request.post(`${backend}/__test/reset`, { data: { mode } });
    const panel = page.getByRole("region", { name: "Historical feed provider" });
    await panel.getByRole("button", { name: "Inspect provider contract" }).click();
    await panel.getByLabel("Provider token ID").fill("12345");
    await panel.getByRole("button", { name: "Check official history" }).click();
    await expect(panel.getByText(`Readiness: ${state}`, { exact: true })).toBeVisible();
    await expect(panel.getByText(`Reason: ${reason}`, { exact: true })).toBeVisible();
    await expect(panel.getByText("Readiness: READY", { exact: true })).toHaveCount(0);
    await expect(panel.getByText(/Recorded observation: [a-f0-9]{64}/)).toBeVisible();
  });
}

test("disabled canary and late disable clear prior successful output", async ({
  page,
  request,
}) => {
  const panel = page.getByRole("region", { name: "Historical feed provider" });
  await panel.getByRole("button", { name: "Inspect provider contract" }).click();
  await panel.getByLabel("Provider token ID").fill("12345");
  await panel.getByRole("button", { name: "Check official history" }).click();
  await expect(panel.getByText("Readiness: READY", { exact: true })).toBeVisible();
  await request.post(`${backend}/__test/reset`, { data: { mode: "disabled" } });
  await panel.getByRole("button", { name: "Check official history" }).click();
  await expect(panel.getByRole("alert")).toContainText("Historical feed canary disabled");
  await expect(panel.getByText("Readiness: READY", { exact: true })).toHaveCount(0);
  await panel.getByRole("button", { name: "Inspect provider contract" }).click();
  await expect(panel.getByText("Canary disabled", { exact: true })).toBeVisible();
  await expect(panel.getByRole("button", { name: "Check official history" })).toBeDisabled();
});
