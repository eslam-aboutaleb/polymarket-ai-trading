import { test, expect } from "../fixtures/base";

/**
 * Cross-venue signals (Kalshi / Manifold / PredictIt comparison rows).
 */

test.describe("Cross-venue signals", () => {
  test.beforeEach(async ({ adminPage: page }) => {
    await page.goto("/venues");
    await expect(page.getByRole("heading", { name: "Cross-Venue Signals" })).toBeVisible();
  });

  test("renders the venue comparison board and the signal-only notice", async ({
    adminPage: page,
  }) => {
    await expect(page.getByText(/Signal-only\. These rows are comparison data/)).toBeVisible();
    for (const venue of ["Kalshi", "Manifold", "PredictIt"]) {
      await expect(page.getByText(venue).first()).toBeVisible();
    }
  });

  test("explains that adapters are read-only and off by default", async ({ adminPage: page }) => {
    await expect(page.getByText(/off by default and stay read-only/)).toBeVisible();
  });
});
