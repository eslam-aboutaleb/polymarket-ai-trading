import { apiClient } from "./apiClient";

// ── Types ──

export type BacktestStrategyType = "copy_trade" | "indicator" | "custom";
export type BacktestStatus = "pending" | "running" | "completed" | "failed";

export interface BacktestRun {
  id: number;
  strategy_type: BacktestStrategyType;
  strategy_name: string;
  start_date: string;
  end_date: string;
  parameters: Record<string, unknown>;
  total_trades: number;
  winning_trades: number;
  losing_trades: number;
  win_rate: number | null;
  total_pnl: number;
  max_drawdown: number | null;
  sharpe_ratio: number | null;
  profit_factor: number | null;
  avg_trade_pnl: number | null;
  max_consecutive_losses: number | null;
  total_volume: number | null;
  status: BacktestStatus;
  error_message: string | null;
  trade_log: unknown[] | null;
  indicator_values: Record<string, unknown> | null;
  created_at: string;
  completed_at: string | null;
}

export interface BacktestListResponse {
  runs: BacktestRun[];
  total: number;
}

export interface CreateBacktestRequest {
  strategy_type: BacktestStrategyType;
  strategy_name?: string;
  start_date: string;
  end_date: string;
  parameters: Record<string, unknown>;
}

export interface StrategyInfo {
  type: string;
  name: string;
  description: string;
  parameters: Record<string, string>;
}

export interface StrategiesResponse {
  strategies: StrategyInfo[];
}

// ── API Functions ──

export async function listBacktestRuns(
  limit?: number,
): Promise<BacktestListResponse> {
  const params = limit ? `?limit=${limit}` : "";
  return apiClient.get<BacktestListResponse>(`/api/backtesting/runs${params}`);
}

export async function getBacktestRun(runId: number): Promise<BacktestRun> {
  return apiClient.get<BacktestRun>(`/api/backtesting/runs/${runId}`);
}

export async function createBacktestRun(
  body: CreateBacktestRequest,
): Promise<BacktestRun> {
  return apiClient.post<BacktestRun>("/api/backtesting/runs", body);
}

export async function deleteBacktestRun(runId: number): Promise<void> {
  await apiClient.delete(`/api/backtesting/runs/${runId}`);
}

export async function listStrategies(): Promise<StrategiesResponse> {
  return apiClient.get<StrategiesResponse>("/api/backtesting/strategies");
}

export const backtestingService = {
  listBacktestRuns,
  getBacktestRun,
  createBacktestRun,
  deleteBacktestRun,
  listStrategies,
};
