import { test, expect } from "@playwright/test";

const wallet = "0x" + "a".repeat(40);

test.beforeEach(async ({ page }) => {
  await page.route("**/api/**", (route) => {
    const pathname = new URL(route.request().url()).pathname;
    if (pathname.startsWith("/api/replay/") || pathname.startsWith("/api/copy-trading/experiments"))
      return route.continue();
    if (pathname === "/api/auth/me")
      return route.fulfill({ json: { id: 1, wallet_address: wallet, is_admin: false } });
    if (pathname === "/api/accounts") return route.fulfill({ json: [] });
    if (pathname === "/api/trades/copy-trades/pnl")
      return route.fulfill({ json: { daily_pnl: 0 } });
    if (/following|copy-trades|evaluations/.test(pathname)) return route.fulfill({ json: [] });
    return route.fulfill({ json: {} });
  });
  await page.goto("/copy-trading");
  await page.getByRole("button", { name: "Use native captured data" }).click();
  await page.getByLabel("Trader wallet", { exact: true }).fill(wallet);
  await page.getByLabel("Assumed fee (basis points; blank means unknown)").fill("0");
});

test("native HTTP capture saves a frozen comparison, provenance and journal across reload", async ({
  page,
}, testInfo) => {
  const errors: string[] = [];
  page.on("pageerror", (error) => errors.push(error.message));
  const captured = page.waitForResponse(
    (r) => r.url().endsWith("/api/replay/capture") && r.request().method() === "POST",
  );
  await page.getByRole("button", { name: "Capture trader and native books now" }).click();
  expect((await captured).status()).toBe(200);
  const saved = page.waitForResponse(
    (r) => r.url().endsWith("/experiments/from-capture") && r.request().method() === "POST",
  );
  await page.getByRole("button", { name: "Run and save simulation" }).click();
  const response = await saved;
  expect(response.status()).toBe(200);
  const run = await response.json();
  const result = page.getByTestId("follower-result");
  await expect(result).toContainText("native captures, modeled fills");
  await expect(result).toContainText("$-0.25");
  await result.getByText("0s: signal outcomes and accounting journal", { exact: true }).click();
  await expect(result).toContainText("modeled_depth_fill");
  await expect(result.getByRole("link", { name: "Download journal" })).toBeVisible();
  expect(run.result.execution_eligible).toBe(false);
  await result.getByText("Captured source manifest", { exact: true }).click();
  await expect(result.getByRole("link", { name: "Download source manifest" })).toBeVisible();
  expect(run.result.capture_manifest_hash).toMatch(/^[0-9a-f]{64}$/);
  expect(run.result.capture_ids.length).toBeGreaterThanOrEqual(4);
  const source = await page.request.get(
    "http://127.0.0.1:8002/api/replay/captures/" + run.result.capture_ids[0],
  );
  expect(source.status()).toBe(200);
  expect((await source.json()).content_hash).toMatch(/^[0-9a-f]{64}$/);
  await page.screenshot({
    path: testInfo.outputPath("native-follower-capture.png"),
    fullPage: true,
  });
  await page.reload();
  await page.getByText("Saved experiments in this account", { exact: true }).click();
  await page.getByRole("button", { name: new RegExp(run.id) }).click();
  await expect(page.getByTestId("follower-result")).toContainText("$-0.25");
  expect(errors).toEqual([]);
});

test("missing native evidence and capture outages do not show completed metrics", async ({
  page,
}) => {
  await page.getByLabel("Follow start (ISO timestamp with timezone)").fill("2000-01-01T00:00:00Z");
  await page.getByLabel("End (ISO timestamp with timezone)").fill("2000-01-02T00:00:00Z");
  await page.getByRole("button", { name: "Run and save simulation" }).click();
  await expect(page.getByRole("alert")).toContainText("No signals captured");
  await expect(page.getByTestId("follower-result")).toHaveCount(0);
  await page.route("**/api/replay/capture", (route) =>
    route.fulfill({ status: 503, json: { detail: "Native capture unavailable" } }),
  );
  await page.getByRole("button", { name: "Capture trader and native books now" }).click();
  await expect(page.getByRole("alert")).toContainText("Native capture unavailable");
  await expect(page.getByTestId("follower-result")).toHaveCount(0);
});
