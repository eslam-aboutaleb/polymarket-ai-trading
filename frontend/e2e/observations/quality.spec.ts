import { test, expect } from "@playwright/test";

test.beforeEach(async ({ page, request }) => {
  await request.post("http://127.0.0.1:8002/__test/reset");
  await page.route("**/api/**", (route) => {
    const path = new URL(route.request().url()).pathname;
    if (path.startsWith("/api/market-data/")) return route.continue();
    if (path === "/api/auth/me")
      return route.fulfill({
        json: { id: 1, wallet_address: "0x" + "11".repeat(20), is_admin: false },
      });
    if (path === "/api/accounts") return route.fulfill({ json: [] });
    if (path === "/api/backtesting/runs") return route.fulfill({ json: { runs: [] } });
    if (path === "/api/backtesting/strategies") return route.fulfill({ json: { strategies: [] } });
    return route.fulfill({ json: {} });
  });
  await page.goto("/backtesting");
});

test("quality shows source gaps, exact depth and immutable replay", async ({ page }, testInfo) => {
  const panel = page.getByRole("region", { name: "Market data quality" });
  await panel.getByLabel("Token ID", { exact: true }).fill("11");
  await panel.getByRole("button", { name: "Inspect observations" }).click();
  await expect(panel.getByText("Source: SOURCE_DISCONNECTED", { exact: false })).toBeVisible();
  await expect(panel.getByText("Quality: INSUFFICIENT_DATA", { exact: false })).toContainText(
    "SEQUENCE_GAP",
  );
  await expect(panel.getByRole("cell", { name: "0.400000000000000001", exact: true })).toHaveCount(
    2,
  );
  await expect(panel.getByRole("row")).toHaveCount(4); // header, two books, source status; future receipt excluded
  await panel.getByRole("button", { name: "Freeze these observations" }).click();
  await expect(panel.getByText("Frozen dataset:", { exact: false })).toBeVisible();
  const datasetId = await panel.getByLabel("Dataset ID", { exact: true }).inputValue();
  expect(datasetId).toMatch(/^[a-f0-9]{64}$/);
  await panel.getByRole("button", { name: "Replay frozen observations" }).click();
  await expect(panel.getByText("Frozen dataset:", { exact: false })).toContainText(datasetId);
  await expect(panel.getByRole("row")).toHaveCount(4);
  await page.screenshot({ path: testInfo.outputPath("market-data-quality.png"), fullPage: true });
});

test("missing evidence and disabled source do not appear as successful history", async ({
  page,
  request,
}) => {
  const panel = page.getByRole("region", { name: "Market data quality" });
  await panel.getByLabel("Token ID", { exact: true }).fill("99");
  await panel.getByRole("button", { name: "Inspect observations" }).click();
  await expect(panel.getByText("Stale or unavailable", { exact: false })).toBeVisible();
  await expect(panel.getByRole("button", { name: "Freeze these observations" })).toBeDisabled();
  await request.post("http://127.0.0.1:8002/__test/disable");
  await panel.getByRole("button", { name: "Inspect observations" }).click();
  await expect(panel.getByRole("alert")).toContainText("Observation archive disabled");
  await expect(panel.getByRole("table")).toHaveCount(0);
});

test("freeze pins the inspected IDs even if another receipt arrives before the cutoff", async ({
  page,
  request,
}) => {
  const panel = page.getByRole("region", { name: "Market data quality" });
  await panel.getByLabel("Token ID", { exact: true }).fill("11");
  await panel
    .getByLabel("As of (ISO timestamp with timezone)", { exact: true })
    .fill("2050-01-01T00:00:00+00:00");
  await panel.getByRole("button", { name: "Inspect observations" }).click();
  await expect(panel.getByRole("row")).toHaveCount(5);
  await request.post("http://127.0.0.1:8002/__test/add");
  await panel.getByRole("button", { name: "Freeze these observations" }).click();
  await expect(panel.getByText("Frozen dataset:", { exact: false })).toContainText(
    "4 observations",
  );
  await panel.getByRole("button", { name: "Replay frozen observations" }).click();
  await expect(panel.getByRole("row")).toHaveCount(5);
  await expect(panel.getByRole("cell", { name: "99", exact: true })).toHaveCount(0);
});
