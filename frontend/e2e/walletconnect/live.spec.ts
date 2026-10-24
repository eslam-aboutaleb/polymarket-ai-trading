import { test, expect } from "@playwright/test";
import { installGuideFixtures } from "../../scripts/user-guide-fixtures";

// Real SDK + Reown relay; API responses are controlled. No wallet is paired or signed.
test("real project ID initializes a WalletConnect pairing QR", async ({ page }) => {
  await installGuideFixtures(page, true);
  await page.goto("/login");
  await page.getByRole("button", { name: "WalletConnect", exact: true }).click();
  await page.getByRole("button", { name: "Connect Wallet", exact: true }).click();
  await expect(page.locator("w3m-modal")).toBeVisible({ timeout: 60_000 });
  await expect(page.locator("w3m-qr-code")).toBeVisible({ timeout: 60_000 });
  await expect(page.getByRole("button", { name: "Continue to Sign" })).toHaveCount(0);
});
