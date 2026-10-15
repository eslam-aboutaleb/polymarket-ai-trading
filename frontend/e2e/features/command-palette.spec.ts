import { test, expect } from "../fixtures/base";

/**
 * Ask the AI (command palette): open/close keyboard flows and the input.
 */

test.describe("Ask the AI — command palette", () => {
  test.beforeEach(async ({ adminPage: page }) => {
    await page.goto("/command-palette");
    await expect(page.getByRole("heading", { name: "Ask the AI" })).toBeVisible();
  });

  test("opens from the button and closes with Escape", async ({ adminPage: page }) => {
    await page.getByRole("button", { name: "Open palette" }).click();
    const dialog = page.getByRole("dialog", { name: "Ask the AI" });
    await expect(dialog).toBeVisible();
    await expect(
      dialog.getByPlaceholder("Should I buy YES on the Fed rate decision market?"),
    ).toBeVisible();

    await page.keyboard.press("Escape");
    await expect(dialog).toBeHidden();
  });

  test("Cmd/Ctrl+K opens the palette from the page", async ({ adminPage: page }) => {
    await page.keyboard.press("ControlOrMeta+k");
    await expect(page.getByRole("dialog", { name: "Ask the AI" })).toBeVisible();
  });

  test("ask button enables only with a question", async ({ adminPage: page }) => {
    await page.getByRole("button", { name: "Open palette" }).click();
    const dialog = page.getByRole("dialog", { name: "Ask the AI" });
    const ask = dialog.getByRole("button", { name: /^Ask$/ });
    await expect(ask).toBeDisabled();
    await dialog.getByRole("combobox").fill("What is the outlook for the Fed rate market?");
    await expect(ask).toBeEnabled();
  });
});
