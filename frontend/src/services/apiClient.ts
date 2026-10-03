/**
 * Shared axios instance that unwraps response bodies and transparently refreshes expired sessions.
 *
 * Requests send cookies (`withCredentials`) against `API_BASE_URL`. A response interceptor catches
 * 401s, retries once via `POST /api/auth/refresh`, and replays the original request; if the refresh
 * fails it clears the auth store and rejects. Requests that are themselves the refresh call are
 * never retried.
 *
 * @module services/apiClient
 */

import axios, { AxiosInstance, AxiosError, AxiosResponse } from "axios";
import { useAuthStore } from "../store/authStore";
import { API_BASE_URL } from "../config/api";

// In-flight refresh request. Concurrent 401s await this single
// promise instead of each firing POST /api/auth/refresh: the
// first refresh rotates the shared refresh token, so a second
// parallel call would present the already-revoked token, get a
// 401, and clear the session mid-flight (random logouts).
// Reset in `finally` once the refresh settles.
let refreshPromise: Promise<unknown> | null = null;

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

    // Add response interceptor to handle cookie-based token refresh.
    this.client.interceptors.response.use(
      (response: AxiosResponse) => response,
      async (error: AxiosError) => {
        const originalRequest = error.config as
          (AxiosError["config"] & { _retry?: boolean }) | undefined;

        if (!originalRequest) {
          return Promise.reject(error);
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
            return Promise.reject(refreshError);
          }
        }

        return Promise.reject(error);
      },
    );
  }

  async get<T>(url: string, config?: any): Promise<T> {
    const response = await this.client.get<T>(url, config);
    return response.data;
  }

  async post<T>(url: string, data?: any, config?: any): Promise<T> {
    const response = await this.client.post<T>(url, data, config);
    return response.data;
  }

  async put<T>(url: string, data?: any, config?: any): Promise<T> {
    const response = await this.client.put<T>(url, data, config);
    return response.data;
  }

  async patch<T>(url: string, data?: any, config?: any): Promise<T> {
    const response = await this.client.patch<T>(url, data, config);
    return response.data;
  }

  async delete<T>(url: string, config?: any): Promise<T> {
    const response = await this.client.delete<T>(url, config);
    return response.data;
  }
}

export const apiClient = new ApiClient();
