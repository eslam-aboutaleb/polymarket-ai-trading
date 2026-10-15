/**
 * Collaborative forecasting endpoints under `/api/collaboration` (plan 02).
 *
 * A user casts one probability per market, revises it by voting again,
 * and annotates any market with a note that may carry the same
 * probability. Both start **private**: a wallet-linked vote is linkable
 * to a wallet, so publishing one is an explicit opt-in this UI states
 * before it happens. Only public votes enter the consensus and the
 * accuracy board, and a caller never sees another user's private note.
 *
 * The consensus payload is a description, not a signal: it carries the
 * AI probability, the weighted consensus of public forecasts, the market
 * price and the disagreement gap between them. Nothing in this service
 * places or implies an order.
 *
 * `advisory` is echoed by the backend on every vote/consensus response
 * and is surfaced in the panel so the "informational only" framing is
 * visible rather than buried in a docstring.
 *
 * @module services/collaborationService
 */

import { apiClient } from "./apiClient";

// ── Types ───────────────────────────────────────────────────

/** `private` (default) or `public` (opt-in, enters the consensus). */
export type Visibility = "private" | "public";

export interface ConsensusResponse {
  condition_id: string;
  generated_at: string;
  /** True when the payload was served from the 60s consensus cache. */
  cached: boolean;
  /** Always `informational` — never a trade instruction. */
  advisory: string;
  window_days: number;
  n_votes: number;
  n_contributors: number;
  /** Weighted mean of the public votes, or null with no public votes. */
  consensus: number | null;
  ai_probability: number | null;
  market_price: number | null;
  /** Consensus − market price: the crowd against the price. */
  gap: number | null;
  /** Consensus − AI probability. */
  ai_gap: number | null;
  /** Spread between the pooled forecasts — how much they disagree. */
  disagreement: number | null;
  /** True when the caller's own vote was dropped from the pool. */
  excluded_self: boolean;
  contributors: ConsensusContributor[];
  /** Rendered from `collaborative.consensus_summary` when present. */
  narrative?: string;
}

export interface ConsensusContributor {
  user_id: number;
  display_name: string | null;
  wallet_address?: string;
  probability: number;
  weight: number;
  skill: number;
  resolved: number;
  vote_seq: number;
}

export interface VoteSubmission {
  id: number;
  condition_id: string;
  probability: number;
  vote_seq: number;
  visibility: Visibility;
  created_at: string | null;
  advisory: string;
}

export interface VotePayload {
  probability: number;
  visibility?: Visibility;
}

export interface Annotation {
  id: number;
  user_id: number;
  condition_id: string;
  body: string;
  probability: number | null;
  visibility: Visibility;
  created_at: string | null;
  updated_at: string | null;
  /** True for the caller's own row, whatever its visibility. */
  mine: boolean;
}

export interface AnnotationListResponse {
  condition_id: string;
  count: number;
  annotations: Annotation[];
}

export interface AnnotationPayload {
  body: string;
  probability?: number | null;
  visibility?: Visibility;
}

export interface FollowRow {
  /** `user_follows.id` — the row this edge was created from. */
  id?: number;
  follower_user_id: number;
  followee_user_id: number;
  created_at: string | null;
}

export interface ForecasterSummary {
  user_id: number;
  display_name: string | null;
  wallet_address: string | null;
  followed_at: string | null;
  /** Brier skill against the 0.25 baseline, clipped at zero. */
  skill: number;
  resolved: number;
  sufficient_samples: boolean;
}

export interface FollowingForecast {
  condition_id: string;
  probability: number;
  vote_seq: number;
  created_at: string | null;
  forecaster: {
    user_id: number;
    display_name: string | null;
    wallet_address: string | null;
    skill: number;
    resolved: number;
  };
}

export interface FollowedForecastersResponse {
  count: number;
  following: ForecasterSummary[];
  forecasts: FollowingForecast[];
}

export interface ForecasterBoardEntry {
  user_id: number;
  display_name: string | null;
  wallet_address: string | null;
  skill: number;
  mean_brier: number | null;
  resolved: number;
  public_votes: number;
  total_votes: number;
  /** Ensemble weight this entry earns: 1 + k · skill (1 without samples). */
  weight: number;
}

