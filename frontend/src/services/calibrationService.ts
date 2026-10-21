/**
 * Forecast calibration endpoints under `/api/analysis/calibration`.
 *
 * Scores the AI's probability estimates against market resolutions:
 * Brier score versus the 0.25 coin-flip baseline, the reliability
 * curve (predicted vs actual frequency per probability bucket), and
 * per-model / per-task accuracy, plus the raw per-market forecast
 * rows with probability, market price, edge and Brier score.
 *
 * Model governance (plan 04) layers on top of the same rows: per
 * model/task drift with the degrading slope, calibration grouped by
 * the prompt version that produced each verdict, and human-in-the-loop
 * verdict feedback. The A/B experiment endpoints live under
 * `/api/analysis/experiments`, are admin-only, and are therefore
 * exposed through the separate `calibrationAdminService` object below
 * so no non-admin surface can call them by accident.
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
  /**
   * Rows that carry a Brier score but a resolution label that is not a
   * finalized binary YES/NO (the legacy resolver stored the raw Gamma
   * label). They are excluded from every aggregate.
   */
  invalid_legacy_resolution: number;
  /** Rows excluded from scoring, per exclusion reason. */
  exclusion_reasons?: Record<string, number>;
  /** Total number of excluded rows. */
  excluded_count?: number;
  /** Rows without a probability, per recorded reason. */
  null_probability_reasons?: Record<string, number>;
}

export interface CalibrationSummaryResponse {
  window_days: number;
  scope: string;
  generated_at: string;
  sample_count: number;
  resolved_count: number;
  /**
   * Number of distinct markets behind the resolved rows: one market
   * contributes one sample, while `resolved_count` stays the raw row
   * count.
   */
  effective_sample_count?: number;
  mean_brier: number | null;
  /** Mean log loss over the effective samples (finite even for p = 0/1). */
  mean_log_loss?: number | null;
  brier_vs_baseline: number;
  reliability: ReliabilityBucket[];
  by_model: CalibrationGroupStats[];
  by_task: CalibrationGroupStats[];
  data_quality: CalibrationDataQuality;
}

export interface CalibrationForecast {
  /**
   * `assessments.id`, the handle every feedback endpoint needs. The
   * `/forecasts` route does not serialise it yet, so it is `null`
   * until the backend adds `"id": row.id` to that dict; the UI reads
   * it through this field and degrades gracefully while it is absent.
   */
  id: number | null;
  condition_id: string | null;
  task: string | null;
  model: string | null;
  provider: string | null;
  prompt_version: string | null;
  llm_tier: string | null;
  ai_probability: number | null;
  market_price: number | null;
  edge: number | null;
  resolved_outcome: string | null;
  brier_score: number | null;
  scoring_version: string | null;
  exclusion_reason: string | null;
  verified_resolution: boolean;
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

// ── Prompt versions ────────────────────────────────────────

export interface PromptVersionGroup {
  /** Content hash of the prompt store, or `(unversioned)` for legacy rows. */
  prompt_version: string;
  count: number;
  resolved: number;
  mean_brier: number | null;
}

export interface PromptVersionResponse {
  window_days: number;
  scope: string;
  baseline_brier: number;
  versions: PromptVersionGroup[];
}

// ── Model drift ────────────────────────────────────────────

export interface CalibrationDriftRow {
  model_name: string | null;
  task: string | null;
  window_days: number;
  samples: number;
  mean_brier: number | null;
  excess_over_baseline: number | null;
  slope_per_day: number | null;
  baseline_brier: number;
  min_samples: number;
  slope_alarm: number;
  drift_margin: number;
  drifted: boolean;
  /** `excess_over_baseline`, `degrading_slope` or `insufficient_samples`. */
  reasons: string[];
}

export interface CalibrationDriftResponse {
  window_days: number;
  baseline_brier: number;
  min_samples_for_drift: number;
  series: CalibrationDriftRow[];
  drifted: CalibrationDriftRow[];
  drifted_count: number;
  scope: string;
}

// ── Model leaderboard ──────────────────────────────────────

export interface ModelLeaderboardEntry {
  model_name: string;
  task: string;
  samples: number;
  resolved: number;
  mean_brier: number | null;
}

export interface ModelLeaderboardResponse {
  window_days: number;
  baseline_brier: number;
  models: ModelLeaderboardEntry[];
  scope: string;
}

// ── Verdict feedback ───────────────────────────────────────

export interface VerdictFeedbackMine {
  rating: number;
  comment: string | null;
}

export interface VerdictFeedbackCommunity {
  count: number;
  mean_rating: number | null;
}

export interface VerdictFeedbackResponse {
  assessment_id: number;
  mine: VerdictFeedbackMine | null;
  community: VerdictFeedbackCommunity;
}

export interface VerdictFeedbackSubmission {
  id: number;
  assessment_id: number;
  rating: number;
  comment: string | null;
}

export interface VerdictFeedbackPayload {
  rating: number;
  comment?: string | null;
}

// ── Experiments (admin only) ───────────────────────────────

/** `control_config_json` / `variant_config_json` decoded by the API. */
export type ExperimentConfig = Record<string, unknown> | null;

export interface LLMExperiment {
  id: number;
  name: string;
  task: string;
  control_config: ExperimentConfig;
  variant_config: ExperimentConfig;
  traffic_pct: number;
  status: string;
  created_by: string | null;
  created_at: string | null;
  updated_at: string | null;
}

export interface ExperimentListResponse {
  count: number;
  experiments: LLMExperiment[];
}

export interface ExperimentCreatePayload {
  name: string;
  task: string;
  traffic_pct: number;
  control_config?: ExperimentConfig;
  variant_config?: ExperimentConfig;
}

/** Mirrors `ExperimentStatusRequest`: `active | paused | done`. */
export type ExperimentStatus = "active" | "paused" | "done";

export interface ExperimentArmStats {
  samples: number;
  resolved: number;
  mean_brier: number | null;
  best_brier: number | null;
  worst_brier: number | null;
}

export type ExperimentVerdict =
  "variant_better" | "control_better" | "no_difference" | "insufficient_samples" | "inconclusive";

export interface ExperimentComparisonResponse {
  experiment: string;
  window_days: number;
  /** Arms are absent entirely while a task has no rows under them. */
  arms: Partial<Record<"control" | "variant", ExperimentArmStats>>;
  verdict: ExperimentVerdict;
  /** Only present once the API downgrades an inconclusive comparison. */
  min_resolved_for_verdict?: number;
}

export interface ExperimentComparisonQuery {
  window_days?: number;
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

