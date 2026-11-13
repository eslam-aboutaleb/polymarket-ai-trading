import { defineConfig, devices } from "@playwright/test";
import { randomBytes } from "node:crypto";

export default defineConfig({
  testDir: "./e2e/provider",
  timeout: 60_000,
  expect: { timeout: 15_000 },
  workers: 1,
  reporter: [["list"]],
  use: {
    ...devices["Desktop Chrome"],
    baseURL: "http://127.0.0.1:15137",
    serviceWorkers: "block",
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
  },
  webServer: [
    {
      command:
        ".venv/bin/python -m uvicorn e2e_provider_app:app --app-dir tests --host 127.0.0.1 --port 18137",
      cwd: "../backend",
      url: "http://127.0.0.1:18137/",
      reuseExistingServer: false,
      env: {
        PYTHONPATH: ".:../services/shared",
        ENVIRONMENT: "test",
        JWT_SECRET_KEY: randomBytes(32).toString("hex"),
        REFRESH_TOKEN_HASH_SECRET: randomBytes(32).toString("hex"),
        TEST_DATABASE_URL: process.env.TEST_DATABASE_URL ?? process.env.DATABASE_URL ?? "",
      },
    },
    {
      command: "node_modules/.bin/vite --host 127.0.0.1 --port 15137 --strictPort",
      url: "http://127.0.0.1:15137",
      reuseExistingServer: false,
      env: { VITE_API_URL: "http://127.0.0.1:18137" },
    },
  ],
});
