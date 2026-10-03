/**
 * Hook returning a `requireAuth` guard that wraps protected actions on public pages.
 *
 * The wrapped callback runs immediately when a session exists; otherwise the login modal opens and
 * the action is dropped. Reads auth state from `store/authStore` and opens the modal via
 * `context/LoginModalContext`.
 *
 * @module hooks/useRequireAuth
 */

import { useCallback } from "react";
import { useAuthStore } from "../store/authStore";
import { useLoginModal } from "../context/LoginModalContext";

/**
 * Hook that provides a guard function for protected actions on public pages.
 *
 * Usage:
 *   const { requireAuth } = useRequireAuth();
 *   <button onClick={() => requireAuth(() => openTradeModal(market))}>Trade</button>
 *
 * If the user is authenticated, the callback runs immediately.
 * Otherwise, the login modal opens.
 */
export function useRequireAuth() {
  const { isAuthenticated } = useAuthStore();
  const { openLoginModal } = useLoginModal();

  const requireAuth = useCallback(
    (callback?: () => void) => {
      if (isAuthenticated) {
        callback?.();
      } else {
        openLoginModal();
      }
    },
    [isAuthenticated, openLoginModal],
  );

  return { isAuthenticated, requireAuth };
}
