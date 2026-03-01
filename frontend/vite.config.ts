import { defineConfig, loadEnv } from "vite";
import react from "@vitejs/plugin-react";
import path from "path";

// https://vitejs.dev/config/
export default defineConfig(({ command, mode }) => {
  const env = loadEnv(mode, process.cwd(), "");
  const isDevServer = command === "serve";
  const configuredApi = (env.VITE_API_URL || "").trim();
  const cspConnectSrc = ["'self'", configuredApi || "http://localhost:8000"].join(
    " ",
  );
  const productionCsp = [
    "default-src 'self'",
    "script-src 'self'",
    "style-src 'self' 'unsafe-inline'",
    "img-src 'self' data: https:",
    "font-src 'self' data:",
    `connect-src ${cspConnectSrc}`,
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
              target: "http://localhost:8000",
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
