/**
 * Context that lets any component open or close the wallet-login modal from outside the router.
 *
 * `LoginModalProvider` wraps the whole app in `App.tsx`; `useLoginModal()` exposes `isOpen`,
 * `openLoginModal`, and `closeLoginModal`. Paired with `hooks/useRequireAuth.ts`, which opens the
 * modal when a guarded action is triggered while signed out.
 *
 * @module context/LoginModalContext
 */

import { createContext, useCallback, useContext, useState, type ReactNode } from "react";

interface LoginModalContextType {
  /** Whether the login modal is currently open */
  isOpen: boolean;
  /** Open the login modal */
  openLoginModal: () => void;
  /** Close the login modal */
  closeLoginModal: () => void;
}

const LoginModalContext = createContext<LoginModalContextType>({
  isOpen: false,
  openLoginModal: () => {},
  closeLoginModal: () => {},
});

export function LoginModalProvider({ children }: { children: ReactNode }) {
  const [isOpen, setIsOpen] = useState(false);

  const openLoginModal = useCallback(() => setIsOpen(true), []);
  const closeLoginModal = useCallback(() => setIsOpen(false), []);

  return (
    <LoginModalContext.Provider value={{ isOpen, openLoginModal, closeLoginModal }}>
      {children}
    </LoginModalContext.Provider>
  );
}

export function useLoginModal() {
  return useContext(LoginModalContext);
}
