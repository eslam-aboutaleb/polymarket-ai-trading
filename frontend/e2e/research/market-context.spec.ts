import { test, expect } from "@playwright/test";

const cid = "0x" + "ab".repeat(32);

test.beforeEach(async ({ page }) => {
  // Only unrelated shell services are stubbed. Market-context requests reach
  // FastAPI, which uses a controlled external venue boundary.
  await page.route("**/api/**", async (route) => {
    const path = new URL(route.request().url()).pathname;
    if (
      path.startsWith("/api/analysis/markets/") ||
      path === "/api/analysis/market/research/stream"
    ) {
      return route.continue();
    }
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

test("canonical market source overrides tampered URL prices and dates", async ({
  page,
}, testInfo) => {
  const errors: string[] = [];
  page.on("pageerror", (error) => errors.push(error.message));
  await page.goto(
    `/analysis?condition_id=${cid}&market_title=Forged&yes_price=0.01&end_date=forged`,
  );
  await expect(page.getByRole("heading", { name: "Will the measure pass?" })).toBeVisible();
  await expect(page.getByText("YES 70¢", { exact: true })).toBeVisible();
  await expect(page.getByText("NO 30¢", { exact: true })).toBeVisible();
  await expect(
    page.getByText("Research only · Indicative prices, not executable quotes"),
  ).toBeVisible();
  await expect(page.getByText(/A tie resolves NO/)).toBeVisible();
  await expect(
    page.getByText("Settlement cutoff and oracle: not independently verified."),
  ).toBeVisible();
  await expect(page.getByRole("link", { name: "Review original market source" })).toHaveAttribute(
    "href",
    `https://clob.polymarket.com/markets/${cid}`,
  );
  await page.getByText("Source identity", { exact: true }).click();
  await expect(page.getByText(cid, { exact: true })).toBeVisible();
  await page.reload();
  await expect(page.getByText("YES 70¢", { exact: true })).toBeVisible();
  expect(errors).toEqual([]);
  await page.screenshot({ path: testInfo.outputPath("market-context.png"), fullPage: true });
});

test("missing rules and price remain unavailable instead of guessed", async ({ page }) => {
  await page.goto(`/analysis?condition_id=${"0x" + "cd".repeat(32)}`);
  await expect(page.getByText("NO unavailable", { exact: true })).toBeVisible();
  await expect(
    page.getByText("Resolution rules unavailable. Do not infer them from the title."),
  ).toBeVisible();
});

test("wrong market identity fails closed and retry preserves the error", async ({ page }) => {
  await page.goto(`/analysis?condition_id=${"0x" + "ef".repeat(32)}`);
  await expect(page.getByRole("alert")).toContainText("different condition ID");
  await page.getByRole("button", { name: "Retry market lookup" }).click();
  await expect(page.getByRole("alert")).toContainText("different condition ID");
  await expect(page.getByText("YES 50¢")).toHaveCount(0);
});

test("invalid market links show an error with no default prices", async ({ page }) => {
  await page.goto("/analysis?condition_id=invalid&yes_price=0.5");
  await expect(page.getByRole("heading", { name: "Market research unavailable" })).toBeVisible();
  await expect(page.getByText("YES 50¢")).toHaveCount(0);
});

test("rules and warnings remain readable on mobile", async ({ page }) => {
  await page.setViewportSize({ width: 375, height: 812 });
  await page.goto(`/analysis?condition_id=${cid}`);
  await expect(page.getByRole("heading", { name: "What determines the outcome?" })).toBeVisible();
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(
    true,
  );
  await page.getByRole("link", { name: "Review original market source" }).focus();
  await expect(page.getByRole("link", { name: "Review original market source" })).toBeFocused();
});

test("source-backed research preserves an explicit legacy AI continuation", async ({ page }) => {
  await page.route("**/api/analysis/market/stream", (route) =>
    route.fulfill({
      contentType: "text/event-stream",
      body: 'data: {"chunk":"Legacy research output"}\n\ndata: {"done":true}\n\n',
    }),
  );
  await page.goto(`/analysis?condition_id=${cid}`);
  await page.getByRole("link", { name: "Continue to legacy AI analysis" }).click();
  await expect(page.getByText("Legacy research output")).toBeVisible();
  await expect(page.getByText(/Link-supplied prices and dates are unverified/)).toBeVisible();
});

test("Opportunities opens canonical research without removing AI analysis", async ({ page }) => {
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
  await expect(page.getByRole("button", { name: "AI Analysis", exact: true })).toBeVisible();
  await page.getByRole("link", { name: "Research market", exact: true }).click();
  await expect(page).toHaveURL(new RegExp(`condition_id=${cid}`));
  await expect(page.getByText("YES 70¢", { exact: true })).toBeVisible();
});
