import { test, expect } from "../fixtures/base";

/**
 * Route guards: anonymous redirects, the admin-only gate and the
 * already-signed-in redirect on /login.
 */

test.describe("Route protection", () => {
  test("anonymous visitors are redirected from the dashboard to Markets", async ({ page }) => {
    await page.goto("/");
    await expect(page).toHaveURL(/\/markets$/);
    await expect(page.getByRole("heading", { name: "Markets" })).toBeVisible();
  });

  test("anonymous visitors are redirected from Settings to Markets", async ({ page }) => {
    await page.goto("/settings");
    await expect(page).toHaveURL(/\/markets$/);
  });

  test("anonymous visitors are redirected from authenticated feature pages", async ({ page }) => {
    for (const route of [
      "/trades",
      "/whales",
      "/market-making",
      "/news",
      "/orchestrator",
      "/ops",
    ]) {
      await page.goto(route);
      await expect(page).toHaveURL(/\/markets$/);
    }
  });

  test("non-admin users cannot open the debug dashboard", async ({ userPage: page }) => {
    await page.goto("/debug");
    // AdminRoute redirects to the personal dashboard.
    await expect(page).toHaveURL(/\/$/, { timeout: 15_000 });
    await expect(page.getByRole("heading", { name: "Dashboard" })).toBeVisible();
    await expect(page.getByRole("heading", { name: /Debug/ })).toHaveCount(0);
  });

  test("admin users can open the debug dashboard", async ({ adminPage: page }) => {
    await page.goto("/debug");
    await expect(page.getByRole("heading", { name: /Debug/ }).first()).toBeVisible({
      timeout: 15_000,
    });
  });

  test("signed-in users skip the login page", async ({ userPage: page }) => {
    await page.goto("/login");
    await expect(page).toHaveURL(/\/$/, { timeout: 15_000 });
    await expect(page.getByRole("heading", { name: "Dashboard" })).toBeVisible();
  });
});
