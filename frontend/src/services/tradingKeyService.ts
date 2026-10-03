/**
 * Trading-key endpoints under `/api/auth/trading-key`.
 *
 * Login is signature-only; the wallet private key is stored
 * separately through this explicit "enable auto-trading" step so
 * automated strategies (stop-loss, inverse bot, latency arb) can
 * sign orders. The key is encrypted at rest and never returned by
 * the API — only a boolean `has_trading_key` status is exposed.
 *
 * @module services/tradingKeyService
 */

import { apiClient } from "./apiClient";

export interface TradingKeyStatus {
  has_trading_key: boolean;
}

export interface TradingKeyResponse {
  ok: boolean;
}

export interface ConnectTradingKeyRequest {
  private_key: string;
  clob_credentials?: Record<string, string> | null;
}

/**
 * Check whether the session user has a trading key stored.
 */
export async function getTradingKeyStatus(): Promise<TradingKeyStatus> {
  return apiClient.get<TradingKeyStatus>("/api/auth/trading-key");
}

/**
 * Store the wallet private key (and optional CLOB credentials)
 * for the session user's own wallet.
 */
export async function connectTradingKey(
  privateKey: string,
  clobCredentials?: object,
): Promise<TradingKeyResponse> {
  return apiClient.post<TradingKeyResponse>("/api/auth/trading-key", {
    private_key: privateKey,
    clob_credentials: clobCredentials ?? null,
  });
}

/**
 * Delete the session user's stored trading key.
 */
export async function disconnectTradingKey(): Promise<TradingKeyResponse> {
  return apiClient.delete<TradingKeyResponse>("/api/auth/trading-key");
}

export const tradingKeyService = {
  getTradingKeyStatus,
  connectTradingKey,
  disconnectTradingKey,
};
