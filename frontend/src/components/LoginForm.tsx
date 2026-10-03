/**
 * Wallet sign-in UI.
 *
 * Runs a connect -> ready -> sign flow: `walletConnector` opens the session, a
 * challenge is fetched from `authService.getLoginChallenge`, and the signed
 * message is handed to `onLogin`. WalletConnect is only offered when
 * `VITE_WALLETCONNECT_PROJECT_ID` is set. Login is signature-only; the
 * private-key login tab was removed — trading keys are now stored separately
 * via the "enable auto-trading" step in Settings.
 *
 * @module components/LoginForm
 */
import { useCallback, useEffect, useRef, useState } from "react";
import { authService } from "../services/authService";
import {
  connectInjectedWallet,
  connectWalletConnect,
  MISSING_WALLETCONNECT_PROJECT_ID_MESSAGE,
  type WalletProviderType,
  type WalletSession,
} from "../services/walletConnector";
import { getApiErrorMessage } from "../utils/apiError";

interface LoginFormProps {
  onLogin: (
    walletAddress: string,
    signature: string,
    keepLoggedIn: boolean,
    challengeId: string | null,
  ) => Promise<void> | void;
  loading: boolean;
}

type WalletStep = "connect" | "ready" | "sign";

const walletConnectEnabled = Boolean(import.meta.env.VITE_WALLETCONNECT_PROJECT_ID?.trim());

function walletProviderLabel(providerType: WalletProviderType): string {
  if (providerType === "walletconnect") {
    return "WalletConnect";
  }
  return "Browser Wallet";
}

