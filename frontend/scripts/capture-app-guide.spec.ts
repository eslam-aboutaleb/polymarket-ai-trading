import { test, expect } from "@playwright/test";
import path from "node:path";
import fs from "node:fs";
import { installGuideFixtures } from "./user-guide-fixtures";

const screens = [
  ["dashboard", "/", "Dashboard"],
  ["markets", "/markets", "Markets"],
  ["leaderboard", "/leaderboard", "Leaderboard"],
  ["trades", "/trades", "Trades"],
  ["backtesting", "/backtesting", "Backtesting"],
  ["news", "/news", "News"],
  ["whales", "/whales", "Whales"],
  ["latency-arb", "/latency-arb", "Latency Arb"],
  ["execution", "/analytics/execution", "Execution Analytics"],
  ["calibration", "/analytics/calibration", "Forecast Calibration"],
  ["risk", "/analytics/risk", "Portfolio Risk Report"],
  ["venues", "/venues", "Cross-Venue Signals"],
  ["orchestrator", "/orchestrator", "AI Orchestrator"],
  ["ask-ai", "/command-palette", ""],
  ["ops", "/ops", "Ops Center"],
  ["debug", "/debug", ""],
  ["in-app-guide", "/guide", "📖 Your Guide"],
  ["login", "/login", ""],
] as const;

for (const [name, url, heading] of screens) {
  test(`capture ${name}`, async ({ page }) => {
    const errors: string[] = [];
    page.on("pageerror", (error) => errors.push(error.message));
    const unknown = await installGuideFixtures(page, name === "login");
    await page.goto(url);
    if (heading)
      await expect(page.getByRole("heading", { name: heading, exact: true })).toBeVisible();
    await expect(page.getByText("Something went wrong", { exact: true })).toHaveCount(0);
    if (name === "ask-ai")
      await page.getByRole("button", { name: "Open palette", exact: true }).click();
    await page.waitForTimeout(800); // Let charts/fonts settle for documentation captures.
    expect(errors).toEqual([]);
    expect([...unknown], `Unmapped endpoints on ${name}`).toEqual([]);
    await page.screenshot({
      path: path.resolve(`../.github/images/user-guide/app-${name}.png`),
      animations: "disabled",
    });
  });
}

for (const tab of ["Profile", "Trading", "Notifications", "Accounts", "Admin Settings"]) {
  test(`capture settings ${tab}`, async ({ page }) => {
    const errors: string[] = [];
    page.on("pageerror", (error) => errors.push(error.message));
    const unknown = await installGuideFixtures(page);
    await page.goto("/settings");
    await expect(page.getByRole("heading", { name: "Settings", exact: true })).toBeVisible();
    if (tab !== "Profile") await page.getByRole("button", { name: tab, exact: true }).click();
    await page.waitForTimeout(700);
    expect(errors).toEqual([]);
    expect([...unknown], `Unmapped endpoints on ${tab}`).toEqual([]);
    await page.screenshot({
      path: path.resolve(
        `../.github/images/user-guide/app-settings-${tab.toLowerCase().replace(/ /g, "-")}.png`,
      ),
      animations: "disabled",
      fullPage: true,
    });
  });
}

test("capture position management and exit review dialogs", async ({ page }) => {
  const errors: string[] = [];
  page.on("pageerror", (error) => errors.push(error.message));
  const unknown = await installGuideFixtures(page);
  await page.goto("/trades");
  await page.getByRole("button", { name: "Your Positions", exact: true }).click();
  await expect(page.getByRole("button", { name: "Stop Loss", exact: true })).toBeVisible();
  await page.screenshot({
    path: path.resolve("../.github/images/user-guide/app-positions.png"),
    animations: "disabled",
  });
  for (const [label, image] of [
    ["Stop Loss", "stop-loss"],
    ["Take Profit", "take-profit"],
    ["Cash Out", "cash-out"],
  ]) {
    await page.getByRole("button", { name: label, exact: true }).click();
    const dialog = page.getByRole("dialog").last();
    await expect(dialog).toBeVisible();
    await page.screenshot({
      path: path.resolve(`../.github/images/user-guide/app-${image}.png`),
      animations: "disabled",
    });
    await page.keyboard.press("Escape");
  }
  expect(errors).toEqual([]);
  expect([...unknown]).toEqual([]);
});

