import { test, expect } from "@playwright/test";

test("static maker syncs persist one unfilled intent and zero execution volume", async ({
  page,
  request,
}, testInfo) => {
  const first = await request.post("http://127.0.0.1:8002/__fixtures/maker-cycle");
  expect(first.ok()).toBeTruthy();
  for (let n = 0; n < 9; n++) {
    const result = await request.post("http://127.0.0.1:8002/__fixtures/maker-cycle");
    expect((await result.json()).placed).toBe(0);
  }
  await page.route("**/api/**", (route) => {
    const pathname = new URL(route.request().url()).pathname;
    if (pathname.startsWith("/api/market-maker/")) return route.continue();
    if (pathname === "/api/auth/me")
      return route.fulfill({
        json: { id: 1, wallet_address: "0x" + "11".repeat(20), is_admin: false },
      });
    if (pathname === "/api/accounts") return route.fulfill({ json: [] });
    return route.fulfill({ json: {} });
  });
  await page.goto("/market-making");
  await expect(page.getByText("Synthetic maker fixture", { exact: true })).toBeVisible();
  await page.getByRole("button", { name: "Inspect paper quote intents" }).click();
  const list = page.getByRole("list", { name: "Paper quote intents" });
  await expect(list.getByRole("listitem")).toHaveCount(1);
  await expect(list).toContainText("UNFILLED_DATA_MISSING");
  await expect(page.getByText("$0.00", { exact: true })).toBeVisible();
  await page.screenshot({ path: testInfo.outputPath("paper-maker-intents.png"), fullPage: true });
  await page.reload();
  await page.getByRole("button", { name: "Inspect paper quote intents" }).click();
  await expect(
    page.getByRole("list", { name: "Paper quote intents" }).getByRole("listitem"),
  ).toHaveCount(1);
});
