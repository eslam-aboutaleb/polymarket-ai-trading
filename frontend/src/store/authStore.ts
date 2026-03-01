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
