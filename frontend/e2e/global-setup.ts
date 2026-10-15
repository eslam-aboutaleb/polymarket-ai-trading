/**
 * Global e2e setup: signs in the test wallets once and persists their
 * sessions as Playwright storage state.
 *
 * Two well-known Hardhat/Anvil development keys are used (public, documented
 * test keys that hold no value on any network):
 *
 *   - admin  — first entry of the backend's `ADMIN_WALLETS`, so the app
 *              grants `is_admin` (debug dashboard, ops center, admin settings).
 *   - user   — not in `ADMIN_WALLETS`, exercising the regular-user surface
 *              including redirects away from admin-only routes.
 *
 * Logging in through the browser origin (`/api` → Vite proxy → backend) means
 * the httpOnly session cookies are stored for `localhost` and reused by every
 * test that loads the storage state.
 */

import { chromium, type BrowserContext, type FullConfig } from "@playwright/test";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const E2E_DIR = path.dirname(fileURLToPath(import.meta.url));

export const ADMIN_PRIVATE_KEY =
  "0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80";
export const ADMIN_WALLET = "0xf39fd6e51aad88f6f4ce6ab8827279cfffb92266";

export const USER_PRIVATE_KEY =
  "0x5de4111afa1a4b94908f83103eb1f1706367c2e68ca870fc3fb9a804cdab365a";
export const USER_WALLET = "0x3c44cdddb6a900fa2b585dd299e03d12fa4293bc";

const BASE_URL = process.env.E2E_BASE_URL ?? "http://localhost:5174";
const AUTH_DIR = path.resolve(E2E_DIR, ".auth");

interface LoginResult {
  wallet_address: string;
  is_admin: boolean;
}

async function signIn(context: BrowserContext, privateKey: string): Promise<LoginResult> {
  const response = await context.request.post(`${BASE_URL}/api/auth/login-with-key`, {
    data: { private_key: privateKey, keep_logged_in: true },
  });
  if (!response.ok()) {
    const body = await response.text();
    throw new Error(`login-with-key failed (${response.status()}): ${body}`);
  }

  const me = await context.request.get(`${BASE_URL}/api/auth/me`);
  if (!me.ok()) {
    const body = await me.text();
    throw new Error(`/api/auth/me failed (${me.status()}): ${body}`);
  }
  return (await me.json()) as LoginResult;
}

async function globalSetup(_config: FullConfig): Promise<void> {
  fs.mkdirSync(AUTH_DIR, { recursive: true });

  const browser = await chromium.launch();

  const accounts: Array<{ name: string; privateKey: string; expectAdmin: boolean }> = [
    { name: "admin", privateKey: ADMIN_PRIVATE_KEY, expectAdmin: true },
    { name: "user", privateKey: USER_PRIVATE_KEY, expectAdmin: false },
  ];

  for (const account of accounts) {
    const statePath = path.join(AUTH_DIR, `${account.name}.json`);

    // Reuse a still-valid session when one exists: the backend rate-limits
    // login endpoints (5/min per IP), which repeated suite runs would hit.
    if (fs.existsSync(statePath)) {
      const existing = await browser.newContext({ baseURL: BASE_URL, storageState: statePath });
      try {
        const me = await existing.request.get(`${BASE_URL}/api/auth/me`);
        if (me.ok()) {
          const user = (await me.json()) as LoginResult;
          if (user.is_admin === account.expectAdmin) {
            console.log(
              `[global-setup] ${account.name} session reused for ${user.wallet_address} ` +
                `(is_admin=${user.is_admin})`,
            );
            continue;
          }
        }
      } catch {
        // fall through to a fresh login
      }
      await existing.close();
    }

    const context = await browser.newContext({ baseURL: BASE_URL });
    try {
      const user = await signIn(context, account.privateKey);
      if (user.is_admin !== account.expectAdmin) {
        throw new Error(
          `Wallet ${user.wallet_address} expected is_admin=${account.expectAdmin} but got ${user.is_admin}. ` +
            `Check that ADMIN_WALLETS includes the admin test wallet and that the backend has been ` +
            `restarted since the wallet first logged in (admin sync runs at startup).`,
        );
      }
      await context.storageState({ path: statePath });
      console.log(
        `[global-setup] ${account.name} session saved for ${user.wallet_address} (is_admin=${user.is_admin})`,
      );
    } finally {
      await context.close();
    }
  }

  await browser.close();
}

export default globalSetup;
