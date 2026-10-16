/**
 * ESLint configuration for the frontend (eslint 8, eslintrc format).
 *
 * Beyond `@typescript-eslint`'s recommended set, three rules encode the
 * gateway boundaries:
 *
 * 1. `src/components/**` and `src/pages/**` must not talk to the transport
 *    directly — no `axios`, no bare `fetch(` — because every request has to
 *    carry the session cookie, the active-account header and the single-flight
 *    refresh. Wallet SDK imports (and the wallet connector itself) are exempt:
 *    they talk to the wallet, not to this API.
 * 2. `services/apiClient` may only be imported from inside `src/services/**`.
 *    Pages and components consume a service module, never the client.
 *
 * Zero warnings is a build gate (`npm run lint` uses `--max-warnings 0`), so
 * keep new code warning-clean rather than reaching for a disable comment.
 */

module.exports = {
  root: true,
  env: {
    browser: true,
    es2021: true,
  },
  parser: "@typescript-eslint/parser",
  parserOptions: {
    ecmaVersion: "latest",
    sourceType: "module",
    ecmaFeatures: { jsx: true },
  },
  plugins: ["@typescript-eslint", "react-hooks"],
  extends: ["eslint:recommended", "plugin:@typescript-eslint/recommended"],
  ignorePatterns: ["dist", "node_modules", "test-results", "playwright-report", "coverage", "*.cjs"],
  rules: {
    "react-hooks/rules-of-hooks": "error",
    "react-hooks/exhaustive-deps": "warn",
    // `any` is used deliberately at the gateway's generic request/response
    // boundary; the rest of the tree is typed through the service modules.
    "@typescript-eslint/no-explicit-any": "off",
  },
  overrides: [
    // Rule 2: the shared client is a services-only dependency.
    {
      files: ["src/**/*.{js,jsx,ts,tsx}"],
      excludedFiles: ["src/services/**/*.{js,jsx,ts,tsx}"],
      rules: {
        "no-restricted-imports": [
          "error",
          {
            patterns: [
              {
                group: ["*/services/apiClient", "services/apiClient", "**/services/apiClient"],
                message:
                  "Import the service module instead of `services/apiClient`: the client is the transport boundary owned by `src/services`.",
              },
            ],
          },
        ],
      },
    },
    // Rule 1: no direct transport calls from UI code, wallet SDKs exempt.
    {
      files: ["src/components/**/*.{js,jsx,ts,tsx}", "src/pages/**/*.{js,jsx,ts,tsx}"],
      excludedFiles: [
        "src/components/**/walletConnector*",
        "src/components/**/wallet/**",
        "src/pages/**/wallet/**",
      ],
      rules: {
        "no-restricted-imports": [
          "error",
          {
            paths: [
              {
                name: "axios",
                message:
                  "Call a service module in `src/services` instead: it sends the session cookie, the active-account header and refreshes on 401.",
              },
            ],
            patterns: [
              {
                group: ["axios", "axios/*"],
                message:
                  "Call a service module in `src/services` instead: it sends the session cookie, the active-account header and refreshes on 401.",
              },
            ],
          },
        ],
        // Ban bare fetch() calls; wallet SDK usage imports stay allowed.
        "no-restricted-syntax": [
          "error",
          {
            selector: "CallExpression[callee.type='Identifier'][callee.name='fetch']",
            message:
              "Use the service layer (or `fetchStream` from `src/services/sseStream` for streams): raw fetch() skips the session cookie, the active-account header and the single-flight refresh.",
          },
        ],
      },
    },
  ],
};
