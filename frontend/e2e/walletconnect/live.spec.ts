import { test, expect } from "@playwright/test";
import { installGuideFixtures } from "../../scripts/user-guide-fixtures";

// Real SDK + Reown relay; API responses are controlled. No wallet is paired or signed.
test("real project ID initializes a WalletConnect pairing QR", async ({ page }) => {
  // A QR can render before the real project and relay have accepted the request.
  const cdp = await page.context().newCDPSession(page);
  await cdp.send("Network.enable");
  const relayRequests = new Set<string>();
  let relayAccepted = false;
  cdp.on("Network.webSocketCreated", ({ requestId, url }) => {
    if (/^wss:\/\/relay\.walletconnect\.(com|org)(?:\/|\?)/.test(url)) relayRequests.add(requestId);
  });
  cdp.on("Network.webSocketHandshakeResponseReceived", ({ requestId, response }) => {
    if (relayRequests.has(requestId) && response.status === 101) relayAccepted = true;
  });
  const projectLimits = page.waitForResponse((response) => {
    const url = new URL(response.url());
    return url.hostname === "api.web3modal.org" && url.pathname === "/appkit/v1/project-limits";
  });
  await page.addInitScript(() => {
    (window as any).__walletCspViolations = [];
    document.addEventListener("securitypolicyviolation", (event) => {
      // Keep diagnostics useful without recording project IDs or pairing URIs.
      let origin = event.blockedURI;
      try {
        origin = new URL(origin).origin;
      } catch {
        /* inline/eval directives */
      }
      (window as any).__walletCspViolations.push({ directive: event.violatedDirective, origin });
    });
  });
  await installGuideFixtures(page, true);
  await page.goto("/login");
  await page.getByRole("button", { name: "WalletConnect", exact: true }).click();
  await page.getByRole("button", { name: "Connect Wallet", exact: true }).click();
  await expect(page.locator("w3m-modal")).toBeVisible({ timeout: 60_000 });
  await expect(page.locator("wui-qr-code")).toBeVisible({ timeout: 60_000 });
  await expect(page.locator("wui-qr-code")).toHaveAttribute("uri", /^wc:/);
  await expect(page.getByRole("button", { name: "Continue to Sign" })).toHaveCount(0);
  expect((await projectLimits).status()).toBe(200);
  await expect
    .poll(() => relayAccepted, {
      message: "Reown relay accepts the project WebSocket handshake",
      timeout: 30_000,
    })
    .toBe(true);
  expect(await page.evaluate(() => (window as any).__walletCspViolations)).toEqual([]);
  if (process.env.UPDATE_WALLET_SCREENSHOT === "1") {
    await page.screenshot({
      path: new URL("../../../user guide/images/app-walletconnect-qr.png", import.meta.url)
        .pathname,
      animations: "disabled",
    });
  }
});
