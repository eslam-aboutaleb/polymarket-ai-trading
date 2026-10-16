/**
 * Shared logout sequence: drop the server session, then the local one.
 *
 * `POST /api/auth/logout` may fail (expired cookie, offline), but the session
 * must be cleared regardless — a failed logout must never become an unhandled
 * rejection or leave a confirm modal stuck open.
 *
 * @module hooks/useLogout
 */

import { useCallback } from "react";
import { authService } from "../services/authService";
import { useAuthStore } from "../store/authStore";

/** Returns the logout handler to pass to `Navigation onLogout`. */
export function useLogout(): () => Promise<void> {
  const clearSession = useAuthStore((state) => state.clearSession);

  return useCallback(async () => {
    try {
      await authService.logout();
    } catch (err) {
      // The server-side cookie may fail to clear, but the session is dropped
      // below regardless — a failed logout must not become an unhandled
      // rejection or leave the confirm modal stuck open.
      console.warn("Logout request failed; clearing session anyway", err);
    } finally {
      clearSession();
    }
  }, [clearSession]);
}