for (const [url, name, button, heading] of [
  ["/markets", "trade-ticket", "", ""],
  ["/backtesting", "backtest-form", "+ New Backtest", "New Backtest"],
  ["/market-making", "maker-form", "+ New Config", "New Market Maker Config"],
] as const) {
  test(`capture ${name}`, async ({ page }) => {
    const errors: string[] = [];
    page.on("pageerror", (error) => errors.push(error.message));
    const unknown = await installGuideFixtures(page);
    await page.goto(url);
    if (name === "trade-ticket") {
      await page.getByRole("button", { name: /^Yes/ }).first().click();
      await expect(page.getByRole("button", { name: "Close trade ticket" })).toBeVisible();
    } else {
      await page.getByRole("button", { name: button, exact: true }).click();
      await expect(page.getByRole("heading", { name: heading, exact: true })).toBeVisible();
    }
    await page.waitForTimeout(400);
    expect(errors).toEqual([]);
    expect([...unknown]).toEqual([]);
    await page.screenshot({
      path: path.resolve(`../.github/images/user-guide/app-${name}.png`),
      animations: "disabled",
    });
  });
}

test("guide capture inventory covers every application page", () => {
  const app = fs.readFileSync(path.resolve("src/App.tsx"), "utf8");
  const routes = [...app.matchAll(/path="([^"]+)"/g)]
    .map((match) => match[1])
    .filter((route) => route !== "*")
    .map((route) => "/" + route.replace(/^\//, ""));
  const covered = new Set<string>([
    ...screens.map((screen) => screen[1]),
    "/settings",
    "/analysis",
    "/opportunities",
    "/copy-trading",
    "/market-making",
  ]);
  expect(routes.filter((route) => !covered.has(route))).toEqual([]);
});

test("capture forecasting collaboration", async ({ page }) => {
  const errors: string[] = [];
  page.on("pageerror", (error) => errors.push(error.message));
  const unknown = await installGuideFixtures(page);
  await page.goto("/leaderboard");
  await page.getByRole("button", { name: "Forecasting accuracy", exact: true }).click();
  await page.getByLabel("Market condition id to forecast on").fill("0x" + "ab".repeat(32));
  await expect(page.getByRole("heading", { name: "Consensus", exact: true })).toBeVisible();
  await expect(page.getByText("Loading consensus…", { exact: true })).toHaveCount(0);
  await page.waitForTimeout(800);
  expect(errors).toEqual([]);
  expect([...unknown]).toEqual([]);
  await page.screenshot({
    path: path.resolve("../.github/images/user-guide/app-forecasting.png"),
    fullPage: true,
    animations: "disabled",
  });
});

test("capture notification inbox", async ({ page }) => {
  const errors: string[] = [];
  page.on("pageerror", (error) => errors.push(error.message));
  const unknown = await installGuideFixtures(page);
  await page.goto("/");
  await page.getByRole("button", { name: /^Notifications \(/ }).click();
  await expect(page.getByRole("button", { name: "Close notifications" })).toBeVisible();
  await expect(page.getByText("No notifications yet", { exact: true })).toBeVisible();
  await expect(page.getByText("No notifications yet", { exact: true })).toBeInViewport();
  expect(
    await page
      .getByRole("dialog", { name: "Notifications", exact: true })
      .evaluate((el) => el.getBoundingClientRect().height),
  ).toBeGreaterThanOrEqual(await page.evaluate(() => window.innerHeight - 1));
  await expect(page.getByRole("dialog", { name: "Notifications", exact: true })).not.toHaveCSS(
    "background-color",
    "rgba(0, 0, 0, 0)",
  );
  await page.waitForTimeout(400);
  expect(errors).toEqual([]);
  expect([...unknown]).toEqual([]);
  await page.screenshot({
    path: path.resolve("../.github/images/user-guide/app-notification-inbox.png"),
    animations: "disabled",
  });
});

test("capture account risk and simulation settings", async ({ page }) => {
  const errors: string[] = [];
  page.on("pageerror", (error) => errors.push(error.message));
  const unknown = await installGuideFixtures(page);
  await page.goto("/settings");
  await page.getByRole("button", { name: "Accounts", exact: true }).click();
  await page.getByRole("listitem").getByRole("button", { name: "Settings", exact: true }).click();
  await expect(page.getByRole("heading", { name: /^Settings —/ })).toBeVisible();
  await expect(page.getByText("Kelly fraction (0-1)", { exact: true })).toBeVisible();
  await page.waitForTimeout(400);
  expect(errors).toEqual([]);
  expect([...unknown]).toEqual([]);
  await page.getByRole("heading", { name: /^Settings —/ }).scrollIntoViewIfNeeded();
  await page.screenshot({
    path: path.resolve("../.github/images/user-guide/app-account-risk-settings.png"),
    fullPage: true,
    animations: "disabled",
  });
});
