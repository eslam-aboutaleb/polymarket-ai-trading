import { test, expect } from "@playwright/test";

test("halt reporting preserves unknown exposure across reload and recovery", async ({ page }) => {
  let halted = false;
  let reconciled = false;
  let statusUnavailable = false;
  const report = () => ({
    operation_id: "durable-operation-1",
    halted: true,
    halt_status: reconciled ? "RECOVERY_REQUIRED" : "HALTING",
    operation_status: reconciled ? "RECOVERY_REQUIRED" : "PENDING",
    cancel_requests_submitted: reconciled ? 2 : 0,
    orders_confirmed_cancelled: reconciled ? 1 : 0,
    orders_still_open: reconciled ? 1 : null,
    close_requests_submitted: 0,
    positions_confirmed_closed: 0,
    unresolved_positions: null,
    message: "Trading halt persisted. Cancellation verification is pending; positions remain open.",
  });
  await page.route("**/api/**", async (route) => {
    const url = new URL(route.request().url());
    const path = url.pathname;
    if (path === "/api/auth/me") {
      return route.fulfill({ json: { id: 1, wallet_address: "0x123", is_admin: false } });
    }
    if (path === "/api/accounts") return route.fulfill({ json: [] });
    if (path === "/api/trades/emergency-stop") {
      if (route.request().method() === "POST") {
        expect(url.searchParams.get("close_positions")).toBe("false");
        halted = true;
        return route.fulfill({ status: 202, json: report() });
      }
      if (statusUnavailable) return route.fulfill({ status: 503, json: { detail: "unavailable" } });
      return route.fulfill({ json: halted ? report() : null });
    }
    if (path === "/api/v1/risk/recovery-request") {
      return route.fulfill({ status: 202, json: { resumed: false, state: "RECOVERY_REQUIRED" } });
    }
    // Other panels are outside this deterministic emergency-state test.
    return route.fulfill({ json: path.includes("opportunities") ? [] : {} });
  });
  await page.goto("/ops");
  await page.getByRole("button", { name: "Emergency Stop", exact: true }).click();
  const dialog = page.getByRole("dialog");
  await expect(dialog).toContainText("Existing positions remain exposed");
  await dialog.getByRole("button", { name: "Persist halt & request cancellation" }).click();
  await expect(page.getByText("Trading halted · PENDING")).toBeVisible();
  await expect(page.getByText(/Still open: Unverified/)).toBeVisible();
  reconciled = true;
  await expect(page.getByText(/Confirmed cancelled: 1. Still open: 1/)).toBeVisible();
  await expect(
    page.getByText(/Confirmed closed: 0. Unresolved positions: Unverified/),
  ).toBeVisible();
  await page.reload();
  await expect(page.getByText("Operation: durable-operation-1")).toBeVisible();
  await page.getByRole("button", { name: "Request Recovery", exact: true }).click();
  await expect(page.getByText(/Trading remains halted pending verification/)).toBeVisible();
  statusUnavailable = true;
  await expect(
    page.getByText("Halt status unavailable. Previous results may be stale."),
  ).toBeVisible();
  await expect(page.getByText(/Confirmed cancelled: 1. Still open: 1/)).toBeVisible();
});
