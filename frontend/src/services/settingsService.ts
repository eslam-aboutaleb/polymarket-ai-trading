/**
 * User settings, profile, and LLM administration endpoints under `/api/settings`.
 *
 * Three groups: core settings (`/api/settings`, `/copy-trading`) covering copy-trading risk modes,
 * multi-layer loss limits, and inverse-bot defaults; profile read/update including avatar, contact
 * details, and personal LLM provider/model preferences; and LLM preferences, which include personal provider/model selection plus admin
 * endpoints for system defaults and per-user overrides. Every function is also re-exported on the
 * `settingsService` object.
 *
 * @module services/settingsService
 */

import { apiClient } from "./apiClient";

export type RiskMode = "max_position_daily_loss" | "percentage_mirror" | "fixed_amount";
export type InverseBotSizeMode = "full_notional" | "fixed_amount";

export interface UserSettings {
  ai_backend: "llm_chain" | "cli_agent";
  preferred_llm_provider: string | null;
  preferred_llm_model: string | null;
  copy_trading_enabled: boolean;
  risk_mode: RiskMode;
  max_position_size: number;
  daily_loss_limit: number;
  mirror_percentage: number;
  fixed_trade_amount: number;
  require_ai_approval: boolean;
  follow_email_notifications_enabled: boolean;
  // Multi-layer risk protection
  monthly_loss_limit: number | null;
  max_drawdown_pct: number;
  total_loss_halt_pct: number;
  peak_capital: number | null;
  initial_capital: number | null;
  trading_halted: boolean;
  halt_reason: string | null;
  cooldown_until: string | null;
  // Dynamic sizing
  dynamic_sizing_enabled: boolean;
  consecutive_wins: number;
  consecutive_losses: number;
  // Simulation mode
  simulation_mode: boolean;
  paper_balance: number;
  // Inverse bot
  inverse_bot_enabled: boolean;
  inverse_bot_default_size_mode: InverseBotSizeMode;
  inverse_bot_fixed_amount: number;
  inverse_bot_confidence_threshold: number;
  inverse_bot_cooldown_minutes: number;
  inverse_bot_max_reversals_per_day: number;
  updated_at: string | null;
}

export interface CopyTradingSettingsUpdate {
  copy_trading_enabled?: boolean;
  risk_mode?: RiskMode;
  max_position_size?: number;
  daily_loss_limit?: number;
  mirror_percentage?: number;
  fixed_trade_amount?: number;
  require_ai_approval?: boolean;
  follow_email_notifications_enabled?: boolean;
  simulation_mode?: boolean;
  paper_balance?: number;
  inverse_bot_enabled?: boolean;
  inverse_bot_default_size_mode?: InverseBotSizeMode;
  inverse_bot_fixed_amount?: number;
  inverse_bot_confidence_threshold?: number;
  inverse_bot_cooldown_minutes?: number;
  inverse_bot_max_reversals_per_day?: number;
}

export interface UserProfile {
  wallet_address: string;
  display_name: string;
  email: string | null;
  phone: string | null;
  profile_picture_url: string | null;
}

export interface UpdateProfileRequest {
  display_name?: string;
  email?: string | null;
  phone?: string | null;
  profile_picture_url?: string | null;
}

export interface AIBackendStatus {
  name: string;
  description: string;
  healthy: boolean;
  status: string;
}

export interface BackendsStatus {
  llm_chain: AIBackendStatus;
  cli_agent: AIBackendStatus;
}

// ── LLM Provider Types ──

export type LLMProvider = "openai" | "anthropic" | "google" | "groq" | "ollama" | "github_models";

export interface LLMProviderInfo {
  id: string;
  name: string;
  backend: string;
  models: string[];
  description: string;
  requires_api_key: boolean;
}

export interface LLMProvidersResponse {
  providers: LLMProviderInfo[];
  default_provider: string;
}

export interface LLMCurrentSettings {
  provider: string | null;
  model: string | null;
  effective_provider: string;
  available_models: string[];
}

export interface UpdateLLMSettingsRequest {
  provider?: string | null;
  model?: string | null;
}

// ── Admin Types ──

export interface AdminProviderStatus extends LLMProviderInfo {
  is_configured: boolean;
  is_healthy: boolean;
}

export interface AdminProvidersResponse {
  providers: AdminProviderStatus[];
  default_provider: string;
  default_model: string;
}

export interface AdminDefaultsRequest {
  default_provider?: string;
  default_model?: string;
}

export interface AdminDefaultsResponse {
  success: boolean;
  default_provider: string;
  default_model: string;
  note: string;
}

