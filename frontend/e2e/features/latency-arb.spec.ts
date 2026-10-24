import { test, expect } from "../fixtures/base";

/**
 * Latency arbitrage: engine mode, edge board and simulation controls.
 */

test.describe("Latency arbitrage", () => {
  test.beforeEach(async ({ adminPage: page }) => {
    await page.goto("/latency-arb");
    await expect(page.getByRole("heading", { name: "Latency Arb" })).toBeVisible();
  });

  test("renders the edge board with engine state", async ({ adminPage: page }) => {
    const response = await page.request.get("/api/latency-arb/opportunities");
    expect(response.ok()).toBeTruthy();
    const { engine } = await response.json();
    expect(typeof engine.live_mode).toBe("boolean");
    await expect(page.getByText(/Live Edge Board \(\d+ opportunities\)/)).toBeVisible();
    await expect(page.getByText(/Engine stopped|Engine running|Engine/).first()).toBeVisible();
    const mode = engine.live_mode ? "LIVE" : "PAPER";
    const board = page.getByRole("main");
    await expect(board.getByText(mode, { exact: true })).toBeVisible();
    await expect(board.getByText(engine.live_mode ? "PAPER" : "LIVE", { exact: true })).toHaveCount(
      0,
    );
    await expect(page.getByRole("button", { name: "Refresh" }).first()).toBeVisible();
  });

  test("explains the paper-first safety model", async ({ adminPage: page }) => {
    await expect(page.getByText(/simulated fills never touch the exchange/)).toBeVisible();
    await expect(
      page.getByText(/No latency-arb opportunities above the edge threshold right now/),
    ).toBeVisible();
  });
});
