import { defineConfig, devices } from "@playwright/test";

export default defineConfig({
  testDir: "./e2e/safety",
  timeout: 30_000,
  workers: 1,
  use: {
    ...devices["Desktop Chrome"],
    baseURL: "http://127.0.0.1:5186",
    serviceWorkers: "block",
    trace: "retain-on-failure",
  },
  webServer: {
    command: "node_modules/.bin/vite --host 127.0.0.1 --port 5186 --strictPort",
    url: "http://127.0.0.1:5186",
    reuseExistingServer: !process.env.CI,
  },
});
