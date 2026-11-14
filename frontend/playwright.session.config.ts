import { defineConfig, devices } from "@playwright/test";

export default defineConfig({
  testDir: "./e2e/auth",
  testMatch: "session-bootstrap.spec.ts",
  timeout: 20_000,
  expect: { timeout: 3_000 },
  workers: 1,
  use: {
    ...devices["Desktop Chrome"],
    baseURL: process.env.E2E_BASE_URL || "http://127.0.0.1:5186",
  },
  webServer: process.env.E2E_BASE_URL
    ? undefined
    : {
        command: "npm run dev -- --host 127.0.0.1 --port 5186 --strictPort",
        url: "http://127.0.0.1:5186",
        reuseExistingServer: !process.env.CI,
        env: { VITE_API_URL: "" },
      },
});
