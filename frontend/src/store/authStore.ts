/**
 * Zustand store holding the client-side auth session (wallet address, admin flag, ready flag).
 *
 * Intentionally in-memory only and not persisted: `App.tsx` re-validates the session on every load
 * with `GET /api/auth/me` and sets `sessionReady` once that settles, which is what `ProtectedRoute`
 * waits on. `services/apiClient.ts` also calls `clearSession` when a token refresh fails.
 *
 * @module store/authStore
 */

import { create } from "zustand";

interface AuthState {
  walletAddress: string | null;
  isAuthenticated: boolean;
  isAdmin: boolean;
  sessionReady: boolean;
}

interface AuthActions {
  setSession: (session: { walletAddress: string; isAdmin: boolean }) => void;
  setSessionReady: (ready: boolean) => void;
  clearSession: () => void;
  logout: () => void;
}

type AuthStore = AuthState & AuthActions;

export const useAuthStore = create<AuthStore>()((set) => ({
  walletAddress: null,
  isAuthenticated: false,
  isAdmin: false,
  sessionReady: false,

  setSession: ({ walletAddress, isAdmin }) => {
    set({
      walletAddress,
      isAdmin,
      isAuthenticated: true,
    });
  },

  setSessionReady: (ready: boolean) => {
    set({ sessionReady: ready });
  },

  clearSession: () => {
    set({
      walletAddress: null,
      isAuthenticated: false,
      isAdmin: false,
    });
  },

  logout: () => {
    set({
      walletAddress: null,
      isAuthenticated: false,
      isAdmin: false,
    });
  },
}));
