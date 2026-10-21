import { test, expect, installMockWallet, provideMockSignature } from "../fixtures/base";
import { Wallet } from "ethers";
import { ADMIN_PRIVATE_KEY, ADMIN_WALLET, USER_PRIVATE_KEY, USER_WALLET } from "../global-setup";

/**
 * Wallet-signature authentication (EIP-191 `personal_sign`).
 *
 * The mock EIP-1193 provider signs nothing on its own: the test reads the
 * challenge the backend issued, signs it with ethers like a real wallet would,
 * hands the signature to the provider, and the app completes the same
 * challenge → sign → verify → session flow a browser wallet drives.
 */

const CHALLENGE_PATTERN = /^Sign this message to authenticate with Polymarket AI Trading:/;

test.describe("Login page", () => {
  test("renders the sign-in form", async ({ page }) => {
    await page.goto("/login");
    await expect(page.getByRole("heading", { name: "Polymarket AI" })).toBeVisible();
    await expect(page.getByText("AI-Powered Trading Automation")).toBeVisible();
    await expect(page.getByRole("button", { name: "Connect Wallet" })).toBeVisible();
    await expect(page.getByText("Sign in with your Ethereum wallet")).toBeVisible();
  });

  test("reports a missing browser wallet", async ({ page }) => {
    await page.goto("/login");
    await page.getByRole("button", { name: "Connect Wallet" }).click();
    await expect(
      page.getByText(/No browser wallet detected\. Install MetaMask\/Rabby or use WalletConnect\./),
    ).toBeVisible();
  });

  test("rejects a wallet that returns no signature", async ({ page }) => {
    await installMockWallet(page, { address: USER_WALLET });
    await page.goto("/login");

    await page.getByRole("button", { name: "Connect Wallet" }).click();
    // Ready step shows the connected account.
    await expect(page.getByText(USER_WALLET)).toBeVisible();
    await page.getByRole("button", { name: "Continue to Sign" }).click();

    // Challenge step without providing a signature to the mock wallet.
    await expect(page.getByText(CHALLENGE_PATTERN)).toBeVisible();
    await page.getByRole("button", { name: "Sign Message" }).click();
    await expect(page.getByText(/Mock wallet has no signature set/)).toBeVisible();
  });

  test("full signature login lands on the dashboard and logout returns to the gate", async ({
    page,
  }) => {
    await installMockWallet(page, { address: ADMIN_WALLET });
    await page.goto("/login");

    // 1. Connect the wallet.
    await page.getByRole("button", { name: "Connect Wallet" }).click();
    await expect(page.getByText(ADMIN_WALLET)).toBeVisible();

    // 2. Keep the session checkbox toggles.
    const keepLoggedIn = page.locator("#keepLoggedInWallet");
    await expect(keepLoggedIn).not.toBeChecked();
    await keepLoggedIn.check();
    await expect(keepLoggedIn).toBeChecked();

    // 3. Request the challenge.
    await page.getByRole("button", { name: "Continue to Sign" }).click();
    const challenge = await page
      .getByText(CHALLENGE_PATTERN)
      .evaluate((el) => el.textContent ?? "");
    expect(challenge).toContain(ADMIN_WALLET);

    // 4. Sign it exactly like a wallet would and hand the signature over.
    const signature = await new Wallet(ADMIN_PRIVATE_KEY).signMessage(challenge);
    await provideMockSignature(page, signature);
    await page.getByRole("button", { name: "Sign Message" }).click();

    // 5. Session established: dashboard with the wallet chip.
    await expect(page).toHaveURL(/\/$/, { timeout: 15_000 });
    await expect(page.getByRole("heading", { name: "Dashboard" })).toBeVisible();
    await expect(page.getByText(/0xf39f\.\.\.2266/).first()).toBeVisible();

    // 6. Logout asks for confirmation and then clears the session.
    await page.getByRole("button", { name: "Logout" }).first().click();
    await expect(page.getByRole("heading", { name: "Confirm Logout" })).toBeVisible();
    await page.getByRole("dialog", { name: "Confirm Logout" })
      .getByRole("button", { name: "Log out", exact: true })
      .click();
    await expect(page.getByRole("button", { name: "Connect Wallet" }).first()).toBeVisible();
  });
  test("rejects a signature the backend cannot verify", async ({ page }) => {
    await installMockWallet(page, { address: USER_WALLET });
    await page.goto("/login");

    await page.getByRole("button", { name: "Connect Wallet" }).click();
    await page.getByRole("button", { name: "Continue to Sign" }).click();
    const challenge = await page
      .getByText(CHALLENGE_PATTERN)
      .evaluate((el) => el.textContent ?? "");

    // Sign a *different* message: the signature is valid ECDSA but does not
    // match the issued challenge, so the backend must reject it.
    const wrongSignature = await new Wallet(ADMIN_PRIVATE_KEY).signMessage(challenge + " tampered");
    await provideMockSignature(page, wrongSignature);
    await page.getByRole("button", { name: "Sign Message" }).click();

    await expect(page.getByText("Invalid signature")).toBeVisible();
    await expect(page).toHaveURL(/\/login$/);
  });
});

test.describe("Login modal on public pages", () => {
  test("opens from the Connect Wallet CTA and closes again", async ({ page }) => {
    await page.goto("/markets");
    await page.getByRole("button", { name: "Connect Wallet" }).click();
    const modal = page.getByRole("heading", { name: "Connect Wallet" });
    await expect(modal).toBeVisible();

    await page.getByRole("dialog", { name: "Connect Wallet" })
      .getByRole("button", { name: "Close", exact: true })
      .click();
    await expect(modal).toBeHidden();
  });

  test("completes signature login from the modal", async ({ page }) => {
    await installMockWallet(page, { address: ADMIN_WALLET });
    await page.goto("/markets");

    await page.getByRole("button", { name: "Connect Wallet" }).click();
    await page.getByRole("button", { name: "Connect Wallet" }).nth(1).click();
    await page.getByRole("button", { name: "Continue to Sign" }).click();
    const challenge = await page
      .getByText(CHALLENGE_PATTERN)
      .evaluate((el) => el.textContent ?? "");
    const signature = await new Wallet(ADMIN_PRIVATE_KEY).signMessage(challenge);
    await provideMockSignature(page, signature);
    await page.getByRole("button", { name: "Sign Message" }).click();

    // The modal closes and the visitor stays on Markets, now authenticated:
    // the nav swaps the Connect Wallet CTA for the wallet chip + Logout.
    await expect(page.getByRole("button", { name: "Logout" })).toBeVisible({ timeout: 25_000 });
    await expect(page.getByRole("button", { name: "Connect Wallet" })).toHaveCount(0);
    await expect(page.getByText(/0xf39f\.\.\.2266/).first()).toBeVisible();
  });
});
