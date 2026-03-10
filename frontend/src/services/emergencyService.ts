import { apiClient } from "./apiClient";

// --- Types ---

export interface EmergencyStopResponse {
  halted: boolean;
  positions_closed: number;
  stop_losses_cancelled: number;
  take_profits_cancelled: number;
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
  /** Trigger emergency stop: halt trading, cancel SL/TP, optionally close all positions */
  async emergencyStop(closePositions = true): Promise<EmergencyStopResponse> {
    return apiClient.post<EmergencyStopResponse>(
      `/api/trades/emergency-stop?close_positions=${closePositions}`,
    );
  },

  /** Resume trading after emergency stop or risk halt */
  async resumeTrading(): Promise<{ resumed: boolean; message: string }> {
    return apiClient.post("/api/trades/resume-trading");
  },

  /** Get recently detected arbitrage opportunities */
  async getArbitrageOpportunities(): Promise<ArbitrageOpportunity[]> {
    return apiClient.get<ArbitrageOpportunity[]>(
      "/api/trades/arbitrage/opportunities",
    );
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
