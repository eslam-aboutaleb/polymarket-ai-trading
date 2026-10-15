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
import { Banner, Modal } from "./ui";

/**
 * Login modal that overlays on top of public pages.
 * Wraps the existing LoginForm in a modal shell and handles
 * authentication the same way LoginPage does.
 */
export default function LoginModal() {
  const { isOpen, closeLoginModal, clearLoginAction, drainLoginAction } = useLoginModal();
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
      drainLoginAction();
    } catch (err: unknown) {
      setError(getApiErrorMessage(err, "Authentication failed"));
    } finally {
      setLoading(false);
    }
  };

  return (
    <Modal
      open={isOpen}
      onClose={() => {
        clearLoginAction();
        closeLoginModal();
      }}
      title="Connect Wallet"
      size="md"
      zLevel="login"
    >
      <p className="text-sm text-soft">Sign in to access trading features</p>

      {error && (
        <div className="mt-3">
          <Banner variant="error" onDismiss={() => setError(null)}>
            {error}
          </Banner>
        </div>
      )}

      <div className="mt-3">
        <LoginForm onLogin={handleLogin} loading={loading} />
      </div>
    </Modal>
  );
}
