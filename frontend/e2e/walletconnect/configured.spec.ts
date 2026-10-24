import { test, expect } from "@playwright/test";
import { address, installWalletConnectFixture } from "./provider-fixture";

async function connect(page: import("@playwright/test").Page) {
  await page.goto("/login");
  await page.getByRole("button", { name: "WalletConnect", exact: true }).click();
  await page.getByRole("button", { name: "Connect Wallet", exact: true }).click();
}

test("configured WalletConnect selects Polygon and reaches signature review", async ({ page }) => {
  await installWalletConnectFixture(page);
  await connect(page);
  await expect(page.getByText(address, { exact: true })).toBeVisible();
  await expect(page.getByText("Provider: WalletConnect", { exact: true })).toBeVisible();
  await expect(page.getByRole("button", { name: "Continue to Sign" })).toBeEnabled();
  expect(await page.evaluate(() => (window as any).__walletConnectTest.options)).toEqual({
    configured: true,
    chains: [137],
    showQrModal: true,
  });
  await page.getByRole("button", { name: "Change Wallet", exact: true }).click();
  await expect(page.getByRole("button", { name: "Connect Wallet", exact: true })).toBeVisible();
  expect(await page.evaluate(() => (window as any).__walletConnectTest.disconnects)).toBe(1);
});

test("rejected pairing disconnects and allows a successful retry", async ({ page }) => {
  await installWalletConnectFixture(page, "rejected");
  await connect(page);
  await expect(page.getByText("Request rejected in wallet.", { exact: true })).toBeVisible();
  expect(await page.evaluate(() => (window as any).__walletConnectTest.disconnects)).toBe(1);
  await page.evaluate(() => {
    (window as any).__walletConnectTest.mode = "ok";
  });
  await page.getByRole("button", { name: "Connect Wallet", exact: true }).click();
  await expect(page.getByText(address, { exact: true })).toBeVisible();
});

test("empty WalletConnect accounts cannot become a connected session", async ({ page }) => {
  await installWalletConnectFixture(page, "empty");
  await connect(page);
  await expect(
    page.getByText("No wallet account returned by WalletConnect.", { exact: true }),
  ).toBeVisible();
  await expect(page.getByRole("button", { name: "Continue to Sign" })).toHaveCount(0);
  expect(await page.evaluate(() => (window as any).__walletConnectTest.disconnects)).toBe(1);
});
