import { test, expect } from "../fixtures/base";

/**
 * AI Orchestrator: intent picker and the multi-step analysis plan form.
 */

test.describe("AI Orchestrator", () => {
  test.beforeEach(async ({ adminPage: page }) => {
    await page.goto("/orchestrator");
    await expect(page.getByRole("heading", { name: "AI Orchestrator" })).toBeVisible();
  });

  test("renders the intent picker", async ({ adminPage: page }) => {
    await expect(page.getByText("Market Deep Dive")).toBeVisible();
    await expect(page.getByText("Opportunity Sweep")).toBeVisible();
    await expect(page.getByText("Trader Diligence")).toBeVisible();
    await expect(page.getByText(/1\. Pick an intent/)).toBeVisible();
  });

  test("selecting an intent reveals its step configuration", async ({ adminPage: page }) => {
    await page.getByText("Market Deep Dive").click();
    await expect(page.getByText(/2\./)).toBeVisible();
    // The run control is present for the selected intent.
    await expect(page.getByRole("button", { name: /Run|Analyze|Start/i }).first()).toBeVisible();
  });
});
