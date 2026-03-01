import { apiClient } from "./apiClient";

/* ───────── Types ───────── */

export interface LogEntry {
  request_id: string;
  timestamp: string;
  method: string;
  path: string;
  query: string;
  status_code: number;
  duration_ms: number;
  level: "info" | "warning" | "error";
  client_ip: string;
  user_agent: string;
  error_detail?: string;
}

export interface LogsResponse {
  total: number;
  filtered: number;
  entries: LogEntry[];
}

export interface EndpointStat {
  method: string;
  path: string;
  call_count: number;
  error_count: number;
  warning_count: number;
  avg_duration_ms: number;
  min_duration_ms: number;
  max_duration_ms: number;
  last_called: string;
  last_status: number;
}

export interface StatsResponse {
  total_requests: number;
  total_errors: number;
  total_warnings: number;
  error_rate: number;
  endpoints: EndpointStat[];
}

export interface ServiceHealth {
  name: string;
  status: "healthy" | "unhealthy" | "unknown" | "warning";
  latency_ms: number | null;
  detail: string;
}

export interface HealthResponse {
  uptime_seconds: number;
  uptime_human: string;
  server_time: string;
  python_version: string;
  services: ServiceHealth[];
}

/* ───────── API calls ───────── */

export const debugService = {
  async fetchLogs(filters?: {
    level?: string;
    path?: string;
    method?: string;
    limit?: number;
  }): Promise<LogsResponse> {
    const params = new URLSearchParams();
    if (filters?.level) params.set("level", filters.level);
    if (filters?.path) params.set("path", filters.path);
    if (filters?.method) params.set("method", filters.method);
    if (filters?.limit) params.set("limit", String(filters.limit));
    const qs = params.toString();
    return apiClient.get<LogsResponse>(`/api/debug/logs${qs ? `?${qs}` : ""}`);
  },

  async fetchStats(): Promise<StatsResponse> {
    return apiClient.get<StatsResponse>("/api/debug/stats");
  },

  async fetchHealth(): Promise<HealthResponse> {
    return apiClient.get<HealthResponse>("/api/debug/health");
  },

  async clearLogs(): Promise<{ cleared: number; message: string }> {
    return apiClient.delete<{ cleared: number; message: string }>(
      "/api/debug/logs",
    );
  },
};
