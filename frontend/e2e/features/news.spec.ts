import { test, expect } from "../fixtures/base";

/**
 * News: feed refresh and article generation form.
 */

test.describe("News", () => {
  test.beforeEach(async ({ adminPage: page }) => {
    await page.goto("/news");
    await expect(page.getByRole("heading", { name: "News" })).toBeVisible();
  });

  test("renders the feed controls and empty state", async ({ adminPage: page }) => {
    await expect(page.getByRole("button", { name: /Refresh Feed/ })).toBeVisible();
    await expect(page.getByRole("heading", { name: "Generate Article" })).toBeVisible();
    await expect(page.getByPlaceholder("Condition ID")).toBeVisible();
    await expect(page.getByPlaceholder("Market question")).toBeVisible();
    await expect(page.getByText(/No articles yet/)).toBeVisible();
  });

  test("generate requires both a condition id and a question", async ({ adminPage: page }) => {
    const generate = page.getByRole("button", { name: "Generate", exact: true });
    await expect(generate).toBeDisabled();

    await page.getByPlaceholder("Condition ID").fill("0xtest");
    await expect(generate).toBeDisabled();

    await page.getByPlaceholder("Market question").fill("Will BTC hit 100k?");
    await expect(generate).toBeEnabled();
  });
});