  async getSummaryByPromptVersion(query: CalibrationQuery = {}): Promise<PromptVersionResponse> {
    return apiClient.get<PromptVersionResponse>(
      "/api/analysis/calibration/summary/by-prompt-version",
      { params: query },
    );
  },

  async getDrift(query: CalibrationQuery = {}): Promise<CalibrationDriftResponse> {
    return apiClient.get<CalibrationDriftResponse>("/api/analysis/calibration/summary/drift", {
      params: query,
    });
  },

  async getModelLeaderboard(query: CalibrationQuery = {}): Promise<ModelLeaderboardResponse> {
    return apiClient.get<ModelLeaderboardResponse>("/api/analysis/calibration/leaderboard/models", {
      params: query,
    });
  },

  /** The caller's own rating plus the community mean for one verdict. */
  async getVerdictFeedback(assessmentId: number): Promise<VerdictFeedbackResponse> {
    return apiClient.get<VerdictFeedbackResponse>(
      `/api/analysis/calibration/verdicts/${assessmentId}/feedback`,
    );
  },

  /** Create or replace the caller's rating (one rating per user per verdict). */
  async submitVerdictFeedback(
    assessmentId: number,
    payload: VerdictFeedbackPayload,
  ): Promise<VerdictFeedbackSubmission> {
    return apiClient.post<VerdictFeedbackSubmission>(
      `/api/analysis/calibration/verdicts/${assessmentId}/feedback`,
      payload,
    );
  },

  /** Withdraw the caller's rating. */
  async withdrawVerdictFeedback(assessmentId: number): Promise<{ deleted: boolean }> {
    return apiClient.delete<{ deleted: boolean }>(
      `/api/analysis/calibration/verdicts/${assessmentId}/feedback`,
    );
  },
};

/**
 * Admin-only experiment endpoints (`/api/analysis/experiments`).
 *
 * Kept apart from `calibrationService` so a non-admin surface cannot
 * reach them: every method returns 403 for a non-admin session, and
 * no component below the admin gate should import this object.
 */
export const calibrationAdminService = {
  async listExperiments(task?: string): Promise<ExperimentListResponse> {
    return apiClient.get<ExperimentListResponse>("/api/analysis/experiments", {
      params: task ? { task } : undefined,
    });
  },

  async createExperiment(payload: ExperimentCreatePayload): Promise<LLMExperiment> {
    return apiClient.post<LLMExperiment>("/api/analysis/experiments", payload);
  },

  /** Pause (`paused`), resume (`active`) or retire (`done`) an experiment. */
  async setExperimentStatus(
    experimentId: number,
    status: ExperimentStatus,
  ): Promise<LLMExperiment> {
    return apiClient.patch<LLMExperiment>(`/api/analysis/experiments/${experimentId}`, { status });
  },

  async getExperimentComparison(
    experimentName: string,
    query: ExperimentComparisonQuery = {},
  ): Promise<ExperimentComparisonResponse> {
    return apiClient.get<ExperimentComparisonResponse>(
      `/api/analysis/experiments/${encodeURIComponent(experimentName)}/comparison`,
      { params: query },
    );
  },
};
