import { defineConfig, loadEnv } from "vite";
import react from "@vitejs/plugin-react";
import path from "path";
// https://vitejs.dev/config/
export default defineConfig(function (_a) {
    var command = _a.command, mode = _a.mode;
    var env = loadEnv(mode, process.cwd(), "");
    var isDevServer = command === "serve";
    var configuredApi = (env.VITE_API_URL || "").trim();
    var cspConnectSrc = ["'self'", configuredApi || "http://localhost:8000"].join(" ");
    var productionCsp = [
        "default-src 'self'",
        "script-src 'self'",
        "style-src 'self' 'unsafe-inline'",
        "img-src 'self' data: https:",
        "font-src 'self' data:",
        "connect-src ".concat(cspConnectSrc),
        "frame-ancestors 'none'",
        "base-uri 'self'",
        "form-action 'self'",
    ].join("; ");
    return {
        plugins: [
            react(),
            {
                name: "inject-production-csp",
                transformIndexHtml: function (html) {
                    if (isDevServer)
                        return html;
                    var meta = "    <meta http-equiv=\"Content-Security-Policy\" content=\"".concat(productionCsp, "\" />");
                    return html.replace("</head>", "".concat(meta, "\n  </head>"));
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
