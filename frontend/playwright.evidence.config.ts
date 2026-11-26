import { defineConfig } from "@playwright/test";

// Pure logic and server-rendered component tests. No browser, no network.
export default defineConfig({
  testDir: "./e2e/evidence",
  timeout: 30_000,
  workers: 1,
  reporter: [["list"]],
});
