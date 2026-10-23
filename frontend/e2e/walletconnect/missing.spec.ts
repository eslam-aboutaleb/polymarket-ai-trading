import { test, expect } from "@playwright/test";
import { installGuideFixtures } from "../../scripts/user-guide-fixtures";

test("missing project ID disables WalletConnect while retaining browser-wallet login", async ({
  page,
}) => {
  await installGuideFixtures(page, true);
  await page.goto("/login");
  await expect(page.getByRole("button", { name: "WalletConnect", exact: true })).toBeDisabled();
  await expect(page.getByText(/WalletConnect disabled: set/)).toBeVisible();
  await expect(page.getByRole("button", { name: "Browser Wallet", exact: true })).toBeEnabled();
  await page.getByRole("button", { name: "Connect Wallet", exact: true }).click();
  await expect(page.getByText(/No browser wallet detected/)).toBeVisible();
});
