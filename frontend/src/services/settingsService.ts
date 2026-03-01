import { apiClient } from "./apiClient";

export type RiskMode =
  | "max_position_daily_loss"
  | "percentage_mirror"
  | "fixed_amount";
export type InverseBotSizeMode = "full_notional" | "fixed_amount";

export interface UserSettings {
  ai_backend: "llm_chain" | "cli_agent";
  copy_trading_enabled: boolean;
  risk_mode: RiskMode;
  max_position_size: number;
  daily_loss_limit: number;
  mirror_percentage: number;
  fixed_trade_amount: number;
  require_ai_approval: boolean;
  follow_email_notifications_enabled: boolean;
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
  two_fa_enabled: boolean;
}

export interface UpdateProfileRequest {
  display_name?: string;
  email?: string | null;
  phone?: string | null;
  profile_picture_url?: string | null;
  two_fa_enabled?: boolean;
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

/**
 * Get current user settings
 */
export async function getUserSettings(): Promise<UserSettings> {
  return apiClient.get<UserSettings>("/api/settings");
}

/**
 * Update user settings
 */
export async function updateUserSettings(
  settings: Partial<UserSettings>,
): Promise<UserSettings> {
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
export async function updateUserProfile(
  profile: UpdateProfileRequest,
): Promise<UserProfile> {
  return apiClient.put<UserProfile>("/api/settings/profile", profile);
}

export const settingsService = {
  getUserSettings,
  updateUserSettings,
  getBackendsStatus,
  updateCopyTradingSettings,
  getUserProfile,
  updateUserProfile,
};
