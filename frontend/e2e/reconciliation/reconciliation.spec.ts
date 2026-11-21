import { test, expect } from "@playwright/test";

test("unknown costs remain unknown and owned evidence can be downloaded", async ({ page }) => {
  await page.addInitScript(() => {
    localStorage.setItem("polymarket_active_account", "11");
  });
  await page.route("**/api/**", (route) => {
    const path = new URL(route.request().url()).pathname;
    if (
      path.endsWith("/balance-snapshots") ||
      path.endsWith("/reconciliation") ||
      path.endsWith("/reconciliation-reports")
    )
      return route.continue();
    if (path === "/api/auth/me")
      return route.fulfill({ json: { id: 1, wallet_address: "0x" + "1".repeat(40) } });
    if (path === "/api/accounts")
      return route.fulfill({
        json: [{ id: 11, name: "Personal", kind: "personal", is_default: true }],
      });
    if (path.endsWith("/summary"))
      return route.fulfill({ json: { per_strategy: {}, totals: {}, data_quality: {} } });
    if (path.endsWith("/trades")) return route.fulfill({ json: { trades: [] } });
    return route.fulfill({ json: { strategies: [], data_quality: {} } });
  });
  await page.goto("/analytics/execution");
  const panel = page.getByRole("region", { name: "Live account reconciliation" });
  await expect(panel.getByLabel("Opening observation").locator("option")).toHaveCount(3);
  await panel.getByRole("button", { name: "Compare observations" }).click();
  await expect(panel.getByText("Reconciliation: Incomplete", { exact: true })).toBeVisible();
  await expect(panel.getByText("Net realized P&L: Unknown", { exact: true })).toBeVisible();
  await expect(panel.getByText("missing confirmed fees", { exact: true })).toBeVisible();
  await panel.getByText(/Acceptance evidence/).click();
  await expect(panel.getByText(/exchange cash and positions: incomplete/)).toBeVisible();
  await expect(panel.getByText(/forward validation: incomplete/)).toBeVisible();
  await panel.getByText("Point-in-time benchmarks", { exact: true }).click();
  await expect(panel.getByText(/Policy: arrival-midpoint-spread-v1/)).toBeVisible();
  await expect(
    panel.getByText("Verified net realized P&L: Unknown", { exact: true }),
  ).toBeVisible();
  await panel.getByRole("button", { name: "Save immutable report" }).click();
  await expect(panel.getByText(/Saved report: [a-f0-9]{64}/)).toBeVisible();
  const downloaded = page.waitForEvent("download");
  await panel.getByRole("button", { name: "Download reconciliation evidence" }).click();
  const download = await downloaded;
  expect(download.suggestedFilename()).toMatch(/^reconciliation-[a-f0-9]{12}\.json$/);
  const stream = await download.createReadStream();
  const parts: Buffer[] = [];
  for await (const chunk of stream!) parts.push(Buffer.from(chunk));
  const evidence = JSON.parse(Buffer.concat(parts).toString());
  expect(evidence.net_realized_pnl).toBeNull();
  expect(evidence.report_id).toMatch(/^[a-f0-9]{64}$/);
  expect(evidence.schema_version).toBe("live-attribution-v2");
  expect(evidence.acceptance_complete).toBe(false);
  expect(evidence.benchmarks).toHaveLength(1);
  expect(evidence.full_wallet_reconciled).toBe(false);
  expect(evidence.fill_evidence).toHaveLength(1);
  expect(evidence.input_hash).toMatch(/^[a-f0-9]{64}$/);
});
