import { test, expect } from "../fixtures/base";

/**
 * Settings: profile, trading key, notification channels and accounts tabs.
 */

test.describe("Settings", () => {
  test.beforeEach(async ({ adminPage: page }) => {
    await page.goto("/settings");
    await expect(page.getByRole("heading", { name: "Settings" })).toBeVisible();
  });

  test("profile tab saves the display name", async ({ adminPage: page }) => {
    // The input's placeholder is the connected wallet address, not "Username".
    const displayName = page.getByPlaceholder(/^0x[a-fA-F0-9]+$/);
    await expect(displayName).toBeVisible();
    // A fresh value each run: the Save Profile button only appears once the
    // field actually changes.
    const newName = `e2e-tester-${Date.now()}`;
    await displayName.fill(newName);
    const save = page.getByRole("button", { name: "Save Profile" });
    await expect(save).toBeVisible();
    await save.click();

    // The save completes: the button disappears again once the draft is clean.
    await expect(save).toBeHidden({ timeout: 20_000 });

    // The change really persisted server-side: reload and read it back.
    await page.reload();
    await expect(page.getByPlaceholder(/^0x[a-fA-F0-9]+$/)).toHaveValue(newName, {
      timeout: 20_000,
    });
  });

  test("personal AI model can be chosen and saved", async ({ adminPage: page }) => {
    await expect(page.getByRole("heading", { name: "Personal AI Model" })).toBeVisible();
    // Scope to the Personal AI Model select (the header has its own account select).
    const provider = page.locator("select").filter({ hasText: "Openai" });
    await provider.selectOption("openai");
    await page.getByRole("button", { name: "Save AI Preferences" }).click();
    await expect(page.getByText(/saved|Saved|success/i).first()).toBeVisible({ timeout: 15_000 });
  });

  test("trading tab manages the stored trading key", async ({ adminPage: page }) => {
    await page.getByRole("button", { name: "Trading", exact: true }).click();
    await expect(page.getByRole("heading", { name: "Trading Key" })).toBeVisible();
    // The panel explains what the key unlocks and offers exactly one action:
    // connect while disconnected, disconnect while a key is stored (logout
    // purges the stored key, so either state is valid here).
    await expect(page.getByText(/encrypted at rest and is never used for login/)).toBeVisible();
    const connect = page.getByRole("button", { name: "Connect Trading Key" });
    const disconnect = page.getByRole("button", { name: "Disconnect Trading Key" });
    expect((await connect.count()) + (await disconnect.count())).toBe(1);
  });

  test("notifications tab renders channel configuration", async ({ adminPage: page }) => {
    await page.getByRole("button", { name: "Notifications", exact: true }).click();
    await expect(page.getByRole("heading", { name: "Notification Channels" })).toBeVisible();
    await expect(page.getByRole("heading", { name: "Add Channel" })).toBeVisible();
    await expect(page.getByRole("button", { name: "Save Preferences" })).toBeVisible();
  });

  test("accounts tab lists the personal account", async ({ adminPage: page }) => {
    await page.getByRole("button", { name: "Accounts", exact: true }).click();
    await expect(page.getByRole("heading", { name: "Trading Accounts" })).toBeVisible();
    // The personal account is the default list entry (the header select also
    // contains a "Personal" option, so scope to the account list).
    const personalAccount = page.getByRole("listitem").filter({ hasText: "Personal" });
    await expect(personalAccount.first()).toBeVisible();
    await expect(personalAccount.first().getByText("Default")).toBeVisible();
  });
});
