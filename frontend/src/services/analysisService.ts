import { apiClient } from "./apiClient";
import { API_BASE_URL } from "../config/api";

// --- Request Types ---

export interface MarketAnalysisRequest {
  market_title: string;
  market_description: string;
  yes_price: number;
  no_price: number;
  volume_24h?: number;
  end_date: string;
  include_research?: boolean;
}

export interface QuickAnalysisRequest {
  question: string;
  current_price: number;
}

export interface QuickGroupAnalysisSubMarket {
  label: string;
  question?: string;
  yes_price: number;
  no_price: number;
  volume_24h?: number;
  liquidity?: number;
  condition_id?: string;
}

export interface QuickGroupAnalysisRequest {
  event_title: string;
  event_slug?: string;
  event_volume?: number;
  event_liquidity?: number;
  sub_markets: QuickGroupAnalysisSubMarket[];
}

export interface QuickGroupAnalysisData {
  analysis: string;
  event_title: string;
  recommended_option?: string;
  recommended_side?: "YES" | "NO" | string;
}

export interface MarketScanRequest {
  markets: Record<string, any>[];
}

export interface RiskAssessmentRequest {
  market_title: string;
  position_size: number;
  entry_price: number;
  days_to_expiry: number;
  correlation_info?: string;
}

export interface TradePlanRequest {
  action: "buy_yes" | "buy_no" | "sell_yes" | "sell_no";
  market_title: string;
  target_size: number;
  current_price: number;
  order_book?: Record<string, any>;
}

export interface TraderAnalysisRequest {
  wallet_address: string;
  display_name?: string;
  total_pnl: number;
  win_rate: number;
  trade_count: number;
  markets_traded: number;
  recent_trades_json?: string;
}

// --- Opportunity Scoring Types ---

export interface OpportunityScanRequest {
  limit?: number;
  force_refresh?: boolean;
}

export interface OpportunityScore {
  condition_id: string;
  market_title: string;
  ai_score: number; // 0-100
  risk_level: "low" | "medium" | "high";
  pnl_potential: number; // 0-1
  credibility_score: number; // 0-100
  smart_money_signal: "bullish" | "bearish" | "neutral";
  smart_money_summary: string;
  recommendation: "strong_buy" | "buy" | "hold" | "avoid";
  recommended_side: "YES" | "NO";
  recommended_option?: string; // groupItemTitle of AI-recommended sub-market
  reasoning: string;
  search_summary: string;
  key_risks: string[];
  whale_count?: number;
  whale_bias?: "YES" | "NO" | "MIXED";
  yes_whale_pct?: number;
  no_whale_pct?: number;
  search_findings?: string;
}

// --- Response Types ---

export interface AnalysisResponse {
  success: boolean;
  data: Record<string, any>;
  timestamp: string;
  backend?: string;
}

export interface HealthCheckResponse {
  status: string;
  backends: {
    llm_chain: { healthy: boolean; host: string };
    cli_agent: { healthy: boolean; host: string };
  };
}

// --- Service ---

