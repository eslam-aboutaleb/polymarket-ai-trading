/**
 * On-chain whale monitoring endpoints under `/api/whales`.
 *
 * The feed lists whale-sized CTF position movements detected
 * directly on Polygon (seconds after confirmation), plus the
 * per-user thresholds, watchlist and auto-copy toggle.
 *
 * @module services/whaleService
 */

import { apiClient } from "./apiClient";

// ── Types ───────────────────────────────────────────────────

export interface WhaleEvent {
  id: number;
  wallet: string;
  market_id: string;
  token_id: string;
  event_type: string;
  side: string;
  size: number;
  price: number;
  notional: number;
  tx_hash: string;
  log_index: number | null;
  block_number: number | null;
  block_ts: string | null;
  detected_at: string;
}

export interface WhaleEventsResponse {
  events: WhaleEvent[];
  total: number;
  limit: number;
  offset: number;
}

export interface WhaleConfig {
  min_notional: number;
  auto_copy: boolean;
  watchlist: string[];
  whale_set_size: number;
}

export interface WhaleEventQuery {
  limit?: number;
  offset?: number;
  wallet?: string;
  market_id?: string;
  side?: string;
  event_type?: string;
}

// ── Service ─────────────────────────────────────────────────

export const whaleService = {
  async listEvents(query: WhaleEventQuery = {}): Promise<WhaleEventsResponse> {
    return apiClient.get<WhaleEventsResponse>("/api/whales/events", {
      params: query,
    });
  },

  async getConfig(): Promise<WhaleConfig> {
    return apiClient.get<WhaleConfig>("/api/whales/config");
  },

  async updateConfig(config: Partial<WhaleConfig>): Promise<WhaleConfig> {
    return apiClient.put<WhaleConfig>("/api/whales/config", config);
  },
};
