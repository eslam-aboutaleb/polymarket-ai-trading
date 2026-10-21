/**
 * Copy Trading (authenticated): the local trader watchlist.
 *
 * The watchlist is a bookmark store, not a simulated portfolio. These tests
 * pin the truthful wording the panel must use (issue #37): leader figures
 * labelled as a proxy, an explicit as-of/source status line, "Unknown" for
 * missing data, and no fabricated balance, ROI or capital anywhere.
 */

import fs from "node:fs";
import { test, expect } from "../fixtures/base";

const WATCHLIST_KEY = "pm:mock-copy-traders:v2";
const LEGACY_WATCHLIST_KEY = "pm:mock-copy-traders:v1";
/** A syntactically valid address that is certainly not on the leaderboard. */
const UNKNOWN_WALLET = "0x1234567890abcdef1234567890abcdef12345678";

/** Copy that must never appear again: fabricated follower performance. */
const FABRICATED = /Est\. ROI|Virtual (Equity|Balance)|\$10,000|Mock Copy/i;

test.describe("Copy Trading — trader watchlist", () => {
  test.beforeEach(async ({ adminPage: page }) => {
    await page.goto("/copy-trading");
    // The watchlist lives in localStorage and the page is shared between
    // tests, so start every test from an empty watchlist.
    await page.evaluate(
      ([key, legacyKey]) => {
        localStorage.removeItem(key);
        localStorage.removeItem(legacyKey);
      },
      [WATCHLIST_KEY, LEGACY_WATCHLIST_KEY],
    );
    await page.reload();
    await page.getByRole("button", { name: "Trader Watchlist (Illustrative)" }).click();
    await expect(
      page.getByRole("heading", { name: "Trader Watchlist (Illustrative)" }),
    ).toBeVisible();
  });

  test("states the provenance of the figures and never claims follower returns", async ({
    adminPage: page,
  }) => {
    const status = page.getByTestId("watchlist-status");
    await expect(status).toContainText("Leaderboard figures as of");
    await expect(status).toContainText("source: Polymarket leaderboard");
    await expect(status).toContainText("no follower returns are calculated");

    const body = await page.locator("body").innerText();
    expect(body).toMatch(/no follower returns are calculated/i);
    expect(body).toMatch(/no funds are involved/i);
    expect(body).not.toMatch(FABRICATED);
  });

  test("labels the leaderboard columns as a proxy and shows unknown for missing data", async ({
    adminPage: page,
  }) => {
    await page.getByPlaceholder("Add wallet to watchlist (0x...)").fill(UNKNOWN_WALLET);
    await page.getByRole("button", { name: "Add to Watchlist" }).click();

    await expect(
      page.getByRole("columnheader", { name: "Leader P&L proxy (period)" }),
    ).toBeVisible();
    await expect(
      page.getByRole("columnheader", { name: "Leader Volume proxy (period)" }),
    ).toBeVisible();

    // No leaderboard entry for this wallet: the panel must say "Unknown"
    // rather than render a $0.00 flat result.
    const row = page.getByRole("row", { name: /0x1234/ });
    await expect(row.getByText("Unknown").first()).toBeVisible();

    const body = await page.locator("body").innerText();
    expect(body).not.toMatch(FABRICATED);
    expect(body).not.toMatch(/\$0\.00/);
  });

  test("removing a wallet asks for confirmation without mentioning a balance", async ({
    adminPage: page,
  }) => {
    await page.getByPlaceholder("Add wallet to watchlist (0x...)").fill(UNKNOWN_WALLET);
    await page.getByRole("button", { name: "Add to Watchlist" }).click();
    await expect(page.getByRole("button", { name: "Remove" })).toBeVisible();

    await page.getByRole("button", { name: "Remove" }).click();

    const dialog = page.getByRole("dialog");
    await expect(dialog.getByRole("heading", { name: "Remove from watchlist?" })).toBeVisible();
    await expect(dialog.getByText(/No funds or returns are involved/)).toBeVisible();
    expect(await dialog.innerText()).not.toMatch(FABRICATED);
  });

  test("exports a watchlist payload without any capital or balance field", async ({
    adminPage: page,
  }) => {
    await page.getByPlaceholder("Add wallet to watchlist (0x...)").fill(UNKNOWN_WALLET);
    await page.getByRole("button", { name: "Add to Watchlist" }).click();
    await expect(
      page.getByRole("columnheader", { name: "Leader P&L proxy (period)" }),
    ).toBeVisible();

    const downloadPromise = page.waitForEvent("download");
    await page.getByRole("button", { name: "Export Watchlist" }).click();
    const download = await downloadPromise;
    const path = await download.path();
    const payload = fs.readFileSync(path, "utf8");

    const parsed = JSON.parse(payload) as Array<Record<string, unknown>>;
    expect(parsed).toHaveLength(1);
    expect(parsed[0].wallet).toBe(UNKNOWN_WALLET);
    expect(Object.keys(parsed[0])).not.toContain("initial_capital");
    expect(payload).not.toMatch(FABRICATED);
  });

  test("imports a legacy payload and drops the fabricated capital", async ({ adminPage: page }) => {
    const legacy = JSON.stringify([
      { wallet: UNKNOWN_WALLET, created_at: "2024-01-01T00:00:00.000Z", initial_capital: 10000 },
    ]);

    await page.setInputFiles('input[data-testid="watchlist-import-input"]', {
      name: "watchlist.json",
      mimeType: "application/json",
      buffer: Buffer.from(legacy),
    });

    await expect(
      page.getByRole("columnheader", { name: "Leader P&L proxy (period)" }),
    ).toBeVisible();

    const stored = await page.evaluate((key) => localStorage.getItem(key), WATCHLIST_KEY);
    expect(stored).not.toBeNull();
    expect(stored).not.toContain("initial_capital");
    expect(
      await page.evaluate((key) => localStorage.getItem(key), LEGACY_WATCHLIST_KEY),
    ).toBeNull();

    const body = await page.locator("body").innerText();
    expect(body).not.toMatch(FABRICATED);
  });

  test("resets the watchlist after confirmation", async ({ adminPage: page }) => {
    await page.getByPlaceholder("Add wallet to watchlist (0x...)").fill(UNKNOWN_WALLET);
    await page.getByRole("button", { name: "Add to Watchlist" }).click();
    await expect(
      page.getByRole("columnheader", { name: "Leader P&L proxy (period)" }),
    ).toBeVisible();

    await page.getByRole("button", { name: "Reset Watchlist" }).click();
    const dialog = page.getByRole("dialog");
    await expect(dialog.getByRole("heading", { name: "Reset watchlist?" })).toBeVisible();
    await dialog.getByRole("button", { name: "Reset" }).click();

    await expect(page.getByText(/No traders added yet/)).toBeVisible();
    expect(await page.evaluate((key) => localStorage.getItem(key), WATCHLIST_KEY)).not.toContain(
      UNKNOWN_WALLET,
    );
  });
});
