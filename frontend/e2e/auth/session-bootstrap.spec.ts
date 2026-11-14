import { test, expect } from "@playwright/test";

test.use({ storageState: { cookies: [], origins: [] } });

for (const stalledEndpoint of ["me", "refresh"]) {
  test(`session restore finishes when ${stalledEndpoint} never responds`, async ({ page }) => {
    await page.clock.install();
    await page.route("**/api/**", async (route) => {
      const pathname = new URL(route.request().url()).pathname;
      if (pathname === `/api/auth/${stalledEndpoint}`) {
        // Leave the real browser request pending until the app cancels it.
        return;
      }
      await route.fulfill({
        status: pathname.startsWith("/api/auth/") ? 401 : 200,
        contentType: "application/json",
        body: pathname.startsWith("/api/auth/")
          ? JSON.stringify({ detail: "Not authenticated" })
          : "[]",
      });
    });
    const pending = page.waitForRequest(`**/api/auth/${stalledEndpoint}`);
    await page.goto("/");
    await pending;
    await expect(page.getByText("Restoring secure session...", { exact: true })).toBeVisible();
    await page.clock.fastForward(8_100);
    await expect(page.getByText("Restoring secure session...", { exact: true })).toHaveCount(0);
    await expect(page).toHaveURL(/\/markets$/);
    await expect(page.getByRole("heading", { name: "Markets", exact: true })).toBeVisible();
  });
}

test("a valid restored session stays signed in after the deadline", async ({ page }) => {
  await page.clock.install();
  await page.route("**/api/**", async (route) => {
    const pathname = new URL(route.request().url()).pathname;
    await route.fulfill({
      status: pathname === "/api/auth/me" ? 200 : 503,
      contentType: "application/json",
      body: JSON.stringify(
        pathname === "/api/auth/me"
          ? {
              id: 1,
              wallet_address: "0x1111111111111111111111111111111111111111",
              is_admin: false,
              created_at: "2026-01-01T00:00:00Z",
              last_login: null,
            }
          : { detail: "Unavailable in this test" },
      ),
    });
  });
  await page.goto("/settings");
  await expect(page.getByRole("heading", { name: "Settings", exact: true })).toBeVisible();
  await page.clock.fastForward(10_000);
  await expect(page).toHaveURL(/\/settings$/);
  await expect(page.getByRole("heading", { name: "Settings", exact: true })).toBeVisible();
});