export default function LoginForm({ onLogin, loading }: LoginFormProps) {
  const [walletStep, setWalletStep] = useState<WalletStep>("connect");
  const [walletProviderType, setWalletProviderType] = useState<WalletProviderType>("injected");
  const [walletSession, setWalletSession] = useState<WalletSession | null>(null);
  const [challenge, setChallenge] = useState<string | null>(null);
  const [challengeId, setChallengeId] = useState<string | null>(null);
  const [keepLoggedIn, setKeepLoggedIn] = useState(false);
  const [signingError, setSigningError] = useState<string | null>(null);
  const [walletActionLoading, setWalletActionLoading] = useState(false);
  const latestWalletSessionRef = useRef<WalletSession | null>(null);

  useEffect(() => {
    latestWalletSessionRef.current = walletSession;
  }, [walletSession]);

  useEffect(
    () => () => {
      const session = latestWalletSessionRef.current;
      if (session?.providerType === "walletconnect") {
        void session.disconnect();
      }
    },
    [],
  );

  const clearWalletSession = useCallback(async (disconnect: boolean) => {
    const session = latestWalletSessionRef.current;

    setWalletSession(null);
    setWalletStep("connect");
    setChallenge(null);
    setChallengeId(null);
    setKeepLoggedIn(false);

    if (disconnect && session?.providerType === "walletconnect") {
      try {
        await session.disconnect();
      } catch {
        // Ignore disconnect errors during reset.
      }
    }
  }, []);

  useEffect(() => {
    if (!walletSession) {
      return;
    }

    const unsubscribeAccounts = walletSession.onAccountsChanged((accounts) => {
      const nextAddress = accounts[0]?.toLowerCase();
      if (!nextAddress || !/^0x[a-f0-9]{40}$/.test(nextAddress)) {
        setSigningError("Wallet disconnected. Please connect again.");
        void clearWalletSession(true);
        return;
      }

      if (nextAddress !== walletSession.address) {
        setSigningError("Wallet account changed. Please reconnect and sign again.");
        void clearWalletSession(true);
      }
    });

    const unsubscribeDisconnect = walletSession.onDisconnect(() => {
      setSigningError("Wallet disconnected. Please connect again.");
      void clearWalletSession(false);
    });

    return () => {
      unsubscribeAccounts();
      unsubscribeDisconnect();
    };
  }, [clearWalletSession, walletSession]);

  useEffect(() => {
    if (signingError) {
      setSigningError(null);
    }
  }, [walletProviderType]);

  const handleConnectWallet = async () => {
    try {
      setSigningError(null);
      setWalletActionLoading(true);

      await clearWalletSession(true);

      if (walletProviderType === "walletconnect" && !walletConnectEnabled) {
        throw new Error(MISSING_WALLETCONNECT_PROJECT_ID_MESSAGE);
      }

      const session =
        walletProviderType === "walletconnect"
          ? await connectWalletConnect()
          : await connectInjectedWallet();

      setWalletSession(session);
      setWalletStep("ready");
    } catch (error: unknown) {
      setSigningError(getApiErrorMessage(error, "Failed to connect wallet"));
    } finally {
      setWalletActionLoading(false);
    }
  };

  const handlePrepareChallenge = async () => {
    if (!walletSession) {
      return;
    }

    try {
      setSigningError(null);
      setWalletActionLoading(true);

      const result = await authService.getLoginChallenge(walletSession.address);
      setChallenge(result.challenge);
      setChallengeId(result.challenge_id);
      setWalletStep("sign");
    } catch (error: unknown) {
      setSigningError(getApiErrorMessage(error, "Failed to generate challenge for wallet"));
    } finally {
      setWalletActionLoading(false);
    }
  };

  const handleSignMessage = async () => {
    if (!walletSession || !challenge) {
      return;
    }

    try {
      setSigningError(null);
      setWalletActionLoading(true);

      const signature = await walletSession.signMessage(challenge);
      await onLogin(walletSession.address, signature, keepLoggedIn, challengeId);
    } catch (error: unknown) {
      setSigningError(getApiErrorMessage(error, "Failed to sign message"));
    } finally {
      setWalletActionLoading(false);
    }
  };

  const isWalletBusy = loading || walletActionLoading;

  const renderWalletConnectStep = () => (
    <div className="space-y-4">
      <div>
        <p className="text-sm text-soft mb-2">Choose how you want to connect your wallet.</p>
        <div className="grid grid-cols-2 gap-3">
          <button
            type="button"
            onClick={() => setWalletProviderType("injected")}
            className={`btn-muted text-sm ${
              walletProviderType === "injected" ? "tab-toggle-active" : ""
            }`}
          >
            Browser Wallet
          </button>
          <button
            type="button"
            onClick={() => setWalletProviderType("walletconnect")}
            className={`btn-muted text-sm ${
              walletProviderType === "walletconnect" ? "tab-toggle-active" : ""
            }`}
            disabled={!walletConnectEnabled}
          >
            WalletConnect
          </button>
        </div>
      </div>

      {!walletConnectEnabled && (
        <div className="p-3 alert-error rounded text-sm">
          WalletConnect disabled: set `VITE_WALLETCONNECT_PROJECT_ID` in frontend env to enable QR
          wallet connections.
        </div>
      )}

      {signingError && <div className="p-3 alert-error rounded text-sm">{signingError}</div>}

      <button
        type="button"
        onClick={handleConnectWallet}
        disabled={isWalletBusy || (walletProviderType === "walletconnect" && !walletConnectEnabled)}
        className="w-full btn-accent"
      >
        {isWalletBusy ? "Connecting..." : "Connect Wallet"}
      </button>
    </div>
  );

  const renderWalletReadyStep = () => (
    <div className="space-y-4">
      <div className="p-4 surface-soft text-sm">
        <p className="text-soft mb-2">Connected Wallet</p>
        <p className="mono text-xs break-all">{walletSession?.address}</p>
        <p className="text-soft mt-2">
          Provider: {walletSession ? walletProviderLabel(walletSession.providerType) : ""}
        </p>
      </div>

      <div className="flex items-center">
        <input
          type="checkbox"
          id="keepLoggedInWallet"
          checked={keepLoggedIn}
          onChange={(event) => setKeepLoggedIn(event.target.checked)}
          className="w-4 h-4 cursor-pointer accent-[var(--accent)]"
        />
        <label htmlFor="keepLoggedInWallet" className="ml-2 text-sm text-soft cursor-pointer">
          Keep me logged in (90 days)
        </label>
      </div>

      {signingError && <div className="p-3 alert-error rounded text-sm">{signingError}</div>}

      <div className="flex gap-3">
        <button
          type="button"
          onClick={() => void clearWalletSession(true)}
          disabled={isWalletBusy}
          className="flex-1 btn-muted"
        >
          Change Wallet
        </button>
        <button
          type="button"
          onClick={handlePrepareChallenge}
          disabled={isWalletBusy || !walletSession}
          className="flex-1 btn-accent"
        >
          {isWalletBusy ? "Preparing..." : "Continue to Sign"}
        </button>
      </div>
    </div>
  );

  const renderWalletSignStep = () => (
    <div className="space-y-4">
      <div className="p-4 surface-soft text-sm">
        <p className="text-soft mb-2">Signing with</p>
        <p className="mono text-xs break-all mb-3">{walletSession?.address}</p>
        <p className="text-soft mb-2">Challenge:</p>
        <p className="break-words mono text-xs">{challenge}</p>
      </div>

      {signingError && <div className="p-3 alert-error rounded text-sm">{signingError}</div>}

      <div className="flex gap-3">
        <button
          type="button"
          onClick={() => void clearWalletSession(true)}
          disabled={isWalletBusy}
          className="flex-1 btn-muted"
        >
          Cancel
        </button>
        <button
          type="button"
          onClick={handleSignMessage}
          disabled={isWalletBusy || !challenge || !walletSession}
          className="flex-1 btn-accent"
        >
          {isWalletBusy ? "Signing..." : "Sign Message"}
        </button>
      </div>
    </div>
  );

  const renderWalletFlow = () => {
    if (walletStep === "connect") {
      return renderWalletConnectStep();
    }
    if (walletStep === "ready") {
      return renderWalletReadyStep();
    }
    return renderWalletSignStep();
  };

  return <div>{renderWalletFlow()}</div>;
}
