import { test, expect } from "@playwright/test";

const conditionId = "0x" + "18".repeat(32);

test.beforeEach(async ({ page }) => {
  await page.route("**/api/**", (route) => {
    const path = new URL(route.request().url()).pathname;
    if (path.startsWith("/api/analysis/")) return route.continue();
    if (path === "/api/accounts") return route.fulfill({ json: [] });
    if (path === "/api/auth/me")
      return route.fulfill({
        json: {
          id: 1,
          wallet_address: "0x" + "11".repeat(20),
          is_admin: false,
        },
      });
    return route.fulfill({ json: {} });
  });
});

test("captured rules survive reload and later source revisions do not rewrite history", async ({
  page,
}, testInfo) => {
  await page.goto(`/analysis?condition_id=${conditionId}`);
  await expect(page.getByText("YES 70¢", { exact: true })).toBeVisible();
  const initial = await page.request.get(
    `http://127.0.0.1:8002/api/analysis/markets/${conditionId}/observations`,
  );
  const before = (await initial.json()).observations[0];
  await page.request.post(
    `http://127.0.0.1:8002/fixtures/revise-source?condition_id=${conditionId}`,
  );
  await page.reload();
  await expect(page.getByText("YES 80¢", { exact: true })).toBeVisible();
  await page.getByRole("button", { name: "Load observation history", exact: true }).click();
  const history = page.getByRole("list", { name: "Captured observations" });
  await expect(history).toContainText("YES 70¢");
  await expect(history).toContainText("YES 80¢");
  await history.locator("summary").first().click();
  await page.screenshot({ path: testInfo.outputPath("observation-history.png"), fullPage: true });
  const cutoff = await page.request.get(
    `http://127.0.0.1:8002/api/analysis/markets/${conditionId}/observations`,
    {
      params: { as_of: before.observed_at },
    },
  );
  const earlier = (await cutoff.json()).observations;
  expect(earlier.length).toBeGreaterThan(0);
  expect(
    earlier.every((row: { rules: { text: string } }) => !row.rules.text.includes("Revised:")),
  ).toBe(true);
  expect(earlier[0].snapshot_id).toBe(before.snapshot_id);
});

test("an earlier UTC cutoff shows no invented history", async ({ page }) => {
  await page.goto(`/analysis?condition_id=${"0x" + "19".repeat(32)}`);
  await page.getByLabel("Received by (UTC, optional)").fill("2000-01-01T00:00");
  await page.getByRole("button", { name: "Load observation history", exact: true }).click();
  await expect(page.getByText("No observations were captured by this time.")).toBeVisible();
});

test("history service failure is visible without losing current source context", async ({
  page,
}) => {
  await page.route("**/observations?*", (route) =>
    route.fulfill({ status: 503, json: { detail: "Storage unavailable" } }),
  );
  await page.goto(`/analysis?condition_id=${"0x" + "20".repeat(32)}`);
  await page.getByRole("button", { name: "Load observation history", exact: true }).click();
  await expect(page.getByRole("alert")).toContainText("Storage unavailable");
  await expect(page.getByText("YES 70¢", { exact: true })).toBeVisible();
});
