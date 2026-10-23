import { defineConfig, devices } from "@playwright/test";

const live = process.env.WALLETCONNECT_LIVE_CANARY === "1";
const projectId = process.env.VITE_WALLETCONNECT_PROJECT_ID?.trim();
if (live && !/^[a-f0-9]{32}$/i.test(projectId ?? "")) {
  throw new Error(
    "Set the repository secret VITE_WALLETCONNECT_PROJECT_ID before the live canary.",
  );
}

export default defineConfig({
  testDir: "./e2e/walletconnect",
  workers: 1,
  timeout: 90_000,
  expect: { timeout: 20_000 },
  reporter: "list",
  use: { ...devices["Desktop Chrome"], trace: "retain-on-failure", screenshot: "only-on-failure" },
  projects: [
    {
      name: "configured",
      testMatch: live ? "live.spec.ts" : "configured.spec.ts",
      use: { baseURL: "http://127.0.0.1:5190" },
    },
    ...(!live
      ? [
          {
            name: "missing",
            testMatch: "missing.spec.ts",
            use: { baseURL: "http://127.0.0.1:5189" },
          },
        ]
      : []),
  ],
  webServer: [
    {
      command: live
        ? "npm run build && node_modules/.bin/vite preview --host 127.0.0.1 --port 5190 --strictPort"
        : "node_modules/.bin/vite --host 127.0.0.1 --port 5190 --strictPort",
      timeout: 180_000,
      url: "http://127.0.0.1:5190",
      reuseExistingServer: false,
      env: {
        VITE_API_URL: "",
        VITE_WALLETCONNECT_PROJECT_ID: projectId || "0123456789abcdef0123456789abcdef",
      },
    },
    ...(!live
      ? [
          {
            command: "node_modules/.bin/vite --host 127.0.0.1 --port 5189 --strictPort",
            url: "http://127.0.0.1:5189",
            reuseExistingServer: false,
            env: { VITE_API_URL: "", VITE_WALLETCONNECT_PROJECT_ID: "" },
          },
        ]
      : []),
  ],
});
