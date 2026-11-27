/**
 * Mounts the WebMCP registry for the signed-in session.
 *
 * Renders nothing. Re-syncs on session, active-account and opt-in changes and
 * tears everything down on unmount (including when an error boundary above
 * unmounts the tree).
 *
 * @module integrations/webmcp/WebMcpController
 */

import { useEffect, useRef } from "react";
import { useAuthStore } from "../../store/authStore";
import { getActiveAccountSelection, onActiveAccountChange } from "../../services/accountService";
import { WebMcpRegistry, detectModelContext } from "./registry";
import { isOptedIn, useWebMcpStore } from "./store";

export default function WebMcpController() {
  const walletAddress = useAuthStore((s) => s.walletAddress);
  const isAuthenticated = useAuthStore((s) => s.isAuthenticated);
  const sessionReady = useAuthStore((s) => s.sessionReady);
  const optInVersion = useWebMcpStore((s) => s.optInVersion);
  const registryRef = useRef<WebMcpRegistry | null>(null);

  if (registryRef.current === null) {
    registryRef.current = new WebMcpRegistry({
      getModelContext: detectModelContext,
      now: () => Date.now(),
      origin: typeof window === "undefined" ? "" : window.location.origin,
      onCall: (entry) => useWebMcpStore.getState().recordCall(entry),
      getSessionKey: () => {
        const auth = useAuthStore.getState();
        if (!auth.isAuthenticated || !auth.walletAddress) return null;
        return {
          walletAddress: auth.walletAddress,
          accountId: getActiveAccountSelection()?.id ?? null,
        };
      },
    });
  }

  useEffect(() => {
    const registry = registryRef.current!;
    const apply = () => {
      const { walletAddress: wallet, isAuthenticated: authed } = useAuthStore.getState();
      const decision = registry.sync(authed && isOptedIn(wallet));
      useWebMcpStore
        .getState()
        .setStatus(decision.state, decision.state === "active" ? decision.tools : []);
    };
    if (!sessionReady) {
      registry.teardown();
      useWebMcpStore.getState().setStatus("signed_out");
      return undefined;
    }
    apply();
    // Account switches change the key without touching the auth store.
    const off = onActiveAccountChange(apply);
    return off;
  }, [walletAddress, isAuthenticated, sessionReady, optInVersion]);

  useEffect(
    () => () => {
      registryRef.current?.teardown();
      useWebMcpStore.getState().setStatus("signed_out");
    },
    [],
  );

  return null;
}
