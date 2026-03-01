import { apiClient } from "./apiClient";

// --- Types ---

export interface StopLossOrder {
  id: number;
  token_id: string;
  market_id: string;
  market_title: string;
  outcome: string;
  size: number;
  stop_price: number;
  status: "active" | "triggered" | "cancelled" | "failed";
  order_hash?: string | null;
  executed_price?: number | null;
  triggered_at?: string | null;
  created_at: string;
  updated_at: string;
}

export interface SetStopLossRequest {
  token_id: string;
  market_id?: string;
  market_title?: string;
  outcome?: string;
  size: number;
  stop_price: number;
}

// --- Service ---

export const stopLossService = {
  /** Create or update a stop-loss order for a position. */
  async setStopLoss(req: SetStopLossRequest): Promise<StopLossOrder> {
    return apiClient.post<StopLossOrder>("/api/trades/stop-loss", req);
  },

  /** List stop-loss orders. Default: only active ones. */
  async getStopLosses(
    status: "active" | "triggered" | "cancelled" | "failed" | "all" = "active",
  ): Promise<StopLossOrder[]> {
    return apiClient.get<StopLossOrder[]>(
      `/api/trades/stop-loss?status=${status}`,
    );
  },

  /** Cancel an active stop-loss order. */
  async cancelStopLoss(id: number): Promise<{ status: string; id: number }> {
    return apiClient.delete(`/api/trades/stop-loss/${id}`);
  },
};
