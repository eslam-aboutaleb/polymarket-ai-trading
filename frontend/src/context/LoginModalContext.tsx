/**
 * Context that lets any component open or close the wallet-login modal from outside the router.
 *
 * `LoginModalProvider` wraps the whole app in `App.tsx`; `useLoginModal()` exposes `isOpen`,
 * `openLoginModal`, and `closeLoginModal`. Paired with `hooks/useRequireAuth.ts`, which opens the
 * modal when a guarded action is triggered while signed out.
 *
 * @module context/LoginModalContext
 */

import { createContext, useCallback, useContext, useRef, useState, type ReactNode } from "react";

interface LoginModalContextType {
  /** Whether the login modal is currently open */
  isOpen: boolean;
  /** Open the login modal */
  openLoginModal: () => void;
  /** Close the login modal */
  closeLoginModal: () => void;
  /** Queue a callback to run once after the next successful login */
  queueLoginAction: (pendingAction: () => void) => void;
  /** Drop the queued callback without running it */
  clearLoginAction: () => void;
  /** Run the queued callback once and clear it; a no-op when nothing is queued */
  drainLoginAction: () => void;
}

const LoginModalContext = createContext<LoginModalContextType>({
  isOpen: false,
  openLoginModal: () => {},
  closeLoginModal: () => {},
  queueLoginAction: () => {},
  clearLoginAction: () => {},
  drainLoginAction: () => {},
});

export function LoginModalProvider({ children }: { children: ReactNode }) {
  const [isOpen, setIsOpen] = useState(false);
  const pendingActionRef = useRef<(() => void) | null>(null);

  const openLoginModal = useCallback(() => setIsOpen(true), []);
  const closeLoginModal = useCallback(() => setIsOpen(false), []);

  const queueLoginAction = useCallback((pendingAction: () => void) => {
    pendingActionRef.current = pendingAction;
  }, []);

  const clearLoginAction = useCallback(() => {
    pendingActionRef.current = null;
  }, []);

  const drainLoginAction = useCallback(() => {
    const pendingAction = pendingActionRef.current;
    pendingActionRef.current = null;
    pendingAction?.();
  }, []);

  return (
    <LoginModalContext.Provider
      value={{
        isOpen,
        openLoginModal,
        closeLoginModal,
        queueLoginAction,
        clearLoginAction,
        drainLoginAction,
      }}
    >
      {children}
    </LoginModalContext.Provider>
  );
}

export function useLoginModal() {
  return useContext(LoginModalContext);
}
