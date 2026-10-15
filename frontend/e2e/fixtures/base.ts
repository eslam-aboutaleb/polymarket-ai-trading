/**
 * Shared e2e fixtures.
 *
 * `webmcpPage` is an ordinary page with the WebMCP polyfill injected before any
 * application script, so `document.modelContext` exists exactly as it would in
 * a WebMCP-enabled browser. A small raw helper script (not transformed by the
 * test runner, so it stays plain browser JS) exposes a `window.__webmcpTest`
 * namespace the tests call into, following the WebMCP testing recipe: assert
 * on the agent-facing surface (`getTools()` / `executeTool()`), never on
 * implementation details.
 *
 * `mockWallet` installs a deterministic EIP-1193 provider (`window.ethereum`)
 * so the real wallet-signature login flow can be driven headlessly: the test
 * reads the challenge from the sign step, signs it with ethers (EIP-191
 * `personal_sign`), hands the signature back to the mock provider, and the
 * backend verifies it exactly like any wallet signature.
 */

import {
  test as base,
  expect,
  type Browser,
  type BrowserContext,
  type Page,
} from "@playwright/test";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const FIXTURES_DIR = path.dirname(fileURLToPath(import.meta.url));
const E2E_DIR = path.dirname(FIXTURES_DIR);

const POLYFILL_SOURCE = fs.readFileSync(
  path.resolve(E2E_DIR, "../node_modules/@mcp-b/webmcp-polyfill/dist/index.iife.js"),
  "utf8",
);

// Raw browser script: no bundler/transform helpers, plain ES2017.
const WEBMCP_TEST_HELPERS = `
window.__webmcpTest = {
  controller: null,
  events: [],
  resetEvents() {
    this.events = [];
    const mc = document.modelContext;
    if (mc) mc.addEventListener('toolchange', () => this.events.push('toolchange'));
  },
  available() {
    return Boolean(document.modelContext);
  },
  async listTools() {
    const mc = document.modelContext;
    if (!mc) return [];
    const tools = await mc.getTools();
    return tools.map((t) => ({
      name: t.name,
      title: t.title || undefined,
      description: t.description || undefined,
      inputSchema: t.inputSchema,
      annotations: t.annotations || undefined,
    }));
  },
  async registerEcho(name, description) {
    const mc = document.modelContext;
    if (!mc) throw new Error('document.modelContext is not available');
    const controller = new AbortController();
    this.controller = controller;
    // executeTool() resolves to the JSON encoding of whatever execute returns,
    // so return the object itself — not a pre-serialized string.
    const execute = (input) => ({ echoed: (input && input.text) || null, from: name });
    await mc.registerTool(
      {
        name: name,
        title: name,
        description: description,
        inputSchema: {
          type: 'object',
          properties: { text: { type: 'string', description: 'Text echoed back' } },
        },
        execute: execute,
      },
      { signal: controller.signal },
    );
  },
  async registerDuplicate() {
    const mc = document.modelContext;
    const execute = () => 'dup';
    try {
      await mc.registerTool(
        { name: 'duplicate', description: 'first', inputSchema: { type: 'object' }, execute: execute },
      );
      await mc.registerTool(
        { name: 'duplicate', description: 'second', inputSchema: { type: 'object' }, execute: execute },
      );
      return 'no-error';
    } catch (err) {
      return err.name + ': ' + err.message;
    }
  },
  unregister() {
    if (this.controller) this.controller.abort();
  },
  async execute(name, input) {
    const mc = document.modelContext;
    const tools = await mc.getTools();
    const tool = tools.find((t) => t.name === name);
    if (!tool) throw new Error('Tool not registered: ' + name);
    return mc.executeTool(tool, input);
  },
  eventCount() {
    return this.events.length;
  },
};
`;

export interface WebmcpTool {
  name: string;
  title?: string;
  description?: string;
  inputSchema?: unknown;
  annotations?: Record<string, boolean>;
}

/**
 * Navigate to the Markets board and tolerate the app's own "too many
 * requests" alert: the board is backed by the public Polymarket API, whose
 * 30/min budget can be exhausted by repeated runs. The app surfaces a Retry
 * button in that state, so use it instead of failing the UX check.
 */
export async function gotoMarketsBoard(
  page: Page,
  options?: { waitForContent?: boolean },
): Promise<void> {
  await page.goto("/markets");
  await expect(page.getByRole("heading", { name: "Markets" })).toBeVisible();

  const summary = page.getByText(/Showing \d+ events \/ \d+ options/);
  // The public API budget is 30 requests/minute; repeated suite runs can
  // exhaust it, and the app shows its own Retry control until the window
  // clears. Ride it out here instead of failing the UX assertion.
  for (let attempt = 0; attempt < 40; attempt += 1) {
    if (await summary.isVisible()) {
      const text = (await summary.textContent()) ?? "";
      if (!/Showing 0 events \/ 0 options/.test(text)) {
        if (options?.waitForContent) {
          await expect
            .poll(async () => page.getByRole("button", { name: /^Trade .+ Yes$/ }).count(), {
              timeout: 60_000,
            })
            .toBeGreaterThan(0);
        }
        return;
      }
    }
    const retry = page.getByRole("button", { name: "Retry" });
    if (await retry.count()) {
      await retry.first().click();
    }
    await page.waitForTimeout(5_000);
  }
}

