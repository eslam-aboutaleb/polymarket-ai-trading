import { defineConfig, devices } from "@playwright/test";
import path from "node:path";
import { fileURLToPath } from "node:url";

/**
 * Playwright configuration for the Polymarket app e2e suite.
 *
 * The suite exercises the real stack: Vite dev server (port 5174) proxying
 * `/api` to the FastAPI backend (port 8002). Both servers are reused when
 * already running (`reuseExistingServer`), so a local dev loop is not
 * disturbed.
 *
 * Authentication uses wallet-signature login against real test wallets
 * (well-known Hardhat keys). The signed-in session is stored once per role
 * in `e2e/.auth/*.json` by the global setup and reused by every test, which
 * keeps the backend's per-endpoint login rate limits out of the way.
 */

const HERE = path.dirname(fileURLToPath(import.meta.url));
const REPO_ROOT = path.resolve(HERE, "..");
const E2E_DIR = path.resolve(HERE, "e2e");

// The `npm`/`npx` on PATH when Playwright launches is the same node we run
// under (process.execPath), so exporting its directory keeps `npm run dev`
// working for the frontend webServer even when the login shell would have
// injected nvm's shims.
const NODE_BIN_DIR = path.dirname(process.execPath);

export default defineConfig({
  testDir: E2E_DIR,
  timeout: 90_000,
  expect: { timeout: 15_000 },
  fullyParallel: false,
  workers: 1,
  retries: 0,
  reporter: [["list"]],
  globalSetup: path.resolve(E2E_DIR, "global-setup.ts"),

  use: {
    baseURL: "http://localhost:5174",
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
    video: "off",
  },

  projects: [
    {
      name: "public",
      testMatch: /public\/.*\.spec\.ts/,
      use: { ...devices["Desktop Chrome"] },
    },
    {
      name: "auth",
      testMatch: /auth\/.*\.spec\.ts/,
      use: { ...devices["Desktop Chrome"] },
    },
    {
      name: "features",
      testMatch: /features\/.*\.spec\.ts/,
      use: { ...devices["Desktop Chrome"] },
      dependencies: ["auth"],
    },
  ],

  webServer: [
    {
      command: "backend/.venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8002",
      cwd: path.join(REPO_ROOT, "backend"),
      url: "http://127.0.0.1:8002/",
      reuseExistingServer: true,
      timeout: 120_000,
      env: {
        PYTHONPATH:
          path.join(REPO_ROOT, "backend") +
          path.delimiter +
          path.join(REPO_ROOT, "services", "shared"),
      },
    },
    {
      command: "node_modules/.bin/vite --port 5174 --strictPort",
      cwd: path.join(REPO_ROOT, "frontend"),
      url: "http://localhost:5174/",
      reuseExistingServer: true,
      timeout: 120_000,
      env: {
        PATH: NODE_BIN_DIR + path.delimiter + (process.env.PATH ?? ""),
      },
    },
  ],
});
