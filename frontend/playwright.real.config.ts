import { defineConfig, devices } from "@playwright/test";
import base from "./playwright.config";

/** Explicit credentialed suite: never included by the ordinary PR test projects. */
export default defineConfig({
  ...base,
  testDir: "./e2e/real",
  timeout: 180_000,
  expect: { timeout: 150_000 },
  retries: 0,
  workers: 1,
  use: {
    ...base.use,
    ...devices["Desktop Chrome"],
    serviceWorkers: "block",
    trace: "off",
    screenshot: "only-on-failure",
  },
  projects: [{ name: "real-user-experience" }],
  webServer: (Array.isArray(base.webServer) ? base.webServer : []).map((server) => ({
    ...server,
    reuseExistingServer: false,
  })),
});
