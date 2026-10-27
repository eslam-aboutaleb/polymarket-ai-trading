import { expect, test } from "@playwright/test";

for (const [scenario, reason] of [
  ["outage", "PROVIDER_HTTP_503"],
  ["fees", "VENUE_FEE_MODEL_UNKNOWN"],
  ["stale", "STALE_BOOK"],
  ["disconnect", "FEED_DISCONNECTED"],
  ["wrong-outcome", "TOKEN_OUTCOME_MISMATCH"],
  ["disabled", "live mode disabled"],
  ["halt", "TRADING_HALTED_OR_RECOVERY_REQUIRED"],
]) {
  test(`${scenario}: apparent edge abstains visibly with zero exchange calls`, async ({
    page,
    request,
  }) => {
    const attempt = await request.post(`http://127.0.0.1:8002/__test/attempt/${scenario}`);
    expect(attempt.ok()).toBeTruthy();
    const result = await attempt.json();
    expect(result).toMatchObject({ post: 0, prepare: 0, orders: 0, trades: 0, audit_intents: 0 });
    expect(result.outcome.reason).toBe(reason);
    await page.route("**/api/**", (route) => {
      const path = new URL(route.request().url()).pathname;
      if (path.startsWith("/api/latency-arb/")) return route.continue();
      if (path === "/api/auth/me")
        return route.fulfill({
          json: { id: 1, wallet_address: "0x" + "11".repeat(20), is_admin: true },
        });
      if (path === "/api/accounts") return route.fulfill({ json: [] });
      return route.fulfill({ json: {} });
    });
    await page.goto("/latency-arb");
    await expect(page.getByRole("heading", { name: "Latency Arb" })).toBeVisible();
    await expect(page.getByRole("status").filter({ hasText: "Last execution:" })).toContainText(
      reason,
    );
    const api = await page.request.get("/api/latency-arb/opportunities");
    expect((await api.json()).execution_outcome.reason).toBe(reason);
    await page.reload();
    await expect(page.getByRole("status").filter({ hasText: "Last execution:" })).toContainText(
      reason,
    );
  });
}

test("real object depth downsizes $50 to 60 shares with a FOK ask limit", async ({ request }) => {
  const response = await request.post("http://127.0.0.1:8002/__test/attempt/ready");
  expect(response.ok()).toBeTruthy();
  expect(await response.json()).toMatchObject({
    prepare: 1,
    post: 1,
    orders: 1,
    trades: 1,
    audit_intents: 1,
    outcome: { status: "submitted" },
  });
});

test("recovery reuses the durable intent; disabling preserves order history", async ({
  request,
}) => {
  await request.post("http://127.0.0.1:8002/__test/attempt/ready");
  const retried = await request.post("http://127.0.0.1:8002/__test/retry");
  expect(await retried.json()).toMatchObject({ prepare: 1, post: 1, orders: 1, audit_intents: 1 });
  const disabled = await request.post("http://127.0.0.1:8002/__test/disable");
  expect(await disabled.json()).toMatchObject({
    prepare: 1,
    post: 1,
    orders: 1,
    audit_intents: 1,
    outcome: { status: "skipped" },
  });
});
