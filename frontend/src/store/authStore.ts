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

/**
 * Clear the Workbox `api-cache` (see vite.config.ts) so authenticated API
 * responses stored by the service worker are not served to the next user of
 * the same device after logout. Fire-and-forget: a cache failure must never
 * block the logout path.
 */
function clearAuthenticatedApiCache(): void {
  if (typeof window === "undefined" || !("caches" in window)) return;
  void (async () => {
    try {
      const keys = await window.caches.keys();
      await Promise.all(
        keys.filter((key) => key.startsWith("api-")).map((key) => window.caches.delete(key)),
      );
    } catch {
      // Cache cleanup is best-effort; never surface it to the user.
    }
  })();
}

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
    clearAuthenticatedApiCache();
    set({
      walletAddress: null,
      isAuthenticated: false,
      isAdmin: false,
    });
  },

  logout: () => {
    clearAuthenticatedApiCache();
    set({
      walletAddress: null,
      isAuthenticated: false,
      isAdmin: false,
    });
  },
}));