export const analysisService = {
  /** Full AI market analysis with optional web research */
  async analyzeMarket(
    request: MarketAnalysisRequest,
  ): Promise<AnalysisResponse> {
    return apiClient.post<AnalysisResponse>("/api/analysis/market", request);
  },

  /** Quick 2-3 sentence analysis */
  async quickAnalysis(
    request: QuickAnalysisRequest,
  ): Promise<AnalysisResponse> {
    return apiClient.post<AnalysisResponse>("/api/analysis/quick", request);
  },

  /** Quick grouped-event analysis (event treated as one trade with multiple options) */
  async quickGroupAnalysis(
    request: QuickGroupAnalysisRequest,
  ): Promise<AnalysisResponse> {
    return apiClient.post<AnalysisResponse>("/api/analysis/quick-group", request);
  },

  /** Scan multiple markets for top opportunities */
  async scanMarkets(request: MarketScanRequest): Promise<AnalysisResponse> {
    return apiClient.post<AnalysisResponse>("/api/analysis/scan", request);
  },

  /** Assess risk for a potential trade */
  async assessRisk(request: RiskAssessmentRequest): Promise<AnalysisResponse> {
    return apiClient.post<AnalysisResponse>("/api/analysis/risk", request);
  },

  /** Generate a trade execution plan */
  async generateTradePlan(
    request: TradePlanRequest,
  ): Promise<AnalysisResponse> {
    return apiClient.post<AnalysisResponse>(
      "/api/analysis/trade-plan",
      request,
    );
  },

  /** Check health of AI backends */
  async healthCheck(): Promise<HealthCheckResponse> {
    return apiClient.get<HealthCheckResponse>("/api/analysis/health");
  },

  /** Full AI analysis of a trader's profile and patterns */
  async analyzeTrader(
    request: TraderAnalysisRequest,
  ): Promise<AnalysisResponse> {
    return apiClient.post<AnalysisResponse>("/api/analysis/trader", request);
  },

  /**
   * Stream AI trader analysis via SSE.
   * Returns an AbortController so the caller can cancel.
   */
  streamTraderAnalysis(
    request: TraderAnalysisRequest,
    callbacks: {
      onChunk: (text: string) => void;
      onDone: () => void;
      onError: (err: string) => void;
    },
  ): AbortController {
    const controller = new AbortController();

    (async () => {
      try {
        const res = await fetch(`${API_BASE_URL}/api/analysis/trader/stream`, {
          method: "POST",
          headers: {
            "Content-Type": "application/json",
          },
          credentials: "include",
          body: JSON.stringify(request),
          signal: controller.signal,
        });

        if (!res.ok) {
          const errBody = await res.text();
          callbacks.onError(errBody || `HTTP ${res.status}`);
          return;
        }

        const reader = res.body?.getReader();
        if (!reader) {
          callbacks.onError("No response body");
          return;
        }

        const decoder = new TextDecoder();
        let buffer = "";

        while (true) {
          const { done, value } = await reader.read();
          if (done) break;

          buffer += decoder.decode(value, { stream: true });
          const lines = buffer.split("\n");
          buffer = lines.pop() || "";

          for (const line of lines) {
            const trimmed = line.trim();
            if (!trimmed.startsWith("data: ")) continue;
            const jsonStr = trimmed.slice(6);
            try {
              const parsed = JSON.parse(jsonStr);
              if (parsed.error) {
                callbacks.onError(parsed.error);
                return;
              }
              if (parsed.done) {
                callbacks.onDone();
                return;
              }
              if (parsed.chunk) {
                callbacks.onChunk(parsed.chunk);
              }
            } catch {
              // skip malformed JSON
            }
          }
        }
        callbacks.onDone();
      } catch (err: unknown) {
        if ((err as Error).name === "AbortError") return;
        callbacks.onError((err as Error).message || "Stream failed");
      }
    })();

    return controller;
  },

  /**
   * Stream full market analysis via SSE.
   * Calls onChunk for each text fragment, onDone when complete, onError on failure.
   * Returns an AbortController so the caller can cancel.
   */
  streamMarketAnalysis(
    request: MarketAnalysisRequest,
    callbacks: {
      onChunk: (text: string) => void;
      onDone: () => void;
      onError: (err: string) => void;
    },
  ): AbortController {
    const controller = new AbortController();

    (async () => {
      try {
        const res = await fetch(`${API_BASE_URL}/api/analysis/market/stream`, {
          method: "POST",
          headers: {
            "Content-Type": "application/json",
          },
          credentials: "include",
          body: JSON.stringify(request),
          signal: controller.signal,
        });

        if (!res.ok) {
          const errBody = await res.text();
          callbacks.onError(errBody || `HTTP ${res.status}`);
          return;
        }

        const reader = res.body?.getReader();
        if (!reader) {
          callbacks.onError("No response body");
          return;
        }

        const decoder = new TextDecoder();
        let buffer = "";

        while (true) {
          const { done, value } = await reader.read();
          if (done) break;

          buffer += decoder.decode(value, { stream: true });
          const lines = buffer.split("\n");
          buffer = lines.pop() || "";

          for (const line of lines) {
            const trimmed = line.trim();
            if (!trimmed.startsWith("data: ")) continue;
            const jsonStr = trimmed.slice(6);
            try {
              const parsed = JSON.parse(jsonStr);
              if (parsed.error) {
                callbacks.onError(parsed.error);
                return;
              }
              if (parsed.done) {
                callbacks.onDone();
                return;
              }
              if (parsed.chunk) {
                callbacks.onChunk(parsed.chunk);
              }
            } catch {
              // skip malformed JSON
            }
          }
        }
        // stream ended without explicit done event
        callbacks.onDone();
      } catch (err: unknown) {
        if ((err as Error).name === "AbortError") return;
        callbacks.onError((err as Error).message || "Stream failed");
      }
    })();

    return controller;
  },

  /**
   * Stream AI opportunity scanning via SSE.
   * Progressively returns per-market scores as each market is analyzed.
   * Returns an AbortController so the caller can cancel.
   */
  streamOpportunityScan(
    request: OpportunityScanRequest,
    callbacks: {
      onMarketScored: (
        marketId: string,
        scores: OpportunityScore,
        doneCount: number,
        total: number,
      ) => void;
      onStatus: (status: string, total?: number) => void;
      onDone: (total: number) => void;
      onError: (err: string) => void;
    },
  ): AbortController {
    const controller = new AbortController();

    (async () => {
      try {
        const res = await fetch(
          `${API_BASE_URL}/api/analysis/opportunities/stream`,
          {
            method: "POST",
            headers: {
              "Content-Type": "application/json",
            },
            credentials: "include",
            body: JSON.stringify(request),
            signal: controller.signal,
          },
        );

        if (!res.ok) {
          const errBody = await res.text();
          callbacks.onError(errBody || `HTTP ${res.status}`);
          return;
        }

        const reader = res.body?.getReader();
        if (!reader) {
          callbacks.onError("No response body");
          return;
        }

        const decoder = new TextDecoder();
        let buffer = "";

        while (true) {
          const { done, value } = await reader.read();
          if (done) break;

          buffer += decoder.decode(value, { stream: true });
          const lines = buffer.split("\n");
          buffer = lines.pop() || "";

          for (const line of lines) {
            const trimmed = line.trim();
            if (!trimmed.startsWith("data: ")) continue;
            const jsonStr = trimmed.slice(6);
            try {
              const parsed = JSON.parse(jsonStr);
              if (parsed.error) {
                callbacks.onError(parsed.error);
                return;
              }
              if (parsed.all_done) {
                callbacks.onDone(parsed.total || 0);
                return;
              }
              if (parsed.status) {
                callbacks.onStatus(parsed.status, parsed.total);
                continue;
              }
              if (parsed.scores && parsed.market_id) {
                callbacks.onMarketScored(
                  parsed.market_id,
                  parsed.scores as OpportunityScore,
                  parsed.done_count || 0,
                  parsed.total || 0,
                );
              }
            } catch {
              // skip malformed JSON
            }
          }
        }
        callbacks.onDone(0);
      } catch (err: unknown) {
        if ((err as Error).name === "AbortError") return;
        callbacks.onError((err as Error).message || "Stream failed");
      }
    })();

    return controller;
  },
};
