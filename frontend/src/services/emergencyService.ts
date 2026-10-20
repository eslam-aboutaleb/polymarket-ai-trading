/**
 * Trading kill-switch, arbitrage, and trader-scoring endpoints under `/api/trades`.
 *
 * `emergencyStop` persists a halt and tracks exchange cancellation verification;
 * `resumeTrading` requests recovery while preserving the halt. Also reads or recomputes
 * quality-score breakdowns for tracked traders.
 *
 * @module services/emergencyService
 */

import { apiClient } from "./apiClient";

// --- Types ---

export interface EmergencyStopResponse {
  operation_id: string;
  halted: boolean;
  halt_status: "HALTING" | "HALTED" | "RECOVERY_REQUIRED";
  operation_status: "PENDING" | "VERIFIED" | "RECOVERY_REQUIRED";
  open_orders_found: number | null;
  cancel_requests_submitted: number;
  orders_confirmed_cancelled: number;
  orders_still_open: number | null;
  positions_found: number | null;
  close_requests_submitted: number;
  positions_confirmed_closed: number;
  positions_partially_closed: number;
  unresolved_positions: number | null;
  reconciliation_status: string;
  errors: string[];
  message: string;
}

export interface ArbitrageOpportunity {
  type: "complement" | "spread";
  market_id: string;
  market_title: string;
  profit_pct?: number | null;
  spread_pct?: number | null;
  total_cost?: number | null;
  bid?: number | null;
  ask?: number | null;
  token_id?: string | null;
  outcome?: string | null;
  detected_at: string;
}

export interface TraderQuality {
  wallet_address: string;
  quality_score: number | null;
  consistency_score: number | null;
  risk_adjusted_score: number | null;
  activity_score: number | null;
  win_rate_score: number | null;
  quality_tier: string | null;
}

// --- Emergency & Arbitrage Service ---

export const emergencyService = {
  /** Persist a halt. Position exits require verified gateway authorization. */
  async emergencyStop(closePositions = false): Promise<EmergencyStopResponse> {
    return apiClient.post<EmergencyStopResponse>(
      `/api/trades/emergency-stop?close_positions=${closePositions}`,
    );
  },

  async haltStatus(operationId: string): Promise<EmergencyStopResponse> {
    return apiClient.get(`/api/trades/emergency-stop/${encodeURIComponent(operationId)}`);
  },

  async latestHalt(): Promise<EmergencyStopResponse | null> {
    return apiClient.get("/api/trades/emergency-stop");
  },

  /** Request recovery. This cannot automatically resume strategies. */
  async resumeTrading(): Promise<{ resumed: boolean; message: string }> {
    await apiClient.post("/api/v1/risk/recovery-request", {
      reason: "Account owner requested recovery review",
    });
    return {
      resumed: false,
      message: "Recovery requested. Trading remains halted pending verification.",
    };
  },

  /** Get recently detected arbitrage opportunities */
  async getArbitrageOpportunities(): Promise<ArbitrageOpportunity[]> {
    return apiClient.get<ArbitrageOpportunity[]>("/api/trades/arbitrage/opportunities");
  },

  /** Trigger an immediate arbitrage scan */
  async scanArbitrage(): Promise<{
    found: number;
    opportunities: ArbitrageOpportunity[];
  }> {
    return apiClient.post("/api/trades/arbitrage/scan");
  },

  /** Get quality score breakdown for a specific trader */
  async getTraderQuality(wallet: string): Promise<TraderQuality> {
    return apiClient.get<TraderQuality>(`/api/trades/trader/${wallet}/quality`);
  },

  /** Recompute quality scores for all tracked traders */
  async rescoreAllTraders(): Promise<{ scored: number; message: string }> {
    return apiClient.post("/api/trades/traders/rescore");
  },
};
