import { test, expect } from "@playwright/test";
import { installGuideFixtures } from "../../scripts/user-guide-fixtures";
const backend = "http://127.0.0.1:8002";

test.beforeEach(async ({ page, request }) => {
  expect((await request.post(`${backend}/__test/reset`)).ok()).toBeTruthy();
  await installGuideFixtures(page);
  // The surrounding dashboard data is controlled; accounting and chart APIs
  // always cross the real HTTP router, SQL transaction, and journal boundary.
  await page.route("**/api/**", (route) => {
    const path = new URL(route.request().url()).pathname;
    if (
      path.startsWith("/api/paper/") ||
      path.startsWith("/api/portfolio/equity/") ||
      path.startsWith("/api/accounts") ||
      path === "/api/settings/paper-summary"
    )
      return route.continue();
    return route.fallback();
  });
});

test("paper NAV changes from native marks without another trade and gaps stay unknown", async ({
  page,
  request,
}) => {
  await page.goto("/");
  const panel = page.getByRole("region", { name: "Paper account controls" });
  await panel.getByLabel("Fee assumption (bps)").fill("0");
  await panel.getByRole("button", { name: "Save paper assumptions" }).click();
  await expect(panel.getByText(/Cash: \$1000/)).toBeVisible();
  const order = {
    token_id: "11",
    side: "BUY",
    amount: "25",
    units: "SHARES",
    intent_key: "browser-golden",
  };
  const fill = await request.post(`${backend}/api/paper/orders`, { data: order });
  expect(fill.ok()).toBeTruthy();
  const original = await fill.json();
  expect(await (await request.post(`${backend}/api/paper/orders`, { data: order })).json()).toEqual(
    original,
  );
  await Promise.all([
    page.waitForResponse(
      (response) =>
        response.url().endsWith("/api/paper/snapshot") && response.request().method() === "POST",
    ),
    panel.getByRole("button", { name: "Record hourly paper NAV" }).click(),
  ]);
  await request.post(`${backend}/__test/advance`, { data: { bid: "0.60" } });
  await Promise.all([
    page.waitForResponse(
      (response) =>
        response.url().endsWith("/api/paper/snapshot") && response.request().method() === "POST",
    ),
    panel.getByRole("button", { name: "Record hourly paper NAV" }).click(),
  ]);
  await expect(page.getByText("$1,005.00", { exact: true })).toBeVisible();
  const history = await (
    await request.get(`${backend}/api/portfolio/equity/snapshots?is_paper=true&from=2026-01-01`)
  ).json();
  expect(history.map((point: any) => point.bankroll)).toEqual([999.75, 1005]);
  const journal = await (await request.get(`${backend}/api/paper/journal`)).json();
  expect(journal.filter((event: any) => event.method === "fill")).toHaveLength(1);
  await request.post(`${backend}/__test/advance`, { data: {} });
  await Promise.all([
    page.waitForResponse(
      (response) =>
        response.url().endsWith("/api/paper/snapshot") && response.request().method() === "POST",
    ),
    panel.getByRole("button", { name: "Record hourly paper NAV" }).click(),
  ]);
  await expect(page.getByText("Unknown", { exact: true }).first()).toBeVisible();
  const stale = await (
    await request.get(`${backend}/api/portfolio/equity/snapshots?is_paper=true&from=2026-01-01`)
  ).json();
  expect(stale.at(-1).bankroll).toBeNull();
  await expect(page.locator(".recharts-line-curve")).toBeVisible();
});

test("account switches clear old NAV and foreign account headers are rejected", async ({
  page,
  request,
}) => {
  await request.post(`${backend}/api/paper/account`, { data: { fee_bps: "0" } });
  await request.post(`${backend}/api/paper/cash-flows`, {
    data: { event_id: "fund", amount: "100" },
  });
  await request.post(`${backend}/api/paper/snapshot`);
  await request.post(`${backend}/api/paper/account`, {
    headers: { "X-Account-Id": "2" },
    data: { fee_bps: "0" },
  });
  await request.post(`${backend}/api/paper/snapshot`, { headers: { "X-Account-Id": "2" } });
  await page.goto("/");
  await expect(page.getByText("$1,100.00", { exact: true })).toBeVisible();
  await page.getByLabel("Active trading account").selectOption("2");
  await expect(page.getByText("$1,100.00", { exact: true })).toHaveCount(0);
  await expect(page.getByText("$1,000.00", { exact: true }).first()).toBeVisible();
  expect(
    (
      await request.get(`${backend}/api/paper/account`, { headers: { "X-Account-Id": "3" } })
    ).status(),
  ).toBe(403);
  expect(
    (
      await request.get(`${backend}/api/portfolio/equity/snapshots?is_paper=true`, {
        headers: { "X-Account-Id": "3" },
      })
    ).status(),
  ).toBe(403);
});

