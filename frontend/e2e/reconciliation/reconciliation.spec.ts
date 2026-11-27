import { test, expect, Page } from "@playwright/test";
import { writeFile } from "node:fs/promises";

async function mountAccount(page: Page) {
  await page.addInitScript(() => {
    localStorage.setItem("polymarket_active_account", "11");
  });
  await page.route("**/api/**", (route) => {
    const path = new URL(route.request().url()).pathname;
    if (
      path.endsWith("/balance-snapshots") ||
      path.endsWith("/reconciliation") ||
      path.endsWith("/reconciliation-reports") ||
      path.startsWith("/api/trades/analytics/attribution-studies") ||
      path.startsWith("/api/portfolio/redemption-intents")
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
}

test("unknown costs remain unknown and owned evidence can be downloaded", async ({ page }) => {
  await mountAccount(page);
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

test("finalized receipt verification reaches the real API and saves actual fee evidence", async ({
  page,
  request,
}) => {
  const seeded = await request.post("http://127.0.0.1:18139/__test/chain");
  expect(seeded.ok()).toBeTruthy();
  await mountAccount(page);
  await page.goto("/analytics/execution");
  const panel = page.getByRole("region", { name: "Live account reconciliation" });
  await expect(panel.getByLabel("Opening observation").locator("option")).toHaveCount(3);
  await panel.getByRole("button", { name: "Compare observations" }).click();
  await expect(panel.getByText("Net realized P&L: Unknown", { exact: true })).toBeVisible();
  await panel.getByRole("button", { name: "Verify finalized evidence and save" }).click();
  await expect(panel.getByText("Reconciliation: Reconciled", { exact: true })).toBeVisible();
  await expect(panel.getByText("Net realized P&L: 1.700000 pUSD", { exact: true })).toBeVisible();
  await expect(panel.getByText("Confirmed fees: 0.300000 pUSD", { exact: true })).toBeVisible();
  await panel.getByText(/Acceptance evidence/).click();
  await expect(panel.getByText(/exchange cash and positions: pass/)).toBeVisible();
  await expect(panel.getByText(/actual cost evidence: pass/)).toBeVisible();
  await expect(panel.getByText(/forward validation: incomplete/)).toBeVisible();
  await expect(
    panel.getByText("Verified net realized P&L: Unknown", { exact: true }),
  ).toBeVisible();
});

test("future study registration is persisted with a frozen policy", async ({ page }) => {
  await mountAccount(page);
  await page.goto("/analytics/execution");
  const panel = page.getByRole("region", { name: "Live account reconciliation" });
  await panel.getByText("Prospective benchmark study", { exact: true }).click();
  const registered = page.waitForResponse(
    (response) =>
      response.url().endsWith("/api/trades/analytics/attribution-studies") &&
      response.request().method() === "POST",
  );
  await panel.getByRole("button", { name: "Register future study" }).click();
  const response = await registered;
  expect(response.ok()).toBeTruthy();
  const policy = await response.json();
  expect(policy.study_id).toMatch(/^[a-f0-9]{64}$/);
  expect(policy.implementation_fingerprint).toMatch(/^[a-f0-9]{64}$/);
  expect(policy.configuration_fingerprint).toMatch(/^[a-f0-9]{64}$/);
  expect(policy.minimum_events).toBe(20);
  await expect(panel.getByLabel("Study for saved evidence")).toHaveValue(policy.study_id);
});

for (const classified of [true, false]) {
  test(`mock funded account keeps ${classified ? "known funding out of profit" : "unknown funding unverified"}`, async ({
    page,
    request,
  }) => {
    const seeded = await request.post(
      `http://127.0.0.1:18139/__test/chain?deposit_usdc=900&classify_deposit=${classified}`,
    );
    expect(seeded.ok()).toBeTruthy();
    expect((await seeded.json()).classification).toBe("synthetic_rpc_boundary_only");
    await mountAccount(page);
    await page.goto("/analytics/execution");
    const panel = page.getByRole("region", { name: "Live account reconciliation" });
    await expect(panel.getByLabel("Opening observation").locator("option")).toHaveCount(3);
    await panel.getByRole("button", { name: "Compare observations" }).click();
    await expect(panel.getByText("Net realized P&L: Unknown", { exact: true })).toBeVisible();
    const saved = page.waitForResponse(
      (response) =>
        response.url().endsWith("/reconciliation-reports") &&
        response.request().method() === "POST",
    );
    await panel.getByRole("button", { name: "Verify finalized evidence and save" }).click();
    const response = await saved;
    expect(response.ok()).toBeTruthy();
    const evidence = await response.json();
    const evidencePath = test.info().outputPath("mock-funded-account-evidence.json");
    await writeFile(
      evidencePath,
      JSON.stringify(
        {
          classification: "synthetic_mock_funded_account_not_real_money",
          deposit_classified: classified,
          report: evidence,
        },
        null,
        2,
      ),
    );
    await test.info().attach("mock-funded-account-evidence", {
      path: evidencePath,
      contentType: "application/json",
    });
    expect(Number(evidence.chain_reconciliation.opening.cash_usdc)).toBe(100);
    expect(Number(evidence.chain_reconciliation.closing.cash_usdc)).toBe(1001.7);
    expect(evidence.verified_net_realized_pnl).toBeNull();
    expect(evidence.acceptance_complete).toBe(false);
    if (classified) {
      expect(Number(evidence.classified_external_cashflow)).toBe(900);
      expect(Number(evidence.net_realized_pnl)).toBe(1.7);
      expect(Number(evidence.total_fees_usdc)).toBe(0.3);
      expect(Number(evidence.cash_residual_usdc)).toBe(0);
      expect(evidence.chain_reconciliation.scope_reconciled).toBe(true);
      await expect(
        panel.getByText("Net realized P&L: 1.700000 pUSD", { exact: true }),
      ).toBeVisible();
    } else {
      expect(Number(evidence.unattributed_external_cashflow)).toBe(900);
      expect(evidence.chain_reconciliation.scope_reconciled).toBe(false);
      expect(evidence.net_realized_pnl).toBeNull();
      expect(evidence.reasons).toContain("UNCLASSIFIED_EXTERNAL_COLLATERAL_FLOW");
      await expect(panel.getByText("Net realized P&L: Unknown", { exact: true })).toBeVisible();
    }
  });
}

test("redemption preview never broadcasts or reads a nonce", async ({ page, request }) => {
  const scenario = await request.post("http://127.0.0.1:18139/__test/redemption");
  const { condition_id } = await scenario.json();
  await mountAccount(page);
  await page.goto("/portfolio/redemptions");
  const panel = page.getByRole("region", { name: "Redemption evidence" });
  await panel.getByLabel("Resolved condition ID").fill(condition_id);
  await panel.getByRole("button", { name: "Preview redemption", exact: true }).click();
  await expect(panel.getByText("Preview — not broadcast", { exact: true })).toBeVisible();
  await expect(panel.getByText(/Finalized payout evidence: Unknown/)).toBeVisible();
  await expect(panel.getByRole("link", { name: /submitted transaction/ })).toHaveCount(0);
  const counts = await (
    await request.post("http://127.0.0.1:18139/__test/redemption/finality")
  ).json();
  expect(counts.broadcasts).toBe(0);
  expect(counts.nonce_reads).toBe(0);
});

test("redemption requires finality and withdraws a reorged payout", async ({ page, request }) => {
  await request.post("http://127.0.0.1:18139/__test/redemption?submitted=true");
  await mountAccount(page);
  await page.goto("/portfolio/redemptions");
  const panel = page.getByRole("region", { name: "Redemption evidence" });
  await expect(
    panel.getByText("Submitted — awaiting finalized receipt", { exact: true }),
  ).toBeVisible();
  await panel.getByRole("button", { name: "Check finalized receipt" }).click();
  await expect(panel.getByText(/Finalized payout evidence: Unknown/)).toBeVisible();
  await request.post("http://127.0.0.1:18139/__test/redemption/finality");
  await panel.getByRole("button", { name: "Check finalized receipt" }).click();
  await expect(panel.getByText("Finalized receipt verified", { exact: true })).toBeVisible();
  await expect(panel.getByText(/Finalized payout evidence: 10/)).toBeVisible();
  await request.post("http://127.0.0.1:18139/__test/redemption/finality?reorg=true");
  await panel.getByRole("button", { name: "Check finalized receipt" }).click();
  await expect(
    panel.getByText("Receipt invalidated — review required", { exact: true }),
  ).toBeVisible();
  await expect(panel.getByText(/Finalized payout evidence: Unknown/)).toBeVisible();
  const counts = await (
    await request.post("http://127.0.0.1:18139/__test/redemption/finality")
  ).json();
  expect(counts.broadcasts).toBe(1);
});