/**
 * Inject the WebMCP polyfill + test helpers into an existing page before its
 * next navigation. Use this instead of building a second authenticated
 * context: two contexts sharing one session file would rotate the same
 * refresh token and kill each other's session.
 */
export async function injectWebmcpPolyfill(page: Page): Promise<void> {
  await page.addInitScript({ content: POLYFILL_SOURCE });
  await page.addInitScript({ content: WEBMCP_TEST_HELPERS });
}

/** True when the page exposes a WebMCP model context (polyfilled or native). */
export async function isWebmcpAvailable(page: Page): Promise<boolean> {
  return page.evaluate(() =>
    Boolean((document as unknown as { modelContext?: unknown }).modelContext),
  );
}

/** List the tools the current page exposes to an in-page agent. */
export async function listWebmcpTools(page: Page): Promise<WebmcpTool[]> {
  return page.evaluate(() =>
    (
      window as unknown as {
        __webmcpTest?: { listTools: () => Promise<WebmcpTool[]> };
      }
    ).__webmcpTest!.listTools(),
  );
}

/** Register a deterministic echo tool through the real WebMCP API. */
export async function registerWebmcpTool(
  page: Page,
  options: { name: string; description: string },
): Promise<void> {
  await page.evaluate(
    (opts) =>
      (
        window as unknown as {
          __webmcpTest?: { registerEcho: (n: string, d: string) => Promise<void> };
        }
      ).__webmcpTest!.registerEcho(opts.name, opts.description),
    options,
  );
}

/** Register the same tool name twice and return the error the API raises. */
export async function attemptDuplicateRegistration(page: Page): Promise<string> {
  return page.evaluate(() =>
    (
      window as unknown as { __webmcpTest?: { registerDuplicate: () => Promise<string> } }
    ).__webmcpTest!.registerDuplicate(),
  );
}

/** Execute a registered tool and return its string result. */
export async function executeWebmcpTool(
  page: Page,
  name: string,
  input?: unknown,
): Promise<string> {
  return page.evaluate(
    (args) =>
      (
        window as unknown as {
          __webmcpTest?: { execute: (n: string, i?: unknown) => Promise<string> };
        }
      ).__webmcpTest!.execute(args.name, args.input),
    { name, input },
  );
}

/** Unregister the tool registered by `registerWebmcpTool`. */
export async function unregisterWebmcpTool(page: Page): Promise<void> {
  await page.evaluate(() =>
    (window as unknown as { __webmcpTest?: { unregister: () => void } }).__webmcpTest!.unregister(),
  );
}

/** Start collecting WebMCP `toolchange` events on the page. */
export async function captureToolchangeEvents(page: Page): Promise<void> {
  await page.evaluate(() =>
    (
      window as unknown as { __webmcpTest?: { resetEvents: () => void } }
    ).__webmcpTest!.resetEvents(),
  );
}

/** How many `toolchange` events the page has seen since capture started. */
export async function getToolchangeEventCount(page: Page): Promise<number> {
  return page.evaluate(() =>
    (
      window as unknown as { __webmcpTest?: { eventCount: () => number } }
    ).__webmcpTest!.eventCount(),
  );
}

/** Install a mock EIP-1193 wallet before page scripts run. */
export async function installMockWallet(page: Page, options: { address: string }): Promise<void> {
  const address = options.address.toLowerCase();
  await page.addInitScript((addr) => {
    const state = { signature: null as string | null, accounts: [addr] };
    (window as unknown as { __mockWallet: typeof state }).__mockWallet = state;
    const provider = {
      isMetaMask: true,
      request(args: { method: string }) {
        switch (args.method) {
          case "eth_requestAccounts":
          case "eth_accounts":
            return Promise.resolve(state.accounts);
          case "personal_sign":
            if (!state.signature) {
              return Promise.reject(new Error("Mock wallet has no signature set"));
            }
            return Promise.resolve(state.signature);
          case "eth_chainId":
            return Promise.resolve("0x89");
          case "net_version":
            return Promise.resolve("137");
          default:
            return Promise.resolve(null);
        }
      },
      on() {},
      removeListener() {},
    };
    (window as unknown as { ethereum?: unknown }).ethereum = provider;
  }, address);
}

/** Hand a prepared signature to the mock wallet installed by `installMockWallet`. */
export async function provideMockSignature(page: Page, signature: string): Promise<void> {
  await page.evaluate((sig) => {
    const state = (window as unknown as { __mockWallet: { signature: string | null } })
      .__mockWallet;
    state.signature = sig;
  }, signature);
}

