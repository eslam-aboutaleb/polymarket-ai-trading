import { defineConfig, devices } from "@playwright/test";

/** Backend-free config for the Tailwind v4 CSS pipeline regression (z-layers, overlays). */
export default defineConfig({
  testDir: "./e2e/tailwind",
  timeout: 30_000,
  workers: 1,
  use: {
    ...devices["Desktop Chrome"],
    baseURL: "http://127.0.0.1:5187",
    serviceWorkers: "block",
    trace: "retain-on-failure",
  },
  projects: [
    { name: "desktop", use: { viewport: { width: 1280, height: 800 } } },
    { name: "mobile", use: { ...devices["Pixel 7"] } },
  ],
  webServer: {
    command: "node_modules/.bin/vite --host 127.0.0.1 --port 5187 --strictPort",
    url: "http://127.0.0.1:5187",
    reuseExistingServer: !process.env.CI,
  },
});