test("partial close and top-up reconcile profit separately from funding", async ({
  page,
  request,
}) => {
  await request.post(`${backend}/api/paper/account`, { data: { fee_bps: "0" } });
  await request.post(`${backend}/api/paper/orders`, {
    data: { token_id: "11", side: "BUY", amount: "10", units: "USDC", intent_key: "cash-entry" },
  });
  await request.post(`${backend}/__test/advance`, { data: { seconds: 1, bid: "0.60" } });
  await request.post(`${backend}/api/paper/snapshot`);
  await request.post(`${backend}/__test/advance`, {
    data: { seconds: 3600, bid: "0.70", ask: "0.71" },
  });
  const sold = await (
    await request.post(`${backend}/api/paper/orders`, {
      data: {
        token_id: "11",
        side: "SELL",
        amount: "10",
        units: "SHARES",
        intent_key: "partial-exit",
      },
    })
  ).json();
  expect(Number(sold.realized_pnl)).toBe(3);
  await request.post(`${backend}/api/paper/cash-flows`, {
    data: { event_id: "topup", amount: "100" },
  });
  const value = await (await request.post(`${backend}/api/paper/snapshot`)).json();
  expect(Number(value.nav)).toBe(1107.5);
  expect(Number(value.positions["11"].shares)).toBe(15);
  await page.goto("/");
  await expect(page.getByText("$1,107.50", { exact: true })).toBeVisible();
  const summary = await (
    await request.get(`${backend}/api/portfolio/equity/summary?is_paper=true`)
  ).json();
  expect(summary.net_cash_flows).toBe(100);
  expect(summary.delta).toBe(2.5);
  expect(summary.delta_pct).toBeNull();
});

test("a delayed response from the previous account cannot replace the new curve", async ({
  page,
  request,
}) => {
  await request.post(`${backend}/api/paper/account`, { data: { fee_bps: "0" } });
  await request.post(`${backend}/api/paper/cash-flows`, {
    data: { event_id: "fund", amount: "100" },
  });
  await request.post(`${backend}/api/paper/snapshot`);
  await request.post(`${backend}/api/paper/account`, {
    headers: { "X-Account-Id": "2" },
    data: { fee_bps: "0" },
  });
  await request.post(`${backend}/api/paper/snapshot`, { headers: { "X-Account-Id": "2" } });
  let release!: () => void;
  let started!: () => void;
  const held = new Promise<void>((resolve) => {
    release = resolve;
  });
  const ready = new Promise<void>((resolve) => {
    started = resolve;
  });
  await page.route("**/api/portfolio/equity/snapshots?**", async (route) => {
    if (
      !route.request().headers()["x-account-id"] ||
      route.request().headers()["x-account-id"] === "1"
    ) {
      const response = await route.fetch();
      started();
      await held;
      await route.fulfill({ response });
    } else await route.continue();
  });
  await page.goto("/");
  await ready;
  await page.getByLabel("Active trading account").selectOption("2");
  await expect(page.getByText("$1,000.00", { exact: true }).first()).toBeVisible();
  const delivered = page.waitForResponse(
    (response) =>
      response.url().includes("/api/portfolio/equity/snapshots") &&
      (!response.request().headers()["x-account-id"] ||
        response.request().headers()["x-account-id"] === "1"),
  );
  release();
  await (await delivered).finished();
  await page.evaluate(
    () =>
      new Promise<void>((resolve) =>
        requestAnimationFrame(() => requestAnimationFrame(() => resolve())),
      ),
  );
  await expect(page.getByText("$1,100.00", { exact: true })).toHaveCount(0);
  await expect(page.getByText("$1,000.00", { exact: true }).first()).toBeVisible();
});
