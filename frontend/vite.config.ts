import { defineConfig, loadEnv } from "vite";
import react from "@vitejs/plugin-react";
import path from "path";

// https://vitejs.dev/config/
export default defineConfig(({ command, mode }) => {
  const env = loadEnv(mode, process.cwd(), "");
  const isDevServer = command === "serve";
  // Resolve the API origin the same way Vite resolves import.meta.env:
  // process.env (Docker ARG/ENV, CI) wins over .env files. Using loadEnv() here
  // would read the local .env and disagree with the value baked into the bundle.
  const configuredApi = (process.env.VITE_API_URL ?? env.VITE_API_URL ?? "").trim();
  // Only allow-list the configured origin when one is actually set. With an
  // empty VITE_API_URL the app talks to its own origin via nginx, so connect-src
  // must be exactly 'self' — never a hardcoded localhost fallback.
  const cspConnectSrc =
    configuredApi && /^https?:\/\//.test(configuredApi)
      ? ["'self'", configuredApi].join(" ")
      : "'self'";
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
    plugins: [
      react(),
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
