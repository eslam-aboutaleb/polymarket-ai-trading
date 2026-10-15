import { test, expect } from "../fixtures/base";

/**
 * Analytics surfaces: execution analytics, forecast calibration and the
 * portfolio risk report.
 */

test.describe("Execution analytics", () => {
  test.beforeEach(async ({ adminPage: page }) => {
    await page.goto("/analytics/execution");
    await expect(page.getByRole("heading", { name: "Execution Analytics" })).toBeVisible();
  });

  test("renders the fill-quality metrics and window filters", async ({ adminPage: page }) => {
    // Metric labels are small paragraphs; the explainer paragraph mentions
    // some of the same words, so match the labels exactly.
    await expect(page.getByText("Fill rate", { exact: true })).toBeVisible();
    await expect(page.getByText("Avg slippage", { exact: true })).toBeVisible();
    await expect(page.getByText("Fee drag", { exact: true })).toBeVisible();
    await expect(page.getByText("Win rate", { exact: true })).toBeVisible();
    for (const window of ["7d", "30d", "90d"]) {
      await expect(page.getByRole("button", { name: window, exact: true })).toBeVisible();
    }
    await expect(page.getByText("Strategy attribution")).toBeVisible();
  });

  test("switching the window refreshes the metrics", async ({ adminPage: page }) => {
    await page.getByRole("button", { name: "90d", exact: true }).click();
    await expect(page.getByText("Fill rate", { exact: true })).toBeVisible();
  });
});

test.describe("Forecast calibration", () => {
  test.beforeEach(async ({ adminPage: page }) => {
    await page.goto("/analytics/calibration");
    await expect(page.getByRole("heading", { name: "Forecast Calibration" })).toBeVisible();
  });

  test("renders Brier-score panels and window filters", async ({ adminPage: page }) => {
    await expect(page.getByText("Forecasts", { exact: true })).toBeVisible();
    await expect(page.getByText("Mean Brier", { exact: true })).toBeVisible();
    await expect(page.getByText("Brier skill", { exact: true })).toBeVisible();
    await expect(page.getByText(/coin-flip baseline 0\.25/)).toBeVisible();
    await expect(page.getByText("Reliability curve")).toBeVisible();
  });
});

test.describe("Portfolio risk report", () => {
  test("renders the empty state explanation", async ({ adminPage: page }) => {
    await page.goto("/analytics/risk");
    await expect(page.getByRole("heading", { name: "Portfolio Risk Report" })).toBeVisible();
    await expect(page.getByText(/No open positions/)).toBeVisible();
    await expect(page.getByRole("button", { name: "Refresh" })).toBeVisible();
  });
});
