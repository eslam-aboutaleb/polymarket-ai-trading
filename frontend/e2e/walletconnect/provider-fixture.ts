import type { Page } from "@playwright/test";
import { installMockWallet } from "../fixtures/base";
import { installGuideFixtures } from "../../scripts/user-guide-fixtures";

export const address = "0x" + "11".repeat(20);
export async function installWalletConnectFixture(
  page: Page,
  mode: "ok" | "rejected" | "empty" = "ok",
) {
  await installGuideFixtures(page, true);
  await installMockWallet(page, { address });
  await page.addInitScript((value) => {
    (window as any).__walletConnectTest = { mode: value, disconnects: 0, options: null };
  }, mode);
  // Replace only the external SDK boundary, never the app's connector or login code.
  // This Vite development-module route is intentionally test-only.
  await page.route(
    /\/node_modules\/\.vite\/deps\/@walletconnect_ethereum-provider\.js(?:\?.*)?$/,
    (route) =>
      route.fulfill({
        contentType: "application/javascript",
        body: `export default { async init(options) {
      const state = window.__walletConnectTest;
      state.options = { configured: Boolean(options.projectId), chains: options.chains, showQrModal: options.showQrModal };
      const provider = window.ethereum;
      const request = provider.request.bind(provider);
      provider.request = args => state.mode === "empty" && args.method === "eth_accounts" ? Promise.resolve([]) : request(args);
      provider.connect = async () => { if (state.mode === "rejected") throw { code: 4001 }; };
      provider.disconnect = async () => { state.disconnects++; };
      return provider;
    }};`,
      }),
  );
}
