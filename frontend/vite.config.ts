import { defineConfig, loadEnv } from "vite";
import react from "@vitejs/plugin-react";
import { VitePWA } from "vite-plugin-pwa";
import path from "path";

// The loopback origin the app documents for local development only (see
// frontend/.env.example). Vite resolves `import.meta.env` from .env files, so a
// local dev default living there must be recognised here — otherwise a plain
// `npm run build` bakes `http://localhost:8000` into the bundle and CSP.
const DEV_LOOPBACK_ORIGIN = /^http:\/\/(localhost|127(?:\.\d{1,3}){3}|\[::1\])(?::\d+)?(?:\/|$)/i;

// https://vitejs.dev/config/
export default defineConfig(({ command, mode }) => {
  const env = loadEnv(mode, process.cwd(), "");
  const isDevServer = command === "serve";
  // Resolve the API origin the same way Vite resolves import.meta.env:
  // process.env (Docker ARG/ENV, CI) wins over .env files. Using loadEnv() here
  // would read the local .env and disagree with the value baked into the bundle.
  const hasExplicitApiUrl = process.env.VITE_API_URL !== undefined;
  const configuredApi = (process.env.VITE_API_URL ?? env.VITE_API_URL ?? "").trim();
  // The origin that is actually baked into the bundle and the CSP meta tag.
  let bakedApiUrl = configuredApi;
  if (!isDevServer) {
    if (!hasExplicitApiUrl && bakedApiUrl && DEV_LOOPBACK_ORIGIN.test(bakedApiUrl)) {
      // Decision: an .env-sourced loopback dev default is a local-dev
      // convenience, never a production origin. Production talks same-origin
      // through nginx, so convert it to "" (empty = same-origin) instead of
      // baking `http://localhost:8000` into the JS bundle and the CSP meta tag.
      bakedApiUrl = "";
    } else if (bakedApiUrl.startsWith("http://")) {
      throw new Error(
        `VITE_API_URL is "${bakedApiUrl}" — insecure http:// is not allowed in a production build. ` +
          `Use an https:// origin, or leave VITE_API_URL empty to use same-origin /api requests via nginx.`,
      );
    }
  }
  if (isDevServer && bakedApiUrl) {
    // Dev-only visibility of the origin the client will use.
    console.warn(`[vite.config] using VITE_API_URL: ${bakedApiUrl}`);
  }
  // Only allow-list the configured origin when one is actually set. With an
  // empty VITE_API_URL the app talks to its own origin via nginx, so connect-src
  // must be exactly 'self' — never a hardcoded localhost fallback.
  const cspConnectSrc =
    bakedApiUrl && /^https?:\/\//.test(bakedApiUrl) ? ["'self'", bakedApiUrl].join(" ") : "'self'";
  const productionCsp = [
    "default-src 'self'",
    "script-src 'self'",
    "style-src 'self' 'unsafe-inline'",
    "img-src 'self' data: https:",
    "font-src 'self' data:",
    `connect-src ${cspConnectSrc}`,
    "object-src 'none'",
    // frame-ancestors is ignored when delivered via <meta>; nginx.conf sets
    // X-Frame-Options: DENY. Keep it here for documentation value only.
    "frame-ancestors 'none'",
    "base-uri 'self'",
    "form-action 'self'",
  ].join("; ");

  return {
    // Pin the resolved origin in the bundle. Vite's own import.meta.env
    // replacement only reads .env files, so without this define a loopback
    // dev default from .env would reach production output.
    define: {
      "import.meta.env.VITE_API_URL": JSON.stringify(bakedApiUrl),
    },
    plugins: [
      react(),
      VitePWA({
        registerType: "autoUpdate",
        // External registration script: an inline script would be blocked by
        // the CSP `script-src 'self'` meta tag injected below.
        injectRegister: "script",
        // The manifest is hand-written so `public/manifest.json` applies,
        // and `index.html` links it via <link rel="manifest">.
        manifest: false,
        devOptions: {
          // Service worker stays off during `npm run dev`.
          enabled: false,
        },
        includeAssets: [
          "favicon.ico",
          "favicon-16x16.png",
          "favicon-32x32.png",
          "apple-touch-icon.png",
        ],
        workbox: {
          globPatterns: ["**/*.{js,css,html,ico,png,svg,woff2}"],
          runtimeCaching: [
            {
              // In production the app calls its own origin (`/api/...`, proxied
              // by nginx), so this must match relative URLs too — an
              // https-only regex would never hit.
              urlPattern: /\/api\//,
              handler: "NetworkFirst",
              options: {
                cacheName: "api-cache",
                expiration: {
                  maxEntries: 50,
                  maxAgeSeconds: 300,
                },
                cacheableResponse: {
                  statuses: [0, 200],
                },
              },
            },
          ],
        },
      }),
      {
        name: "inject-production-csp",
        transformIndexHtml(html) {
          if (isDevServer) return html;
          const meta = `    <meta http-equiv="Content-Security-Policy" content="${productionCsp}" />`;
          return html.replace("</head>", `${meta}\n  </head>`);
        },
      },
    ],
    server: isDevServer
      ? {
          port: 5173,
          proxy: {
            "/api": {
              target: "http://localhost:8002",
              changeOrigin: true,
            },
          },
        }
      : undefined,
    resolve: {
      alias: {
        "@": path.resolve(__dirname, "./src"),
      },
    },
  };
});
