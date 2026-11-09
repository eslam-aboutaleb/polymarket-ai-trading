import { expect, test, Page } from "@playwright/test";

async function openHistory(page: Page) {
  await page.route("**/api/**", (route) => {
    const path = new URL(route.request().url()).pathname;
    if (path.startsWith("/api/latency-arb/") || path === "/api/accounts") return route.continue();
    if (path === "/api/auth/me")
      return route.fulfill({
        json: { id: 1, wallet_address: "0x" + "11".repeat(20), is_admin: true },
      });
    return route.fulfill({ json: {} });
  });
  await page.goto("/latency-arb");
  return page.getByRole("region", { name: "Paper execution evidence" });
}

for (const scenario of ["missing", "stale", "future", "crossed", "resolved"]) {
  test(`${scenario}: no paper fill or realized returns; history survives refresh`, async ({
    page,
    request,
  }) => {
    const response = await request.post(`http://127.0.0.1:8002/__test/latency-paper/${scenario}`);
    expect(response.ok()).toBeTruthy();
    const state = await response.json();
    expect(state).toMatchObject({
      trades: 0,
      intents: 1,
      orders: 1,
      cash: "1000.00000000",
      realized_pnl: "0",
    });
    expect(state.result.execution_state).toBe("UNVERIFIABLE_DATA");
    const panel = await openHistory(page);
    await expect(panel.getByRole("cell", { name: "Unverifiable", exact: true })).toBeVisible();
    await expect(panel.getByRole("status")).toContainText("not fill-verifiable");
    await expect(panel.getByRole("cell", { name: "Filled", exact: true })).toHaveCount(0);
    await page.reload();
    await expect(panel.getByText(state.result.reason, { exact: true })).toBeVisible();
  });
}

test("native depth is estimated with bounded cash and exactly-once recovery", async ({
  page,
  request,
}) => {
  const first = await (
    await request.post("http://127.0.0.1:8002/__test/latency-paper/full")
  ).json();
  expect(first).toMatchObject({ trades: 1, intents: 1, orders: 1 });
  expect(Number(first.cash)).toBe(990);
  expect(first.result).toMatchObject({ execution_state: "ESTIMATED", exchange_confirmed: false });
  const panel = await openHistory(page);
  await expect(panel.getByRole("cell", { name: "Estimated", exact: true })).toBeVisible();
  await expect(panel).toContainText("never exchange-confirmed");
  const retry = await (
    await request.post("http://127.0.0.1:8002/__test/latency-paper-retry")
  ).json();
  expect(retry).toEqual(first);
  await page.reload();
  await expect(panel.getByRole("cell", { name: "Estimated", exact: true })).toBeVisible();
  const foreign = await request.get("http://127.0.0.1:8002/api/latency-arb/paper-intents", {
    headers: { "X-Account-ID": "3" },
  });
  expect(foreign.status()).toBe(403);
  const other = await request.get("http://127.0.0.1:8002/api/latency-arb/paper-intents", {
    headers: { "X-Account-ID": "2" },
  });
  expect((await other.json()).intents).toEqual([]);
});

for (const [scenario, label] of [
  ["partial", "Partial estimate"],
  ["fees", "Unfilled"],
]) {
  test(`${scenario}: partial capacity and fees are visible`, async ({ page, request }) => {
    const response = await request.post(`http://127.0.0.1:8002/__test/latency-paper/${scenario}`);
    expect(response.ok()).toBeTruthy();
    const result = await response.json();
    expect(
      Number(result.result.notional_usdc) + Number(result.result.fee_usdc),
    ).toBeLessThanOrEqual(10);
    const panel = await openHistory(page);
    await expect(panel.getByRole("cell", { name: label, exact: true })).toBeVisible();
  });
}
