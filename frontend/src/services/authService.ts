/**
 * Wallet authentication endpoints under `/api/auth`.
 *
 * Covers the sign-in challenge, signature verification, private-key login, logout, and the
 * `GET /api/auth/me` session probe used to bootstrap auth state. Sessions are cookie-based, so
 * `keepLoggedIn` is passed through to the backend rather than stored client-side.
 *
 * @module services/authService
 */

import { apiClient } from "./apiClient";

export interface LoginChallenge {
  challenge_id: string;
  challenge: string;
  timestamp: string;
  nonce: string;
  message: string;
}

export interface TokenResponse {
  /**
   * Only returned by /api/auth/refresh. Login endpoints deliver
   * tokens via httpOnly cookies and omit them from the body.
   */
  access_token?: string;
  /** Only returned by /api/auth/refresh; see `access_token`. */
  refresh_token?: string;
  token_type: string;
  expires_in: number;
  wallet_address?: string;
}

export interface CurrentUserResponse {
  id: number;
  wallet_address: string;
  created_at: string;
  last_login: string | null;
  is_admin: boolean;
}

export const authService = {
  async getLoginChallenge(walletAddress: string): Promise<LoginChallenge> {
    return apiClient.post<LoginChallenge>("/api/auth/login", {
      wallet_address: walletAddress,
    });
  },

  async verifySignature(
    walletAddress: string,
    signature: string,
    keepLoggedIn: boolean = false,
    challengeId?: string,
  ): Promise<TokenResponse> {
    return apiClient.post<TokenResponse>("/api/auth/verify", {
      wallet_address: walletAddress,
      signature,
      challenge_id: challengeId,
      keep_logged_in: keepLoggedIn,
    });
  },

  async loginWithPrivateKey(
    privateKey: string,
    keepLoggedIn: boolean = false,
  ): Promise<TokenResponse> {
    return apiClient.post<TokenResponse>("/api/auth/login-with-key", {
      private_key: privateKey,
      keep_logged_in: keepLoggedIn,
    });
  },

  async logout(refreshToken?: string): Promise<void> {
    if (refreshToken) {
      await apiClient.post("/api/auth/logout", {
        refresh_token: refreshToken,
      });
      return;
    }
    await apiClient.post("/api/auth/logout", {});
  },

  async getCurrentUser(): Promise<CurrentUserResponse> {
    return apiClient.get<CurrentUserResponse>("/api/auth/me");
  },
};
