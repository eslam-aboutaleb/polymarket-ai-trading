import { test, expect } from "@playwright/test";
import { installGuideFixtures } from "../../scripts/user-guide-fixtures";
const backend = "http://127.0.0.1:8002";

test.beforeEach(async ({ page, request }) => {
  expect((await request.post(`${backend}/__test/maker/reset`)).ok()).toBeTruthy();
  await installGuideFixtures(page);
  await page.route("**/api/market-maker/**", (route) => route.continue());
  await page.goto("/market-making");
});

test("100 stationary syncs remain resting; attributed partial fill, retry and cancel", async ({
  page,
  request,
}, testInfo) => {
  const inspect = page.getByRole("button", { name: "Inspect paper quote intents" });
  await inspect.click();
  await expect(page.getByText("RESTING", { exact: true })).toBeVisible();
  await expect(page.getByText(/reserved cash \$4.04/)).toBeVisible();
  const first = await request.get(`${backend}/api/market-maker/configs/1/paper-intents`);
  const id = (await first.json()).intents[0].id;
  const filled = await request.post(`${backend}/__test/maker/fill`);
  const balance = await filled.json();
  expect(Number(balance.cash)).toBe(997.98);
  expect(Number(balance.shares)).toBe(5);
  await request.post(`${backend}/__test/maker/fill`);
  await inspect.click();
  await expect(page.getByText("PARTIAL", { exact: true })).toBeVisible();
  await expect(page.getByText(/reserved cash \$2.02/)).toBeVisible();
  await expect(page.getByText(/modeled_low_confidence/)).toBeVisible();
  await page.screenshot({ path: testInfo.outputPath("maker-lifecycle.png"), fullPage: true });
  const stopped = await request.post(`${backend}/api/market-maker/configs/1/stop`);
  expect(stopped.ok()).toBeTruthy();
  await inspect.click();
  await expect(page.getByText("CANCELLED", { exact: true })).toBeVisible();
  const final = await request.get(`${backend}/api/market-maker/configs/1/paper-intents`);
  expect((await final.json()).intents[0].id).toBe(id);
});

test("foreign account header cannot read lifecycle", async ({ request }) => {
  const response = await request.get(`${backend}/api/market-maker/configs/1/paper-intents`, {
    headers: { "X-Account-Id": "999" },
  });
  expect([403, 404]).toContain(response.status());
});
