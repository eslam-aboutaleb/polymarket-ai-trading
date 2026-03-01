const DEV_DEFAULT_API_URL = "http://localhost:8000";

const configuredApiUrl = (import.meta.env.VITE_API_URL || "").trim();

if (import.meta.env.PROD && configuredApiUrl.length === 0) {
  throw new Error("VITE_API_URL must be set for production builds.");
}

export const API_BASE_URL =
  configuredApiUrl.length > 0 ? configuredApiUrl : DEV_DEFAULT_API_URL;
