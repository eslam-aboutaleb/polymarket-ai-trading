/**
 * Login modal shell that overlays public pages, driven by `LoginModalContext`.
 *
 * Wraps `LoginForm` and performs the same signature-only authentication flow as the
 * standalone login page: verify the wallet signature, reload the current user into
 * `authStore`, then close the modal. Renders `null` while closed. Errors from any
 * auth attempt surface above the form.
 *
 * @module components/LoginModal
 */
import { useState } from "react";
import { useAuthStore } from "../store/authStore";
import { authService } from "../services/authService";
import { useLoginModal } from "../context/LoginModalContext";
import LoginForm from "./LoginForm";
import { getApiErrorMessage } from "../utils/apiError";

/**
 * Login modal that overlays on top of public pages.
 * Wraps the existing LoginForm in a modal shell and handles
 * authentication the same way LoginPage does.
 */
export default function LoginModal() {
  const { isOpen, closeLoginModal } = useLoginModal();
  const { setSession } = useAuthStore();
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  if (!isOpen) return null;

  const restoreSession = async () => {
    const user = await authService.getCurrentUser();
    setSession({
      walletAddress: user.wallet_address,
      isAdmin: user.is_admin,
    });
  };

  const handleLogin = async (
    walletAddress: string,
    signature: string,
    keepLoggedIn: boolean,
    challengeId: string | null,
  ) => {
    setLoading(true);
    setError(null);

    try {
      await authService.verifySignature(
        walletAddress,
        signature,
        keepLoggedIn,
        challengeId ?? undefined,
      );
      await restoreSession();
      closeLoginModal();
    } catch (err: unknown) {
      setError(getApiErrorMessage(err, "Authentication failed"));
    } finally {
      setLoading(false);
    }
  };

  return (
    <div
      className="fixed inset-0 bg-black/70 flex items-center justify-center z-[100] p-4"
      onClick={closeLoginModal}
    >
      <div
        className="surface-panel w-full max-w-md p-0 overflow-hidden"
        onClick={(e) => e.stopPropagation()}
      >
        {/* Header */}
        <div className="flex items-center justify-between px-6 pt-5 pb-2">
          <div>
            <h2 className="text-xl font-bold text-white">Connect Wallet</h2>
            <p className="text-sm text-soft mt-0.5">Sign in to access trading features</p>
          </div>
          <button
            onClick={closeLoginModal}
            className="p-1.5 rounded-lg text-soft hover:bg-[var(--bg-soft)] transition"
            aria-label="Close login modal"
          >
            <svg className="w-5 h-5" fill="none" viewBox="0 0 24 24" stroke="currentColor">
              <path
                strokeLinecap="round"
                strokeLinejoin="round"
                strokeWidth={2}
                d="M6 18L18 6M6 6l12 12"
              />
            </svg>
          </button>
        </div>

        {/* Error */}
        {error && <div className="mx-6 mt-2 p-3 alert-error rounded text-sm">{error}</div>}

        {/* Login Form */}
        <div className="px-6 pb-6 pt-2">
          <LoginForm onLogin={handleLogin} loading={loading} />
        </div>
      </div>
    </div>
  );
}