/**
 * Keep a shared storage-state session usable across tests.
 *
 * The backend rotates refresh tokens on every refresh (the old row is revoked)
 * and logout revokes the presented token, so a storage state saved once goes
 * stale: after the 10-minute access token expires, later contexts built from
 * it would be stuck on "Restoring secure session…". Because the suite runs
 * serially (workers: 1), each fixture can repair the chain deterministically:
 * check `/api/auth/me`, refresh (rotating) and persist the new cookies, and
 * only fall back to a full wallet login when the refresh token itself is dead.
 */
const SESSION_ACCOUNTS = {
  user: {
    privateKey: "0x5de4111afa1a4b94908f83103eb1f1706367c2e68ca870fc3fb9a804cdab365a",
    wallet: "0x3c44cdddb6a900fa2b585dd299e03d12fa4293bc",
    expectAdmin: false,
  },
  admin: {
    privateKey: "0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80",
    wallet: "0xf39fd6e51aad88f6f4ce6ab8827279cfffb92266",
    expectAdmin: true,
  },
} as const;

export type Role = keyof typeof SESSION_ACCOUNTS;

/**
 * Bring a freshly-created context onto a valid session thread.
 *
 * Called once per worker (see the fixtures below) rather than per test: the
 * backend caps `/api/auth/refresh` at 30/minute and `login-with-key` at
 * 5/minute, so per-test session maintenance would exhaust both budgets during
 * a full suite run. One long-lived context per role lets the app's own
 * in-page refresh keep the session alive without e2e churn.
 */
export async function ensureFreshSession(
  context: BrowserContext,
  role: Role,
  baseURL: string,
): Promise<void> {
  const statePath = path.resolve(E2E_DIR, ".auth", `${role}.json`);

  // Save on the happy path too: the app rotates the refresh token in-page
  // during a test, and the state file must carry the rotated cookie so the
  // next context (and the next suite run) starts from a live session.
  const me = await context.request.get(`${baseURL}/api/auth/me`);
  if (me.ok()) {
    await context.storageState({ path: statePath });
    return;
  }

  const refreshed = await context.request.post(`${baseURL}/api/auth/refresh`);
  if (refreshed.ok()) {
    await context.storageState({ path: statePath });
    return;
  }

  // Last resort: full wallet login (rate-limited, kept rare by the paths above).
  const login = await context.request.post(`${baseURL}/api/auth/login-with-key`, {
    data: { private_key: SESSION_ACCOUNTS[role].privateKey, keep_logged_in: true },
  });
  if (!login.ok()) {
    throw new Error(`Could not restore the ${role} e2e session: login failed (${login.status()})`);
  }
  const meAfterLogin = await context.request.get(`${baseURL}/api/auth/me`);
  if (!meAfterLogin.ok()) {
    throw new Error(`Could not restore the ${role} e2e session after re-login`);
  }
  await context.storageState({ path: statePath });
}

/** Test-scoped fixtures. */
interface Fixtures {
  /** Page with the WebMCP polyfill + test helpers injected before app code. */
  webmcpPage: Page;
}

/**
 * Worker-scoped fixtures: the authenticated pages are created once per worker
 * so the whole suite shares one session thread per role.
 */
interface WorkerFixtures {
  /** Regular authenticated page (storage state from the `user` wallet). */
  userPage: Page;
  /** Admin authenticated page (storage state from the `admin` wallet). */
  adminPage: Page;
}

/**
 * Authenticated pages share one browser context per worker. The backend
 * rotates refresh tokens on every refresh and rate-limits the auth endpoints,
 * so recreating a context per test would keep invalidating the saved session
 * thread. A single long-lived context lets the app's own refresh keep the
 * session alive for the whole run.
 */
const BASE_URL = process.env.E2E_BASE_URL ?? "http://localhost:5174";

async function rolePage(
  browser: Browser,
  role: Role,
): Promise<{ page: Page; close: () => Promise<void> }> {
  const context = await browser.newContext({
    baseURL: BASE_URL,
    storageState: path.resolve(E2E_DIR, ".auth", `${role}.json`),
  });
  await ensureFreshSession(context, role, BASE_URL);
  const page = await context.newPage();
  return { page, close: () => context.close() };
}

export const test = base.extend<Fixtures, WorkerFixtures>({
  webmcpPage: async ({ page }, use) => {
    await page.addInitScript({ content: POLYFILL_SOURCE });
    await page.addInitScript({ content: WEBMCP_TEST_HELPERS });
    await use(page);
  },
  userPage: [
    async ({ browser }, use) => {
      const { page, close } = await rolePage(browser, "user");
      await use(page);
      await close();
    },
    { scope: "worker" },
  ],
  adminPage: [
    async ({ browser }, use) => {
      const { page, close } = await rolePage(browser, "admin");
      await use(page);
      await close();
    },
    { scope: "worker" },
  ],
});

export { expect };
