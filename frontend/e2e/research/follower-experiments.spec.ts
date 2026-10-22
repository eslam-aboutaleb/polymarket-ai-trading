import { test, expect } from "@playwright/test";
import path from "node:path";
import fs from "node:fs";
const example = path.resolve("public/examples/follower-experiment.json");

test.beforeEach(async ({ page }) => {
  await page.route("**/api/**", async (route) => {
    const pathname = new URL(route.request().url()).pathname;
    if (pathname.startsWith("/api/copy-trading/experiments")) return route.continue();
    if (pathname === "/api/auth/me")
      return route.fulfill({
        json: { id: 1, wallet_address: "0x" + "11".repeat(20), is_admin: false },
      });
    if (pathname === "/api/accounts") return route.fulfill({ json: [] });
    if (pathname === "/api/trades/copy-trades/pnl") return route.fulfill({json:{daily_pnl:0}});
    if (/following|copy-trades|evaluations/.test(pathname)) return route.fulfill({ json: [] });
    return route.fulfill({ json: {} });
  });
  await page.goto("/copy-trading");
  await expect(page.getByRole("heading", { name: "Simulate following a trader" })).toBeVisible();
});

test("real API replay shows delay loss, saves and reloads full journal", async ({
  page,
}, testInfo) => {
  const errors: string[] = [];
  page.on("pageerror", (e) => errors.push(e.message));
  await page.getByLabel("Import frozen experiment JSON").setInputFiles(example);
  await page.getByRole("button", { name: "Run and save simulation" }).click();
  const result = page.getByTestId("follower-result");
  await expect(result).toContainText("$7.50");
  await expect(result).toContainText("$-2.50");
  await expect(result).toContainText("unverified inputs");
  await result.getByText("0s: signal outcomes and accounting journal", { exact: true }).click();
  await expect(result).toContainText("entry: partial");
  await expect(result.getByRole("link", { name: "Download journal" })).toBeVisible();
  await page.screenshot({ path: testInfo.outputPath("follower-replay.png"), fullPage: true });
  await page.reload();
  await page.getByText("Saved experiments in this account", { exact: true }).click();
  await page.getByRole("button", { name: /2026-/ }).first().click();
  await expect(page.getByTestId("follower-result")).toContainText("$7.50");
  expect(errors).toEqual([]);
});

test("later follow start and missing fees do not inherit leader results", async ({ page }) => {
  await page.getByLabel("Import frozen experiment JSON").setInputFiles(example);
  await page.getByLabel("Follow start (ISO timestamp with timezone)").fill("2026-10-01T00:05:00Z");
  await page.getByRole("button", { name: "Run and save simulation" }).click();
  await expect(page.getByTestId("follower-result")).toContainText("$0.00");
  await expect(page.getByTestId("follower-result")).not.toContainText("$7.50");
  await page.getByLabel("Assumed fee (basis points; blank means unknown)").fill("");
  await page.getByRole("button", { name: "Run and save simulation" }).click();
  await page.getByText("0s: signal outcomes and accounting journal", { exact: true }).click();
  await expect(page.getByTestId("follower-result")).toContainText("missing_fee_policy");
});

test("open positions without fresh marks remain unknown", async ({ page }) => {
  const data = JSON.parse(fs.readFileSync(example, "utf8"));
  data.dataset.signals = data.dataset.signals.slice(0, 1);
  await page.getByLabel("Import frozen experiment JSON").setInputFiles({
    name: "open.json",
    mimeType: "application/json",
    buffer: Buffer.from(JSON.stringify(data)),
  });
  await page.getByRole("button", { name: "Run and save simulation" }).click();
  await expect(page.getByTestId("follower-result")).toContainText("Unknown");
  await page.getByText("0s: signal outcomes and accounting journal", { exact: true }).click();
  await expect(page.getByTestId("follower-result")).toContainText("Missing final marks: YES");
});

test("malformed imports and failed save cannot render completed metrics", async ({ page }) => {
  await page.getByLabel("Import frozen experiment JSON").setInputFiles({
    name: "bad.json",
    mimeType: "application/json",
    buffer: Buffer.from('{"wallet":null}'),
  });
  await expect(page.getByRole("alert")).toContainText("Expected a frozen experiment");
  await expect(page.getByTestId("follower-result")).toHaveCount(0);
  await page.getByLabel("Import frozen experiment JSON").setInputFiles(example);
  await page.getByLabel("Trader wallet").fill("invalid");
  await page.getByRole("button", { name: "Run and save simulation" }).click();
  await expect(page.getByRole("alert")).toBeVisible();
  await expect(page.getByTestId("follower-result")).toHaveCount(0);
});

test("account switch discards a pending file import", async ({ page }) => {
  await page.evaluate(() => {
    const original = File.prototype.text;
    File.prototype.text = async function () {
      const contents = await original.call(this);
      await new Promise<void>((resolve) => {
        (window as unknown as { releaseImport: () => void }).releaseImport = resolve;
      });
      return contents;
    };
  });
  await page.getByLabel("Import frozen experiment JSON").setInputFiles(example);
  await page.waitForFunction(
    () => typeof (window as unknown as { releaseImport?: unknown }).releaseImport === "function",
  );
  await page.evaluate(() => {
    window.dispatchEvent(
      new CustomEvent("polymarket:active-account-changed", { detail: { id: 2 } }),
    );
    (window as unknown as { releaseImport: () => void }).releaseImport();
  });
  await expect(page.getByLabel("Trader wallet")).toHaveCount(0);
  await expect(page.getByRole("button", { name: "Run and save simulation" })).toHaveCount(0);
});
