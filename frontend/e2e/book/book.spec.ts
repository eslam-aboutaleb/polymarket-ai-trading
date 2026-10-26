import { test, expect } from "@playwright/test";
const condition = "0x" + "ab".repeat(32);

test.beforeEach(async ({ page, request }) => {
  await request.post("http://127.0.0.1:8002/__test/reset");
  await page.route("**/api/**", (route) => {
    const path = new URL(route.request().url()).pathname;
    if (path.startsWith("/api/microstructure/")) return route.continue();
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
  await page
    .getByRole("region", { name: "Binary book research" })
    .getByLabel("Condition ID", { exact: true })
    .fill(condition);
});

test("native depth and fees, gap abstention, resnapshot and frozen replay", async ({
  page,
  request,
}, testInfo) => {
  const panel = page.getByRole("region", { name: "Binary book research" });
  await panel.getByLabel("Flat fee assumption (basis points)", { exact: true }).fill("100");
  await panel.getByRole("button", { name: "Inspect binary book", exact: true }).click();
  await expect(panel.getByText("YES: VERIFIED_BOOK", { exact: false })).toBeVisible();
  await expect(panel.getByRole("cell", { name: "0.55", exact: true })).toBeVisible();
  await expect(
    panel.getByRole("cell", { name: "Partial displayed depth", exact: true }),
  ).toBeVisible();
  await panel.screenshot({ path: testInfo.outputPath("binary-book-research.png") });
  await panel.getByRole("button", { name: "Freeze book research", exact: true }).click();
  await expect(panel.getByText("Frozen research:", { exact: false })).toBeVisible();
  const id = await panel.getByLabel("Frozen research ID", { exact: true }).inputValue();
  expect(id).toMatch(/^[a-f0-9-]{36}$/);
  await request.post("http://127.0.0.1:8002/__test/gap");
  await panel.getByRole("button", { name: "Inspect binary book", exact: true }).click();
  await expect(panel.getByText("YES: PARTIAL", { exact: false })).toContainText(
    "DELTA_CONTINUITY_UNPROVEN",
  );
  await expect(panel.getByRole("cell", { name: "0.55", exact: true })).toHaveCount(0);
  await panel.getByRole("button", { name: "Replay frozen book research", exact: true }).click();
  await expect(panel.getByText("Verified frozen replay", { exact: false })).toBeVisible();
  await expect(panel.getByRole("cell", { name: "0.55", exact: true })).toBeVisible();
  await panel.getByRole("button", { name: "Capture current public books", exact: true }).click();
  await expect(panel.getByText("Current public capture: captured", { exact: false })).toBeVisible();
  await panel.getByRole("button", { name: "Inspect binary book", exact: true }).click();
  await expect(panel.getByText("YES: VERIFIED_BOOK", { exact: false })).toBeVisible();
});

test("missing fees and stale books never retain a numeric cost", async ({ page, request }) => {
  const panel = page.getByRole("region", { name: "Binary book research" });
  await panel.getByRole("button", { name: "Inspect binary book", exact: true }).click();
  await expect(panel.getByRole("cell", { name: "FEE_POLICY_UNKNOWN", exact: true })).toHaveCount(3);
  await panel.getByLabel("Flat fee assumption (basis points)", { exact: true }).fill("0");
  await panel.getByRole("button", { name: "Inspect binary book", exact: true }).click();
  await expect(panel.getByRole("cell", { name: "0.55", exact: true })).toBeVisible();
  await request.post("http://127.0.0.1:8002/__test/stale");
  await panel.getByRole("button", { name: "Inspect binary book", exact: true }).click();
  await expect(panel.getByText("YES: STALE", { exact: false })).toContainText("STALE_BOOK");
  await expect(panel.getByRole("cell", { name: "0.55", exact: true })).toHaveCount(0);
});

test("existing settled execution compares with frozen depth without claiming causal impact", async ({
  page,
  request,
}) => {
  const panel = page.getByRole("region", { name: "Binary book research" });
  await panel.getByLabel("Flat fee assumption (basis points)", { exact: true }).fill("100");
  await panel.getByRole("button", { name: "Inspect binary book", exact: true }).click();
  await expect(panel.getByText("YES: VERIFIED_BOOK", { exact: false })).toBeVisible();
  await panel.getByRole("button", { name: "Freeze book research", exact: true }).click();
  await expect(panel.getByText("Frozen research:", { exact: false })).toBeVisible();
  const response = await request.post("http://127.0.0.1:8002/__test/execution");
  const { trade_id: tradeId } = await response.json();
  await panel.getByLabel("Recorded execution trade ID", { exact: true }).fill(String(tradeId));
  await panel.getByRole("button", { name: "Compare recorded execution", exact: true }).click();
  await expect(panel.getByText("Recorded execution comparison:", { exact: false })).toContainText(
    "0.02",
  );
  await expect(panel.getByText("Recorded execution comparison:", { exact: false })).toContainText(
    "not causal price impact",
  );
});
