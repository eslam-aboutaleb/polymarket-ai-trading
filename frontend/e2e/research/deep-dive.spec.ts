import { test, expect } from "@playwright/test";

const cid = (byte: string) => "0x" + byte.repeat(32);

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

test("Deep Dive presents both evidence sides and source limits", async ({ page }, testInfo) => {
  const errors: string[] = [];
  page.on("pageerror", (error) => errors.push(error.message));
  await page.goto(`/analysis?condition_id=${cid("ab")}`);
  await page.getByRole("button", { name: "Run Deep Dive", exact: true }).click();
  await expect(page.getByRole("heading", { name: "Investigate further" })).toBeVisible();
  await expect(page.getByRole("region", { name: "supporting evidence" })).toContainText(
    "Matched venue excerpt",
  );
  await expect(page.getByRole("region", { name: "contradicting evidence" })).toContainText(
    "Matched search excerpt",
  );
  await expect(page.getByText("Whether a legal challenge is still active.")).toBeVisible();
  await expect(page.getByText("Provider: fixture · Model: deterministic")).toBeVisible();
  await expect(page.getByRole("link", { name: "Review claim source" }).nth(1)).toHaveAttribute(
    "href",
    "https://evidence.example/report",
  );
  await page.screenshot({ path: testInfo.outputPath("deep-dive.png"), fullPage: true });
  expect(errors).toEqual([]);
});

test("unsupported pivotal citations stay unverified and cannot create unsafe links", async ({
  page,
}) => {
  await page.goto(`/analysis?condition_id=${cid("12")}`);
  await page.getByRole("button", { name: "Run Deep Dive", exact: true }).click();
  await expect(page.getByRole("heading", { name: "Insufficient evidence" })).toBeVisible();
  await expect(page.getByRole("region", { name: "contradicting evidence" })).toContainText(
    "Unverified attribution",
  );
  await expect(page.locator('a[href^="javascript:"]')).toHaveCount(0);
  await expect(page.getByRole("link", { name: "Review claim source" })).toHaveCount(1);
});

test("malformed provider output fails closed without raw prose recommendation cards", async ({
  page,
}) => {
  await page.goto(`/analysis?condition_id=${cid("11")}`);
  await page.getByRole("button", { name: "Run Deep Dive", exact: true }).click();
  await expect(page.getByRole("heading", { name: "Insufficient evidence" })).toBeVisible();
  await expect(
    page.getByText("The provider did not return a usable structured brief."),
  ).toBeVisible();
  await expect(page.getByRole("link", { name: "Review claim source" })).toHaveCount(0);
});

test("provider failure stays an error, with an explicit retry", async ({ page }) => {
  await page.goto(`/analysis?condition_id=${cid("13")}`);
  await page.getByRole("button", { name: "Run Deep Dive", exact: true }).click();
  await expect(page.getByRole("alert")).toContainText("Market research failed");
  await expect(page.getByRole("button", { name: "Run research again" })).toBeEnabled();
  await expect(page.getByRole("heading", { name: "Investigate further" })).toHaveCount(0);
});

test("cancellation aborts generation and late results cannot populate the brief", async ({
  page,
}) => {
  await page.goto(`/analysis?condition_id=${cid("14")}`);
  await page.getByRole("button", { name: "Run Deep Dive", exact: true }).click();
  await expect(page.getByRole("status")).toContainText("Gathering research");
  await page.getByRole("button", { name: "Cancel research" }).click();
  await expect(page.getByRole("status")).toContainText("Research cancelled");
  await expect(page.getByRole("button", { name: "Run Deep Dive", exact: true })).toBeEnabled();
  await expect(page.getByRole("heading", { name: "Investigate further" })).toHaveCount(0);
});

test("SSE terminal messages dispatch completion once and errors never dispatch success", async ({
  page,
}) => {
  await page.goto(`/analysis?condition_id=${cid("ab")}`);
  const events = await page.evaluate(async () => {
    // Exercise the actual shared browser transport, not a duplicate parser.
    // @ts-expect-error Vite serves this local source module in the test browser.
    const { consumeSSEStream } = await import("/src/services/sseStream.ts");
    const seen: string[] = [];
    const callbacks = { onDone: () => seen.push("done"), onError: () => seen.push("error") };
    await consumeSSEStream(new Response('data: {"done":true}\n\n'), callbacks);
    await consumeSSEStream(new Response('data: {"error":"failure"}\n\n'), callbacks);
    return seen;
  });
  expect(events).toEqual(["done", "error"]);
});

test("updated source replaces the original cards when a Deep Dive completes", async ({ page }) => {
  await page.goto(`/analysis?condition_id=${cid("16")}`);
  await expect(page.getByText("YES 70¢", { exact: true })).toBeVisible();
  await page.request.post("http://127.0.0.1:8002/fixtures/revise-source");
  await page.getByRole("button", { name: "Run Deep Dive", exact: true }).click();
  await expect(page.getByRole("heading", { name: "Investigate further" })).toBeVisible();
  await expect(page.getByText("YES 80¢", { exact: true })).toBeVisible();
  await expect(page.getByText(/Revised: certification must be final/)).toBeVisible();
  await expect(page.getByText("YES 70¢", { exact: true })).toHaveCount(0);
});

test("aborted browser request cannot accept a delayed complete response", async ({ page }) => {
  const response = await page.request.post(
    "http://127.0.0.1:8002/api/analysis/market/research/stream",
    {
      data: { condition_id: cid("ab") },
    },
  );
  const body = await response.text();
  let release!: () => void;
  let completed!: () => void;
  let entered!: () => void;
  const routed = new Promise<void>((resolve) => {
    entered = resolve;
  });
  const barrier = new Promise<void>((resolve) => {
    release = resolve;
  });
  const settled = new Promise<void>((resolve) => {
    completed = resolve;
  });
  await page.route("**/api/analysis/market/research/stream", async (route) => {
    entered();
    await barrier;
    await route.fulfill({ contentType: "text/event-stream", body }).catch(() => {});
    completed();
  });
  await page.goto(`/analysis?condition_id=${cid("ab")}`);
  await page.getByRole("button", { name: "Run Deep Dive", exact: true }).click();
  await routed;
  const aborted = page.waitForEvent("requestfailed", {
    predicate: (request) => request.url().endsWith("/market/research/stream"),
  });
  await page.getByRole("button", { name: "Cancel research" }).click();
  await aborted;
  release();
  await settled;
  await expect(page.getByRole("status")).toContainText("Research cancelled");
  await expect(page.getByRole("heading", { name: "Investigate further" })).toHaveCount(0);
});

for (const corruption of ["missing terminal", "wrong market"] as const) {
  test(`incomplete or mismatched stream fails closed: ${corruption}`, async ({ page }) => {
    const response = await page.request.post(
      "http://127.0.0.1:8002/api/analysis/market/research/stream",
      {
        data: { condition_id: cid("ab") },
      },
    );
    const original = await response.text();
    const body =
      corruption === "missing terminal"
        ? original.replace('data: {"done":true}\n\n', "")
        : original.replaceAll(cid("ab"), cid("11"));
    await page.route("**/api/analysis/market/research/stream", (route) =>
      route.fulfill({ contentType: "text/event-stream", body }),
    );
    await page.goto(`/analysis?condition_id=${cid("ab")}`);
    await page.getByRole("button", { name: "Run Deep Dive", exact: true }).click();
    await expect(page.getByRole("alert")).toContainText(
      corruption === "missing terminal" ? "before a complete result" : "invalid or mismatched",
    );
    await expect(page.getByRole("heading", { name: "Investigate further" })).toHaveCount(0);
  });
}
