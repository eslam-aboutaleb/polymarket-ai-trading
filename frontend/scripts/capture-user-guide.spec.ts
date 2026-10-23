import { test, expect, type Page } from "@playwright/test";
import path from "node:path";

const cid = "0x" + "ab".repeat(32);
const assets = path.resolve("../.github/images/user-guide");

// Render the real React app and API against the existing disposable fixture app.
// Stub only session/navigation services that are unrelated to these workflows.
test.beforeEach(async ({ page }) => {
  await page.route("**/api/**", (route) => {
    const pathname = new URL(route.request().url()).pathname;
    if (pathname === "/api/settings")
      return route.fulfill({ json: { simulation_mode: true, paper_balance: 1000 } });
    if (pathname === "/api/portfolio/markets/prices")
      return route.fulfill({ json: { markets: [], ts: new Date().toISOString() } });
    if (pathname === "/api/analysis/opportunities/stream")
      return route.fulfill({
        contentType: "text/event-stream",
        body: 'data: {"done":true,"total":1}\n\n',
      });
    if (
      pathname.startsWith("/api/analysis/") ||
      pathname.startsWith("/api/copy-trading/experiments") ||
      pathname.startsWith("/api/market-maker/")
    )
      return route.continue();
    if (pathname === "/api/auth/me")
      return route.fulfill({
        json: { id: 1, wallet_address: "0x" + "11".repeat(20), is_admin: false },
      });
    if (pathname === "/api/accounts") return route.fulfill({ json: [] });
    if (pathname === "/api/trades/copy-trades/pnl")
      return route.fulfill({ json: { daily_pnl: 0 } });
    if (/following|copy-trades|evaluations/.test(pathname)) return route.fulfill({ json: [] });
    return route.fulfill({ json: {} });
  });
});

async function capture(page: Page, name: string) {
  await page.screenshot({ path: path.join(assets, name), fullPage: true, animations: "disabled" });
}

test("capture market entry, source context, brief and observation history", async ({ page }) => {
  await page.route("**/api/portfolio/markets/combined?*", (route) =>
    route.fulfill({
      json: {
        markets: [
          {
            condition_id: cid,
            question: "Will the measure pass?",
            outcomePrices: ["0.7", "0.3"],
            volume24hr: 2000,
            endDate: "2026-11-03T23:59:00Z",
            tokens: [
              { outcome: "Yes", token_id: "11" },
              { outcome: "No", token_id: "22" },
            ],
          },
        ],
        count: 1,
        has_more: false,
      },
    }),
  );
  await page.goto("/opportunities");
  await page.getByRole("button", { name: "Open market details" }).click();
  await expect(page.getByRole("link", { name: "Research market", exact: true })).toBeVisible();
  await expect(page.getByText(/AI scan error:|Cannot read properties/)).toHaveCount(0);
  await capture(page, "01-open-research.png");
  await page.getByRole("link", { name: "Research market", exact: true }).click();
  await expect(page.getByText("YES 70¢", { exact: true })).toBeVisible();
  await expect(page.getByRole("heading", { name: "What determines the outcome?" })).toBeVisible();
  await page.getByText("Source identity", { exact: true }).click();
  await expect(page.getByText(cid, { exact: true })).toBeVisible();
  await capture(page, "02-source-context.png");
  await page.getByText("Source identity", { exact: true }).click();
  await page.getByRole("button", { name: "Run Deep Dive", exact: true }).click();
  await expect(page.getByRole("heading", { name: "Investigate further" })).toBeVisible();
  await expect(page.getByRole("region", { name: "contradicting evidence" })).toContainText(
    "Matched search excerpt",
  );
  await capture(page, "03-deep-dive.png");
  const historyId = "0x" + "18".repeat(32);
  await page.goto(`/analysis?condition_id=${historyId}`);
  await expect(page.getByText("YES 70¢", { exact: true })).toBeVisible();
  expect(
    (
      await page.request.post(
        `http://127.0.0.1:8002/fixtures/revise-source?condition_id=${historyId}`,
      )
    ).ok(),
  ).toBeTruthy();
  await page.reload();
  await expect(page.getByText("YES 80¢", { exact: true })).toBeVisible();
  await page.getByRole("button", { name: "Load observation history", exact: true }).click();
  const history = page.getByRole("list", { name: "Captured observations" });
  await expect(history).toContainText("YES 70¢");
  await expect(history).toContainText("YES 80¢");
  await history.locator("summary").first().click();
  await capture(page, "04-observation-history.png");
});

test("capture a saved follower simulation and its journal", async ({ page }) => {
  await page.goto("/copy-trading");
  await page
    .getByLabel("Import frozen experiment JSON")
    .setInputFiles(path.resolve("public/examples/follower-experiment.json"));
  await page.getByRole("button", { name: "Run and save simulation" }).click();
  const result = page.getByTestId("follower-result");
  await expect(result).toContainText("$7.50");
  await expect(result).toContainText("$-2.50");
  await result.getByText("0s: signal outcomes and accounting journal", { exact: true }).click();
  await expect(result.getByRole("link", { name: "Download journal" })).toBeVisible();
  await capture(page, "05-follower-simulation.png");
});

test("capture unfilled maker quote definitions", async ({ page, request }) => {
  expect((await request.post("http://127.0.0.1:8002/__fixtures/maker-cycle")).ok()).toBeTruthy();
  await page.goto("/market-making");
  await page.getByRole("button", { name: "Inspect paper quote intents" }).click();
  await expect(page.getByRole("list", { name: "Paper quote intents" })).toContainText(
    "UNFILLED_DATA_MISSING",
  );
  await expect(page.getByText("$0.00", { exact: true })).toBeVisible();
  await expect(page.getByText("PAPER", { exact: true })).toBeVisible();
  await capture(page, "06-maker-intents.png");
});
