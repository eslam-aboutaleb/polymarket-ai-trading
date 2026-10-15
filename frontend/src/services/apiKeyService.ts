/**
 * @module apiKeyService
 * Client for the external-API key management surface (plan 06 Phase B).
 *
 * Keys mint credentials for `/api/v1/external/*`, which is how a
 * non-browser client authenticates: that namespace accepts an
 * `X-API-Key` header only, never the cookie or bearer token the rest
 * of the app uses. Scopes are `read` and `trade`; there is no
 * `withdraw` scope, so a key can never move funds the UI could not.
 *
 * These routes live under `/api/settings/*` and are browser-authenticated
 * like every other settings route — only the *use* of a key happens on
 * the external namespace.
 */

import { apiClient } from "./apiClient";

export type ApiKeyScope = "read" | "trade";

export interface ApiKey {
  id: number;
  name: string;
  key_prefix: string;
  account_id: number | null;
  scopes: ApiKeyScope[];
  expires_at: string | null;
  last_used_at: string | null;
  revoked_at: string | null;
  created_at: string | null;
  revoked: boolean;
}

export interface ApiKeyCreated extends ApiKey {
  /** Returned exactly once, at creation. Never re-derivable afterwards. */
  secret: string;
}

export interface ApiKeyCreateRequest {
  name: string;
  account_id?: number | null;
  scopes?: ApiKeyScope[];
  expires_in_days?: number | null;
}

const BASE = "/api/settings/api-keys";

export const apiKeyService = {
  /** The caller's keys. Digests and secrets are never included. */
  async list(): Promise<ApiKey[]> {
    return apiClient.get<ApiKey[]>(BASE);
  },

  /**
   * Mint a key. The returned `secret` is shown once and never again:
   * only its HMAC digest is persisted, so treat this response as the
   * single chance to store the secret.
   */
  async create(body: ApiKeyCreateRequest): Promise<ApiKeyCreated> {
    return apiClient.post<ApiKeyCreated>(BASE, body);
  },

  /** Revoke immediately. Revocation is immediate and final. */
  async revoke(keyId: number): Promise<ApiKey> {
    return apiClient.delete<ApiKey>(`${BASE}/${keyId}`);
  },
};
