import { apiClient } from "./apiClient";

export interface LoginChallenge {
  challenge: string;
  timestamp: string;
  nonce: string;
  message: string;
}

export interface TokenResponse {
  access_token: string;
  refresh_token: string;
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
  ): Promise<TokenResponse> {
    return apiClient.post<TokenResponse>("/api/auth/verify", {
      wallet_address: walletAddress,
      signature,
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
