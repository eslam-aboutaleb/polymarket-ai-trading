/**
 * Forecast calibration endpoints under `/api/analysis/calibration`.
 *
 * Scores the AI's probability estimates against market resolutions:
 * Brier score versus the 0.25 coin-flip baseline, the reliability
 * curve (predicted vs actual frequency per probability bucket), and
 * per-model / per-task accuracy, plus the raw per-market forecast
 * rows with probability, market price, edge and Brier score.
 *
 * Legacy verdicts without an `ai_probability` are never backfilled;
 * they are reported in `data_quality` instead.
 *
 * @module services/calibrationService
 */

import { apiClient } from "./apiClient";

// ── Types ───────────────────────────────────────────────────

export interface ReliabilityBucket {
  bucket: string;
  predicted: number | null;
  actual: number | null;
  count: number;
}

export interface CalibrationGroupStats {
  model_name?: string;
  task?: string;
  count: number;
  mean_brier: number | null;
}

export interface CalibrationDataQuality {
  missing_model: number;
  legacy_excluded: number;
}

export interface CalibrationSummaryResponse {
  window_days: number;
  scope: string;
  generated_at: string;
  sample_count: number;
  resolved_count: number;
  mean_brier: number | null;
  brier_vs_baseline: number;
  reliability: ReliabilityBucket[];
  by_model: CalibrationGroupStats[];
  by_task: CalibrationGroupStats[];
  data_quality: CalibrationDataQuality;
}

export interface CalibrationForecast {
  condition_id: string | null;
  task: string | null;
  model: string | null;
  provider: string | null;
  ai_probability: number | null;
  market_price: number | null;
  edge: number | null;
  resolved_outcome: string | null;
  brier_score: number | null;
  created_at: string | null;
}

export interface CalibrationForecastsResponse {
  count: number;
  forecasts: CalibrationForecast[];
}

export interface CalibrationQuery {
  window_days?: number;
  scope?: "user" | "global";
}

// ── Service ─────────────────────────────────────────────────

export const calibrationService = {
  async getSummary(query: CalibrationQuery = {}): Promise<CalibrationSummaryResponse> {
    return apiClient.get<CalibrationSummaryResponse>("/api/analysis/calibration/summary", {
      params: query,
    });
  },

  async getForecasts(
    limit: number = 50,
    query: CalibrationQuery = {},
  ): Promise<CalibrationForecastsResponse> {
    return apiClient.get<CalibrationForecastsResponse>("/api/analysis/calibration/forecasts", {
      params: { ...query, limit },
    });
  },
};
