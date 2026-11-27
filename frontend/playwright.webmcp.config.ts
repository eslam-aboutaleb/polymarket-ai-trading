import { defineConfig, devices } from "@playwright/test";

/** Backend-free config for the production WebMCP registry (API traffic is route-mocked). */
export default defineConfig({
  testDir: "./e2e/webmcp",
  timeout: 45_000,
  workers: 1,
  use: {
    ...devices["Desktop Chrome"],
    baseURL: "http://127.0.0.1:5188",
    serviceWorkers: "block",
    trace: "retain-on-failure",
  },
  webServer: {
    command: "node_modules/.bin/vite --host 127.0.0.1 --port 5188 --strictPort",
    url: "http://127.0.0.1:5188",
    reuseExistingServer: !process.env.CI,
  },
});
