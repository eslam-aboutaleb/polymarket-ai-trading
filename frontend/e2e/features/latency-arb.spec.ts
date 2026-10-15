import { test, expect } from "../fixtures/base";

/**
 * Latency arbitrage: paper-first engine board and controls.
 */

test.describe("Latency arbitrage", () => {
  test.beforeEach(async ({ adminPage: page }) => {
    await page.goto("/latency-arb");
    await expect(page.getByRole("heading", { name: "Latency Arb" })).toBeVisible();
  });

  test("renders the edge board with engine state", async ({ adminPage: page }) => {
    await expect(page.getByText(/Live Edge Board \(\d+ opportunities\)/)).toBeVisible();
    await expect(page.getByText(/Engine stopped|Engine running|Engine/).first()).toBeVisible();
    await expect(page.getByText("PAPER", { exact: true })).toBeVisible();
    await expect(page.getByRole("button", { name: "Refresh" }).first()).toBeVisible();
  });

  test("explains the paper-first safety model", async ({ adminPage: page }) => {
    await expect(page.getByText(/simulated fills never touch the exchange/)).toBeVisible();
    await expect(
      page.getByText(/No latency-arb opportunities above the edge threshold right now/),
    ).toBeVisible();
  });
});
