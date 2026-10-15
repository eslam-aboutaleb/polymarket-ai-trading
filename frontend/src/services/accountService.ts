/**
 * Sub-account endpoints under `/api/accounts` (plan 06 Phase A).
 *
 * An owner wallet keeps one *personal* account — the identity the login
 * session and every pre-existing route already use — plus any number of
 * segregated *sub* accounts. Switching accounts sends `X-Account-Id` on
 * subsequent requests; the backend honours it only while the
 * `accounts_enabled` flag is on and otherwise falls back to the personal
 * account, so a stale selection is never an error.
 *
 * The active selection lives in `localStorage` under
 * `polymarket_active_account` because the rest of the app reads server
 * state through per-component `useState` (no react-query here): one
 * module-level store plus a change event is enough for the switcher and
 * the settings page to agree without a context provider.
 *
 * @module services/accountService
 */

import { apiClient } from "./apiClient";

// ── Types ───────────────────────────────────────────────────

export type AccountKind = "personal" | "sub";

export interface Account {
  id: number;
  owner_user_id: number;
  name: string;
  kind: AccountKind;
  is_default: boolean;
  created_at: string | null;
}

/** What the server resolved `X-Account-Id` to, plus the flag state. */
export interface ActiveAccount {
  account: Account;
  /** Feature flag. While false the header is ignored server-side. */
  accounts_enabled: boolean;
  /** True only when the flag is on AND a header was actually sent. */
  header_used: boolean;
}

export interface AccountSettings {
  account_id: number;
  kind: AccountKind;
  ai_backend: string;
  preferred_llm_provider: string | null;
  preferred_llm_model: string | null;
  copy_trading_enabled: boolean;
  risk_mode: string;
  max_position_size: number;
  daily_loss_limit: number;
  mirror_percentage: number;
  fixed_trade_amount: number;
  kelly_fraction: number;
  require_ai_approval: boolean;
  follow_email_notifications_enabled: boolean;
  daily_digest_enabled: boolean;
  monthly_loss_limit: number | null;
  max_drawdown_pct: number;
  total_loss_halt_pct: number;
  dynamic_sizing_enabled: boolean;
  simulation_mode: boolean;
  paper_balance: number;
  inverse_bot_enabled: boolean;
  inverse_bot_default_size_mode: string;
  inverse_bot_fixed_amount: number;
  inverse_bot_confidence_threshold: number;
  inverse_bot_cooldown_minutes: number;
  inverse_bot_max_reversals_per_day: number;
  updated_at: string | null;
}

/** Partial update — omitted fields are left untouched by the server. */
export interface AccountSettingsUpdate {
  ai_backend?: string;
  preferred_llm_provider?: string | null;
  preferred_llm_model?: string | null;
  copy_trading_enabled?: boolean;
  risk_mode?: string;
  max_position_size?: number;
  daily_loss_limit?: number;
  mirror_percentage?: number;
  fixed_trade_amount?: number;
  kelly_fraction?: number;
  require_ai_approval?: boolean;
  follow_email_notifications_enabled?: boolean;
  daily_digest_enabled?: boolean;
  monthly_loss_limit?: number | null;
  max_drawdown_pct?: number;
  total_loss_halt_pct?: number;
  dynamic_sizing_enabled?: boolean;
  simulation_mode?: boolean;
  paper_balance?: number;
  inverse_bot_enabled?: boolean;
  inverse_bot_default_size_mode?: string;
  inverse_bot_fixed_amount?: number;
  inverse_bot_confidence_threshold?: number;
  inverse_bot_cooldown_minutes?: number;
  inverse_bot_max_reversals_per_day?: number;
}

/** Details surfaced to the Accounts settings section. */
export interface AccountSettingsDetail {
  settings: AccountSettings;
  /** False when the sub-account already has its own row. */
  is_new: boolean;
}

// ── Active-account store ────────────────────────────────────

export const ACTIVE_ACCOUNT_STORAGE_KEY = "polymarket_active_account";
const ACTIVE_ACCOUNT_EVENT = "polymarket:active-account-changed";

function readStoredAccount(): Account | null {
  try {
    const raw = window.localStorage.getItem(ACTIVE_ACCOUNT_STORAGE_KEY);
    if (!raw) return null;
    const parsed = JSON.parse(raw) as Account;
    return parsed && typeof parsed.id === "number" ? parsed : null;
  } catch {
    return null;
  }
}

function writeStoredAccount(account: Account | null): void {
  try {
    if (account) {
      window.localStorage.setItem(ACTIVE_ACCOUNT_STORAGE_KEY, JSON.stringify(account));
    } else {
      window.localStorage.removeItem(ACTIVE_ACCOUNT_STORAGE_KEY);
    }
  } catch {
    // Private-mode / quota failures must not break account switching.
  }
  window.dispatchEvent(new CustomEvent<Account | null>(ACTIVE_ACCOUNT_EVENT, { detail: account }));
}

export function getActiveAccountSelection(): Account | null {
  return readStoredAccount();
}

/** Header value for the active account, or `undefined` for none. */
export function getActiveAccountHeader(): Record<string, string> {
  const account = readStoredAccount();
  return account ? { "X-Account-Id": String(account.id) } : {};
}

export function setActiveAccount(account: Account | null): void {
  writeStoredAccount(account);
}

export function clearActiveAccount(): void {
  writeStoredAccount(null);
}

/** Subscribe to account switches. Returns an unsubscribe function. */
export function onActiveAccountChange(listener: (account: Account | null) => void): () => void {
  const handler = (event: Event) => {
    listener((event as CustomEvent<Account | null>).detail ?? readStoredAccount());
  };
  window.addEventListener(ACTIVE_ACCOUNT_EVENT, handler);
  return () => window.removeEventListener(ACTIVE_ACCOUNT_EVENT, handler);
}

// ── Service ─────────────────────────────────────────────────

export const accountService = {
  /** Resolve the selection against the server so a stale id self-heals. */
  async getActiveAccount(): Promise<ActiveAccount> {
    return apiClient.get<ActiveAccount>("/api/accounts/active", {
      headers: getActiveAccountHeader(),
    });
  },

  async listAccounts(): Promise<Account[]> {
    return apiClient.get<Account[]>("/api/accounts", {
      headers: getActiveAccountHeader(),
    });
  },

  async createAccount(name: string): Promise<Account> {
    return apiClient.post<Account>(
      "/api/accounts",
      { name },
      { headers: getActiveAccountHeader() },
    );
  },

  async renameAccount(id: number, name: string): Promise<Account> {
    return apiClient.patch<Account>(
      `/api/accounts/${id}`,
      { name },
      {
        headers: getActiveAccountHeader(),
      },
    );
  },

  async getAccountSettings(id: number): Promise<AccountSettings> {
    return apiClient.get<AccountSettings>(`/api/accounts/${id}/settings`, {
      headers: getActiveAccountHeader(),
    });
  },

  async updateAccountSettings(id: number, update: AccountSettingsUpdate): Promise<AccountSettings> {
    return apiClient.patch<AccountSettings>(`/api/accounts/${id}/settings`, update, {
      headers: getActiveAccountHeader(),
    });
  },
};