export interface AdminUserLLMSettings {
  user_id: number;
  wallet_address: string;
  display_name: string | null;
  preferred_llm_provider: string | null;
  preferred_llm_model: string | null;
  ai_backend: string;
  updated_at: string | null;
}

export interface AdminUsersLLMResponse {
  users: AdminUserLLMSettings[];
  total: number;
}

export interface AdminUpdateUserLLMRequest {
  preferred_llm_provider?: string | null;
  preferred_llm_model?: string | null;
}

// ── Paper Trading ──

export interface PaperSummary {
  simulation_mode: boolean;
  paper_balance: number;
  paper_pnl: number;
  simulated_trades: number;
}

/**
 * Get the paper-trading summary (balance + realized paper PnL,
 * tracked separately from real equity).
 */
export async function getPaperSummary(): Promise<PaperSummary> {
  return apiClient.get<PaperSummary>("/api/settings/paper-summary");
}

/**
 * Get current user settings
 */
export async function getUserSettings(): Promise<UserSettings> {
  return apiClient.get<UserSettings>("/api/settings");
}

/**
 * Update user settings
 */
export async function updateUserSettings(settings: Partial<UserSettings>): Promise<UserSettings> {
  return apiClient.put<UserSettings>("/api/settings", settings);
}

/**
 * Get status of AI backends
 */
export async function getBackendsStatus(): Promise<BackendsStatus> {
  return apiClient.get<BackendsStatus>("/api/settings/backends/status");
}

/**
 * Update copy-trading settings
 */
export async function updateCopyTradingSettings(
  settings: CopyTradingSettingsUpdate,
): Promise<UserSettings> {
  return apiClient.put<UserSettings>("/api/settings/copy-trading", settings);
}

/**
 * Get user profile (name, avatar, contact)
 */
export async function getUserProfile(): Promise<UserProfile> {
  return apiClient.get<UserProfile>("/api/settings/profile");
}

/**
 * Update user profile
 */
export async function updateUserProfile(profile: UpdateProfileRequest): Promise<UserProfile> {
  return apiClient.put<UserProfile>("/api/settings/profile", profile);
}

// ── LLM Settings API ──

/**
 * Get available LLM providers
 */
export async function getLLMProviders(): Promise<LLMProvidersResponse> {
  return apiClient.get<LLMProvidersResponse>("/api/settings/llm/providers");
}

/**
 * Get current user's LLM settings
 */
export async function getCurrentLLMSettings(): Promise<LLMCurrentSettings> {
  return apiClient.get<LLMCurrentSettings>("/api/settings/llm/current");
}

/**
 * Update user's LLM settings
 */
export async function updateLLMSettings(
  settings: UpdateLLMSettingsRequest,
): Promise<LLMCurrentSettings> {
  return apiClient.patch<LLMCurrentSettings>("/api/settings/llm", settings);
}

// ── Admin LLM API ──

/**
 * Get all providers with admin status info
 */
export async function getAdminProviders(): Promise<AdminProvidersResponse> {
  return apiClient.get<AdminProvidersResponse>("/api/settings/admin/providers");
}

/**
 * Update system-wide default provider/model
 */
export async function updateAdminDefaults(
  request: AdminDefaultsRequest,
): Promise<AdminDefaultsResponse> {
  return apiClient.patch<AdminDefaultsResponse>("/api/settings/admin/defaults", request);
}

/**
 * Get all users with their LLM settings
 */
export async function getAdminUsersLLM(
  skip: number = 0,
  limit: number = 50,
): Promise<AdminUsersLLMResponse> {
  return apiClient.get<AdminUsersLLMResponse>(
    `/api/settings/admin/users?skip=${skip}&limit=${limit}`,
  );
}

/**
 * Update a user's LLM settings (admin override)
 */
export async function updateAdminUserLLM(
  userId: number,
  request: AdminUpdateUserLLMRequest,
): Promise<AdminUserLLMSettings> {
  return apiClient.patch<AdminUserLLMSettings>(`/api/settings/admin/users/${userId}/llm`, request);
}

export const settingsService = {
  getUserSettings,
  updateUserSettings,
  getBackendsStatus,
  updateCopyTradingSettings,
  getUserProfile,
  updateUserProfile,
  getPaperSummary,
  // LLM Settings
  getLLMProviders,
  getCurrentLLMSettings,
  updateLLMSettings,
  // Admin
  getAdminProviders,
  updateAdminDefaults,
  getAdminUsersLLM,
  updateAdminUserLLM,
};
