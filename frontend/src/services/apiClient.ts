/**
 * Shared axios instance that unwraps response bodies and transparently refreshes expired sessions.
 *
 * Requests send cookies (`withCredentials`) against `API_BASE_URL` and carry the active-account
 * header chosen in the account switcher. A request interceptor injects that header once, so call
 * sites no longer spread `getActiveAccountHeader()` into every config.
 *
 * A response interceptor catches 401s, retries once via `POST /api/auth/refresh`, and replays the
 * original request; if the refresh fails it clears the auth store and rejects. Requests that are
 * themselves the refresh call are never retried.
 *
 * Every rejection is normalized to `ApiError` (see `utils/apiError`), so consumers pattern-match on
 * `status` / `code` / `message` instead of the axios error shape. `refreshSession` exposes the
 * single-flight refresh to transports that are not axios — the SSE streams in
 * `services/sseStream`.
 *
 * @module services/apiClient
 */

import axios, {
  AxiosError,
  AxiosInstance,
  AxiosRequestConfig,
  AxiosResponse,
  InternalAxiosRequestConfig,
} from "axios";
import { useAuthStore } from "../store/authStore";
import { API_BASE_URL } from "../config/api";
import { ApiError, describeApiError, isApiError } from "../utils/apiError";
import { getActiveAccountHeader } from "./accountService";

// In-flight refresh request. Concurrent 401s await this single
// promise instead of each firing POST /api/auth/refresh: the
// first refresh rotates the shared refresh token, so a second
// parallel call would present the already-revoked token, get a
// 401, and clear the session mid-flight (random logouts).
// Reset in `finally` once the refresh settles.
let refreshPromise: Promise<unknown> | null = null;

/** Copy the active-account header onto an outgoing request config. */
function withActiveAccount(config: InternalAxiosRequestConfig): InternalAxiosRequestConfig {
  const accountHeaders = getActiveAccountHeader();
  if (Object.keys(accountHeaders).length === 0) return config;

  if (config.headers && typeof config.headers.set === "function") {
    for (const [key, value] of Object.entries(accountHeaders)) {
      if (value !== undefined) {
        config.headers.set(key, value);
      }
    }
  } else {
    config.headers = {
      ...(config.headers as Record<string, string> | undefined),
      ...accountHeaders,
    } as unknown as typeof config.headers;
  }
  return config;
}

/**
 * Map a raw axios failure onto the normalized `ApiError` the gateway rejects with.
 *
 * Transport failures (no response) keep their code and message so `ERR_NETWORK` copy survives;
 * HTTP failures keep the flattened payload message plus `status` for callers that branch on it.
 */
function toApiError(error: unknown): ApiError {
  if (isApiError(error)) return error;

  const axiosError = axios.isAxiosError(error) ? error : undefined;
  const response = axiosError?.response;
  const retryAfterHeader = response?.headers?.["retry-after"];
  const retryAfterRaw = Array.isArray(retryAfterHeader) ? retryAfterHeader[0] : retryAfterHeader;
  const retryAfterValue = Number(retryAfterRaw);
  const details = {
    status: response?.status,
    code: axiosError?.code,
    data: response?.data,
    retryAfter: Number.isFinite(retryAfterValue) ? retryAfterValue : null,
  };
  const fallback = axiosError?.message || (error instanceof Error ? error.message : undefined);

  return new ApiError(describeApiError(details, fallback), details);
}

class ApiClient {
  private client: AxiosInstance;

  constructor() {
    this.client = axios.create({
      baseURL: API_BASE_URL,
      withCredentials: true,
      headers: {
        "Content-Type": "application/json",
      },
    });

    // Send the account the user is currently acting as on every request.
    this.client.interceptors.request.use(withActiveAccount);

    // Add response interceptor to handle cookie-based token refresh.
    this.client.interceptors.response.use(
      (response: AxiosResponse) => response,
      async (error: AxiosError) => {
        const originalRequest = error.config as
          (AxiosError["config"] & { _retry?: boolean }) | undefined;

        if (!originalRequest) {
          return Promise.reject(toApiError(error));
        }

        const requestUrl = originalRequest.url || "";
        const isRefreshCall = requestUrl.includes("/api/auth/refresh");

        if (error.response?.status === 401 && !originalRequest._retry && !isRefreshCall) {
          originalRequest._retry = true;

          try {
            if (!refreshPromise) {
              refreshPromise = this.post("/api/auth/refresh", {}).finally(() => {
                refreshPromise = null;
              });
            }
            await refreshPromise;
            return this.client(originalRequest);
          } catch (refreshError) {
            useAuthStore.getState().clearSession();
            return Promise.reject(toApiError(refreshError));
          }
        }

        return Promise.reject(toApiError(error));
      },
    );
  }

  /**
   * Refresh the session, deduplicating concurrent callers onto one request.
   *
   * Shared by the 401 retry above and by `refreshSession`, which non-axios transports call.
   */
  async refreshTokens(): Promise<unknown> {
    if (!refreshPromise) {
      refreshPromise = this.post("/api/auth/refresh", {}).finally(() => {
        refreshPromise = null;
      });
    }
    return refreshPromise;
  }

  async get<T>(url: string, config?: AxiosRequestConfig): Promise<T> {
    const response = await this.client.get<T>(url, config);
    return response.data;
  }

  async post<T>(url: string, data?: any, config?: AxiosRequestConfig): Promise<T> {
    const response = await this.client.post<T>(url, data, config);
    return response.data;
  }

  async put<T>(url: string, data?: any, config?: AxiosRequestConfig): Promise<T> {
    const response = await this.client.put<T>(url, data, config);
    return response.data;
  }

  async patch<T>(url: string, data?: any, config?: AxiosRequestConfig): Promise<T> {
    const response = await this.client.patch<T>(url, data, config);
    return response.data;
  }

  async delete<T>(url: string, config?: AxiosRequestConfig): Promise<T> {
    const response = await this.client.delete<T>(url, config);
    return response.data;
  }
}

export const apiClient = new ApiClient();

/**
 * Refresh the session using the same single-flight refresh as the response interceptor.
 *
 * Resolves once a rotated session is in place; rejects (after clearing the session) when the
 * refresh call itself fails, so a stream caller can surface the 401 it already received.
 */
export async function refreshSession(): Promise<unknown> {
  try {
    return await apiClient.refreshTokens();
  } catch (error) {
    useAuthStore.getState().clearSession();
    throw error;
  }
}