export interface ForecasterBoardResponse {
  window_days: number;
  baseline_brier: number;
  scope: "user" | "global";
  count: number;
  forecasters: ForecasterBoardEntry[];
}

export interface ConsensusQuery {
  /** Drop the caller's own vote (default true — the portfolio rule). */
  exclude_self?: boolean;
  window_days?: number;
}

// ── Service ─────────────────────────────────────────────────

/**
 * Serialize optional query parameters, skipping `undefined`.
 *
 * Generic over the caller's own query shape so a named interface
 * (``ConsensusQuery``) does not need an index signature.
 */
function buildQuery<T extends object>(query: T): string {
  const params = new URLSearchParams();
  Object.entries(query).forEach(([key, value]) => {
    if (value === undefined || value === null) return;
    params.append(key, String(value));
  });
  const encoded = params.toString();
  return encoded ? `?${encoded}` : "";
}

export const collaborationService = {
  /** Cast or revise the caller's forecast on one market. */
  async putVote(conditionId: string, payload: VotePayload): Promise<VoteSubmission> {
    return apiClient.put<VoteSubmission>(
      `/api/collaboration/votes/${encodeURIComponent(conditionId)}`,
      payload,
    );
  },

  /**
   * Consensus for one market. The caller's own vote is excluded by
   * default (`exclude_self`), so a portfolio view never echoes only the
   * reader's own opinion back at them.
   */
  async getConsensus(conditionId: string, query: ConsensusQuery = {}): Promise<ConsensusResponse> {
    return apiClient.get<ConsensusResponse>(
      `/api/collaboration/consensus/${encodeURIComponent(conditionId)}${buildQuery(query)}`,
    );
  },

  /** Consensus for several markets in one round trip (max 50). */
  async getConsensusBatch(
    conditionIds: string[],
    consensusQuery: ConsensusQuery = {},
  ): Promise<{ count: number; consensus: Record<string, ConsensusResponse> }> {
    const ids = conditionIds.map((id) => encodeURIComponent(id)).join(",");
    return apiClient.get<{ count: number; consensus: Record<string, ConsensusResponse> }>(
      `/api/collaboration/consensus/batch${buildQuery({
        condition_ids: ids,
        ...consensusQuery,
      })}`,
    );
  },

  /** The caller's own note plus the market's public notes. */
  async getAnnotations(conditionId: string): Promise<AnnotationListResponse> {
    return apiClient.get<AnnotationListResponse>(
      `/api/collaboration/annotations/${encodeURIComponent(conditionId)}`,
    );
  },

  /** Create or replace the caller's single note on a market. */
  async putAnnotation(conditionId: string, payload: AnnotationPayload): Promise<Annotation> {
    return apiClient.put<Annotation>(
      `/api/collaboration/annotations/${encodeURIComponent(conditionId)}`,
      payload,
    );
  },

  /** Withdraw the caller's own note. */
  async deleteAnnotation(conditionId: string): Promise<{ deleted: boolean }> {
    return apiClient.delete<{ deleted: boolean }>(
      `/api/collaboration/annotations/${encodeURIComponent(conditionId)}`,
    );
  },

  /** Follow a forecaster (idempotent). Never mirrors trades. */
  async followUser(userId: number): Promise<FollowRow> {
    return apiClient.post<FollowRow>(`/api/collaboration/follow/${userId}`);
  },

  async unfollowUser(userId: number): Promise<{ unfollowed: boolean }> {
    return apiClient.delete<{ unfollowed: boolean }>(`/api/collaboration/follow/${userId}`);
  },

  /** Who the caller follows, plus their latest public forecasts. */
  async getFollowedForecasters(limit = 50): Promise<FollowedForecastersResponse> {
    return apiClient.get<FollowedForecastersResponse>(
      `/api/collaboration/forecasters${buildQuery({ limit })}`,
    );
  },

  /**
   * Forecasting accuracy board. `scope: "global"` also exposes private
   * participation counts and is admin-only — the backend answers 403
   * for a non-admin session.
   */
  async getForecasterBoard(
    windowDays: number,
    scope: "user" | "global" = "user",
    limit = 100,
  ): Promise<ForecasterBoardResponse> {
    return apiClient.get<ForecasterBoardResponse>(
      `/api/collaboration/leaderboard${buildQuery({
        window_days: windowDays,
        scope,
        limit,
      })}`,
    );
  },
};
