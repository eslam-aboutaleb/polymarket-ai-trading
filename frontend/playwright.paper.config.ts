import { defineConfig, devices } from "@playwright/test";

export default defineConfig({
  testDir: "./e2e/paper",
  workers: 1,
  timeout: 60_000,
  expect: { timeout: 15_000 },
  reporter: "list",
  use: {
    ...devices["Desktop Chrome"],
    baseURL: "http://127.0.0.1:5188",
    serviceWorkers: "block",
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
  },
  webServer: [
    {
      command:
        ".venv/bin/python -m uvicorn e2e_paper_app:app --app-dir tests --host 127.0.0.1 --port 8002",
      cwd: "../backend",
      url: "http://127.0.0.1:8002/",
      reuseExistingServer: false,
      env: {
        PYTHONPATH: ".:../services/shared",
        ENVIRONMENT: "test",
        JWT_SECRET_KEY: "paper-e2e-disposable-signing-secret-2026",
        REFRESH_TOKEN_HASH_SECRET: "paper-e2e-disposable-refresh-secret-2026",
      },
    },
    {
      command: "node_modules/.bin/vite --host 127.0.0.1 --port 5188 --strictPort",
      url: "http://127.0.0.1:5188",
      reuseExistingServer: false,
      env: { VITE_API_URL: "" },
    },
  ],
});
