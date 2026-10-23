import { test, expect } from "@playwright/test";
import { installGuideFixtures } from "../../scripts/user-guide-fixtures";

// UI regressions with controlled API responses; no external integration claims.
test("forecast consensus finishes loading under React StrictMode", async ({ page }) => {
  const unknown = await installGuideFixtures(page);
  await page.goto("/leaderboard");
  await page.getByRole("button", { name: "Forecasting accuracy", exact: true }).click();
  await page.getByLabel("Market condition id to forecast on").fill("0x" + "ab".repeat(32));
  await expect(page.getByRole("heading", { name: "Consensus", exact: true })).toBeVisible();
  await expect(page.getByText("Loading consensus…", { exact: true })).toHaveCount(0);
  await expect(page.getByTestId("forecast-gap")).toBeVisible();
  expect([...unknown]).toEqual([]);
});

test("notification drawer covers the page with an opaque panel", async ({ page }) => {
  const unknown = await installGuideFixtures(page);
  await page.goto("/");
  await page.getByRole("button", { name: /^Notifications \(/ }).click();
  const dialog = page.getByRole("dialog", { name: "Notifications", exact: true });
  await expect(dialog.getByText("No notifications yet", { exact: true })).toBeVisible();
  await expect(page.getByText("No notifications yet", { exact: true })).toBeInViewport();
  expect(
    await page
      .getByRole("dialog", { name: "Notifications", exact: true })
      .evaluate((el) => el.getBoundingClientRect().height),
  ).toBeGreaterThanOrEqual(await page.evaluate(() => window.innerHeight - 1));
  await expect(dialog).not.toHaveCSS("background-color", "rgba(0, 0, 0, 0)");
  await dialog.getByRole("button", { name: "Close notifications" }).click();
  await expect(dialog).toHaveCount(0);
  expect([...unknown]).toEqual([]);
});
