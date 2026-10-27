import { defineConfig, devices } from "@playwright/test";
import { randomBytes } from "node:crypto";

export default defineConfig({
  testDir: "./e2e/executable",
  timeout: 60_000,
  expect: { timeout: 15_000 },
  workers: 1,
  reporter: [["list"]],
  use: {
    ...devices["Desktop Chrome"],
    baseURL: "http://127.0.0.1:5187",
    serviceWorkers: "block",
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
  },
  webServer: [
    {
      command:
        ".venv/bin/python -m uvicorn e2e_executable_app:app --app-dir tests --host 127.0.0.1 --port 8002",
      cwd: "../backend",
      url: "http://127.0.0.1:8002/",
      reuseExistingServer: false,
      env: {
        PYTHONPATH: ".:../services/shared",
        ENVIRONMENT: "test",
        JWT_SECRET_KEY: randomBytes(32).toString("hex"),
        REFRESH_TOKEN_HASH_SECRET: randomBytes(32).toString("hex"),
      },
    },
    {
      command: "node_modules/.bin/vite --host 127.0.0.1 --port 5187 --strictPort",
      url: "http://127.0.0.1:5187",
      reuseExistingServer: false,
      env: { VITE_API_URL: "" },
    },
  ],
});
