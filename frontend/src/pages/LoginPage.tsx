import { useState } from "react";
import { useAuthStore } from "../store/authStore";
import { authService } from "../services/authService";
import LoginForm from "../components/LoginForm";
import { getApiErrorMessage } from "../utils/apiError";

export default function LoginPage() {
  const { setSession } = useAuthStore();
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

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
  ) => {
    setLoading(true);
    setError(null);

    try {
      await authService.verifySignature(
        walletAddress,
        signature,
        keepLoggedIn,
      );
      await restoreSession();
    } catch (err: unknown) {
      setError(getApiErrorMessage(err, "Authentication failed"));
    } finally {
      setLoading(false);
    }
  };

  const handlePrivateKeyLogin = async (
    privateKey: string,
    keepLoggedIn: boolean,
  ) => {
    setLoading(true);
    setError(null);

    try {
      await authService.loginWithPrivateKey(
        privateKey,
        keepLoggedIn,
      );
      await restoreSession();
    } catch (err: unknown) {
      setError(getApiErrorMessage(err, "Authentication failed"));
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className="app-shell min-h-screen flex items-center justify-center px-4">
      <div className="w-full max-w-md p-8 surface-panel page-enter">
        <h1 className="text-3xl font-extrabold text-center mb-2 theme-brand">
          Polymarket AI
        </h1>
        <p className="text-center theme-subtitle mb-8">
          AI-Powered Trading Automation
        </p>

        {error && (
          <div className="mb-4 p-4 alert-error rounded">
            {error}
          </div>
        )}

        <LoginForm
          onLogin={handleLogin}
          onPrivateKeyLogin={handlePrivateKeyLogin}
          loading={loading}
        />

        <p className="mt-6 text-center text-muted text-sm">
          Sign in with your Ethereum wallet or private key
        </p>
      </div>
    </div>
  );
}
