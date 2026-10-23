import { defineConfig } from "@playwright/test";
import research from "./playwright.research.config";

// Separate from CI regression tests: this opt-in run writes tracked README assets.
export default defineConfig({
  ...research,
  testDir: "./scripts",
  testMatch: ["capture-user-guide.spec.ts", "capture-app-guide.spec.ts"],
  timeout: 120_000,
  use: { ...research.use, viewport: { width: 1440, height: 1080 } },
});
