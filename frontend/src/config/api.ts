// API origin for the browser client.
//
// Two supported modes:
//   - Empty (the default for production): same-origin. Requests use relative
//     `/api/...` paths that nginx proxies to the backend, so no CORS and no
//     cross-origin credential handling are involved.
//   - Absolute URL: used by `npm run dev`, where Vite proxies `/api`.
//
// Service methods always pass paths beginning with `/api`, so the base URL must
// be an origin only (or empty) — never a path, or requests become `/api/api/...`.
const configuredApiUrl = (import.meta.env.VITE_API_URL || "").trim();

// A plaintext origin in a production bundle would send session cookies and the
// login request body over cleartext HTTP. Fail the build loudly instead.
if (import.meta.env.PROD && configuredApiUrl.startsWith("http://")) {
  throw new Error(
    "VITE_API_URL must use https in production, or be empty to use same-origin requests.",
  );
}

export const API_BASE_URL = configuredApiUrl;
