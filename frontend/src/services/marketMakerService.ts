/**
 * Market-making bot configuration endpoints under `/api/market-maker`.
 *
 * CRUD over per-market maker configs (bands or AMM strategy, spread bands, sizing, collateral caps),
 * plus start/stop/sync controls and a metrics summary of which configs are currently running.
 * Configs hold cumulative order/volume counters and the last sync time and error.
 *
 * @module services/marketMakerService
 */

import { apiClient } from "./apiClient";

// ── Types ──

export type MarketMakerStrategy = "bands" | "amm";
export type MarketMakerStatus = "idle" | "running" | "paused" | "error";

export interface MarketMakerConfig {
  id: number;
  condition_id: string;
  token_id_yes: string;
  token_id_no: string;
  market_title: string;
  enabled: boolean;
  strategy: MarketMakerStrategy;
  num_bands: number;
  min_spread: number;
  max_spread: number;
  band_order_size: number;
  amm_liquidity: number;
  max_collateral: number;
  sync_interval_seconds: number;
  min_order_size: number;
  min_price: number;
  max_price: number;
  status: MarketMakerStatus;
  last_sync_at: string | null;
  last_error: string | null;
  total_orders_placed: number;
  total_orders_cancelled: number;
  total_volume_usdc: number;
  current_open_orders: number;
  is_running: boolean;
  created_at: string;
  updated_at: string | null;
}

export interface UpsertMarketMakerConfigRequest {
  condition_id: string;
  token_id_yes: string;
  token_id_no: string;
  market_title?: string;
  enabled?: boolean;
  strategy?: MarketMakerStrategy;
  num_bands?: number;
  min_spread?: number;
  max_spread?: number;
  band_order_size?: number;
  amm_liquidity?: number;
  max_collateral?: number;
  sync_interval_seconds?: number;
  min_order_size?: number;
  min_price?: number;
  max_price?: number;
}

export interface MarketMakerSyncResponse {
  success: boolean;
  result?: Record<string, unknown>;
  detail?: string;
}

export interface MarketMakerMetrics {
  running_configs: number[];
  total_running: number;
}

// ── API Functions ──

export async function listMarketMakerConfigs(): Promise<MarketMakerConfig[]> {
  return apiClient.get<MarketMakerConfig[]>("/api/market-maker/configs");
}

export async function upsertMarketMakerConfig(
  body: UpsertMarketMakerConfigRequest,
): Promise<MarketMakerConfig> {
  return apiClient.post<MarketMakerConfig>("/api/market-maker/configs", body);
}

export async function deleteMarketMakerConfig(configId: number): Promise<void> {
  await apiClient.delete(`/api/market-maker/configs/${configId}`);
}

export async function startMarketMaker(configId: number): Promise<{ status: string }> {
  return apiClient.post<{ status: string }>(`/api/market-maker/configs/${configId}/start`, {});
}

export async function stopMarketMaker(configId: number): Promise<{ status: string }> {
  return apiClient.post<{ status: string }>(`/api/market-maker/configs/${configId}/stop`, {});
}

export async function syncMarketMaker(configId: number): Promise<MarketMakerSyncResponse> {
  return apiClient.post<MarketMakerSyncResponse>(`/api/market-maker/configs/${configId}/sync`, {});
}

export async function getMarketMakerMetrics(): Promise<MarketMakerMetrics> {
  return apiClient.get<MarketMakerMetrics>("/api/market-maker/metrics");
}

export const marketMakerService = {
  listMarketMakerConfigs,
  upsertMarketMakerConfig,
  deleteMarketMakerConfig,
  startMarketMaker,
  stopMarketMaker,
  syncMarketMaker,
  getMarketMakerMetrics,
};
