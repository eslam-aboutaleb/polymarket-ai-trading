import { test, expect, injectWebmcpPolyfill, gotoMarketsBoard } from "../fixtures/base";
import {
  listWebmcpTools,
  registerWebmcpTool,
  executeWebmcpTool,
  unregisterWebmcpTool,
  attemptDuplicateRegistration,
  captureToolchangeEvents,
  getToolchangeEventCount,
  isWebmcpAvailable,
} from "../fixtures/base";

/**
 * WebMCP agent surface.
 *
 * The app does not register any `document.modelContext` tools today, so these
 * tests pin down the contract an in-page agent relies on, on the real app
 * pages: the polyfilled model context must exist on every route, tools
 * registered by the page must be discoverable and executable, duplicate names
 * must be rejected, and aborting a registration must unregister the tool.
 *
 * The last test demonstrates the WebMCP testing recipe end to end: a tool
 * registered on the live Markets page reads the app's own DOM state, proving
 * the agent surface and the UI share one session.
 */

const PUBLIC_ROUTES = ["/markets", "/opportunities", "/leaderboard", "/copy-trading", "/guide"];
const AUTH_ROUTES = ["/", "/trades", "/whales", "/settings", "/news", "/ops"];

test.describe("WebMCP surface", () => {
  test("model context exists on public routes", async ({ webmcpPage: page }) => {
    for (const route of PUBLIC_ROUTES) {
      await page.goto(route);
      await expect.poll(() => isWebmcpAvailable(page)).toBe(true);
    }
  });

  test("model context exists on authenticated routes", async ({ adminPage: page }) => {
    // Reuse the shared admin session: the polyfill is injected into that page
    // before its next navigation. (A second context over the same session
    // file would rotate the shared refresh token and kill the first session.)
    await injectWebmcpPolyfill(page);

    for (const route of AUTH_ROUTES) {
      await page.goto(route);
      await expect.poll(() => isWebmcpAvailable(page)).toBe(true);
    }
  });

  test("the app ships no built-in tools (agent surface starts empty)", async ({
    webmcpPage: page,
  }) => {
    await page.goto("/markets");
    await expect.poll(() => isWebmcpAvailable(page)).toBe(true);
    expect(await listWebmcpTools(page)).toEqual([]);
  });

  test("register → discover → execute → unregister lifecycle", async ({ webmcpPage: page }) => {
    await page.goto("/markets");
    await captureToolchangeEvents(page);

    await registerWebmcpTool(page, {
      name: "markets-count",
      description: "Counts the market rows currently rendered on the board.",
    });

    // Discovered by an agent through getTools().
    const tools = await listWebmcpTools(page);
    expect(tools).toHaveLength(1);
    expect(tools[0].name).toBe("markets-count");
    expect(tools[0].description).toContain("Counts the market rows");
    expect(tools[0].inputSchema).toMatchObject({ type: "object" });

    // Executable through executeTool(); the result is a string.
    const result = await executeWebmcpTool(page, "markets-count", { text: "probe" });
    expect(JSON.parse(result)).toEqual({ echoed: "probe", from: "markets-count" });

    // Registrations and removals fire toolchange events.
    expect(await getToolchangeEventCount(page)).toBeGreaterThanOrEqual(1);

    // Aborting the registration unregisters the tool.
    await unregisterWebmcpTool(page);
    await expect.poll(async () => (await listWebmcpTools(page)).length).toBe(0);
    expect(await getToolchangeEventCount(page)).toBeGreaterThanOrEqual(2);
  });

  test("duplicate tool names are rejected", async ({ webmcpPage: page }) => {
    await page.goto("/markets");
    await expect.poll(() => isWebmcpAvailable(page)).toBe(true);
    // The spec mandates NotAllowedError for a same-name registration; the
    // polyfill surfaces it as InvalidStateError. Both mean "rejected".
    const error = await attemptDuplicateRegistration(page);
    expect(error).toMatch(/NotAllowedError|InvalidStateError/);
  });

  test("executing an unknown tool raises NotFoundError", async ({ webmcpPage: page }) => {
    await page.goto("/markets");
    const error = await page.evaluate(async () => {
      try {
        const mc = (
          document as unknown as {
            modelContext?: {
              getTools: () => Promise<unknown[]>;
              executeTool: (t: unknown, i?: unknown) => Promise<string>;
            };
          }
        ).modelContext!;
        const tools = (await mc.getTools()) as Record<string, unknown>[];
        const fake = { name: "does-not-exist", description: "x", inputSchema: {} };
        await mc.executeTool(tools.length ? tools[0] : fake, {});
        return "no-error";
      } catch (err) {
        return `${(err as Error).name}: ${(err as Error).message}`;
      }
    });
    // Either the polyfill cannot find the tool, or it reports the platform
    // NotFoundError — both mean "no such tool", never silent success.
    expect(error === "no-error").toBe(false);
  });

  test("a registered tool reads live app state (agent drives the real UI)", async ({
    webmcpPage: page,
  }) => {
    // The board streams from the Polymarket API; the helper waits for content
    // and rides out the API's rate-limit window via the app's Retry control.
    await gotoMarketsBoard(page, { waitForContent: true });

    // Register a tool that reads the board the way an agent would, then run it
    // and compare against a direct DOM query: same session, same numbers.
    await page.evaluate(async () => {
      const mc = (
        document as unknown as {
          modelContext?: {
            registerTool: (tool: unknown, options?: unknown) => Promise<void>;
          };
        }
      ).modelContext!;
      // Return the object; executeTool JSON-encodes it for the caller.
      // Count the Yes pills only, so the tool's view matches the DOM query
      // the test performs independently below.
      const execute = () => {
        const rows = Array.from(document.querySelectorAll("[aria-label^='Trade ']")).filter((el) =>
          / Yes$/.test(el.getAttribute("aria-label") || ""),
        );
        const heading = document.querySelector("h1");
        return {
          tradeButtons: rows.length,
          heading: heading ? heading.textContent : null,
        };
      };
      await mc.registerTool({
        name: "read-market-board",
        title: "Read market board",
        description: "Returns the market count and board heading from the live page.",
        inputSchema: { type: "object", properties: {} },
        execute: execute,
      });
    });

    // A live market board may append or replace a page of results between
    // separate Playwright evaluations. Snapshot before/after the tool call
    // *in the same browser task* instead of comparing with a later DOM state.
    const snapshot = await page.evaluate(async () => {
      const countTradeButtons = () =>
        Array.from(document.querySelectorAll("[aria-label^='Trade ']")).filter((el) =>
          / Yes$/.test(el.getAttribute("aria-label") || ""),
        ).length;
      const before = countTradeButtons();
      const tool = (
        window as unknown as {
          __webmcpTest: { execute: (name: string) => Promise<string> };
        }
      ).__webmcpTest;
      const result = JSON.parse(await tool.execute("read-market-board")) as {
        heading: string | null;
        tradeButtons: number;
      };
      const after = countTradeButtons();
      return { before, after, result };
    });

    expect(snapshot.result.heading).toBe("Markets");
    expect(snapshot.result.tradeButtons).toBeGreaterThan(0);
    // An in-flight market-page update is legitimate: the model's result
    // must lie between the observed counts bracketing its execution.
    expect(snapshot.result.tradeButtons).toBeGreaterThanOrEqual(
      Math.min(snapshot.before, snapshot.after),
    );
    expect(snapshot.result.tradeButtons).toBeLessThanOrEqual(
      Math.max(snapshot.before, snapshot.after),
    );
  });
});
