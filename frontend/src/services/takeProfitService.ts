/**
 * Take-profit order endpoints under `/api/trades/take-profit`.
 *
 * Creating a take profit upserts for the position token, listing defaults to `active` orders but
 * accepts `triggered`/`cancelled`/`failed`/`all`, and cancelling deletes the record. Orders are
 * filled by a backend watcher rather than the browser.
 *
 * @module services/takeProfitService
 */

import { apiClient } from "./apiClient";

// --- Types ---

export interface TakeProfitOrder {
  id: number;
  token_id: string;
  market_id: string;
  market_title: string;
  outcome: string;
  size: number;
  take_profit_price: number;
  status: "active" | "triggered" | "cancelled" | "failed";
  order_hash?: string | null;
  executed_price?: number | null;
  triggered_at?: string | null;
  created_at: string;
  updated_at: string;
}

export interface SetTakeProfitRequest {
  token_id: string;
  market_id?: string;
  market_title?: string;
  outcome?: string;
  size: number;
  take_profit_price: number;
}

// --- Service ---

export const takeProfitService = {
  /** Create or update a take-profit order for a position. */
  async setTakeProfit(req: SetTakeProfitRequest): Promise<TakeProfitOrder> {
    return apiClient.post<TakeProfitOrder>("/api/trades/take-profit", req);
  },

  /** List take-profit orders. Default: only active ones. */
  async getTakeProfits(
    status: "active" | "triggered" | "cancelled" | "failed" | "all" = "active",
  ): Promise<TakeProfitOrder[]> {
    return apiClient.get<TakeProfitOrder[]>(`/api/trades/take-profit?status=${status}`);
  },

  /** Cancel an active take-profit order. */
  async cancelTakeProfit(id: number): Promise<{ status: string; id: number }> {
    return apiClient.delete(`/api/trades/take-profit/${id}`);
  },
};
