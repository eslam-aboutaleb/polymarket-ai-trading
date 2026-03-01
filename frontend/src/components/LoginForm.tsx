import { useCallback, useEffect, useRef, useState } from "react";
import { useForm } from "react-hook-form";
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
  ) => Promise<void> | void;
  onPrivateKeyLogin?: (privateKey: string, keepLoggedIn: boolean) => void;
  loading: boolean;
}

interface PrivateKeyFormInputs {
  privateKey: string;
  keepLoggedIn: boolean;
  acknowledgeRisk: boolean;
}

type LoginMethod = "wallet" | "privateKey";
type WalletStep = "connect" | "ready" | "sign";

const walletConnectEnabled = Boolean(
  import.meta.env.VITE_WALLETCONNECT_PROJECT_ID?.trim(),
);

function walletProviderLabel(providerType: WalletProviderType): string {
  if (providerType === "walletconnect") {
    return "WalletConnect";
  }
  return "Browser Wallet";
}

export default function LoginForm({
  onLogin,
  onPrivateKeyLogin,
  loading,
}: LoginFormProps) {
  const [loginMethod, setLoginMethod] = useState<LoginMethod>("wallet");
  const [walletStep, setWalletStep] = useState<WalletStep>("connect");
  const [walletProviderType, setWalletProviderType] =
    useState<WalletProviderType>("injected");
  const [walletSession, setWalletSession] = useState<WalletSession | null>(null);
  const [challenge, setChallenge] = useState<string | null>(null);
  const [keepLoggedIn, setKeepLoggedIn] = useState(false);
  const [signingError, setSigningError] = useState<string | null>(null);
  const [walletActionLoading, setWalletActionLoading] = useState(false);
  const latestWalletSessionRef = useRef<WalletSession | null>(null);

  const {
    register: registerPK,
    handleSubmit: handleSubmitPK,
    watch: watchPK,
    formState: { errors: errorsPK },
  } = useForm<PrivateKeyFormInputs>();

  const watchedPrivateKey = watchPK("privateKey");
  const watchedKeepLoggedInPK = watchPK("keepLoggedIn");
  const watchedAcknowledgeRisk = watchPK("acknowledgeRisk");

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

  const clearWalletSession = useCallback(
    async (disconnect: boolean) => {
      const session = latestWalletSessionRef.current;

      setWalletSession(null);
      setWalletStep("connect");
      setChallenge(null);
      setKeepLoggedIn(false);

      if (disconnect && session?.providerType === "walletconnect") {
        try {
          await session.disconnect();
        } catch {
          // Ignore disconnect errors during reset.
        }
      }
    },
    [],
  );

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
  }, [
    watchedPrivateKey,
    watchedKeepLoggedInPK,
    watchedAcknowledgeRisk,
    loginMethod,
    walletProviderType,
  ]);

  const onSubmitPrivateKey = async (data: PrivateKeyFormInputs) => {
    if (onPrivateKeyLogin) {
      onPrivateKeyLogin(data.privateKey, data.keepLoggedIn);
    }
  };

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
      setWalletStep("sign");
    } catch (error: unknown) {
      setSigningError(
        getApiErrorMessage(error, "Failed to generate challenge for wallet"),
      );
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
      await onLogin(walletSession.address, signature, keepLoggedIn);
    } catch (error: unknown) {
      setSigningError(getApiErrorMessage(error, "Failed to sign message"));
    } finally {
      setWalletActionLoading(false);
    }
  };

  const isWalletBusy = loading || walletActionLoading;

  const renderTabs = () => (
    <div className="flex mb-4 border-b border-[var(--line)]">
      <button
        type="button"
        onClick={() => {
          setLoginMethod("wallet");
          setSigningError(null);
        }}
        className={`tab-toggle flex-1 py-2 px-4 text-sm font-semibold ${
          loginMethod === "wallet" ? "tab-toggle-active" : ""
        }`}
      >
        Wallet
      </button>
      <button
        type="button"
        onClick={() => {
          setLoginMethod("privateKey");
          setSigningError(null);
          void clearWalletSession(true);
        }}
        className={`tab-toggle flex-1 py-2 px-4 text-sm font-semibold ${
          loginMethod === "privateKey" ? "tab-toggle-active" : ""
        }`}
      >
        Private Key
      </button>
    </div>
  );

  const renderPrivateKeyForm = () => (
    <form onSubmit={handleSubmitPK(onSubmitPrivateKey)} className="space-y-4">
      <div className="p-3 rounded text-sm chip chip-warning">
        Only use this on trusted devices. Never share your private key.
      </div>

      <div>
        <label className="block text-sm font-medium mb-2 text-soft">
          Private Key
        </label>
        <input
          type="password"
          autoComplete="off"
          placeholder="0x... or raw hex"
          {...registerPK("privateKey", {
            required: "Private key is required",
            pattern: {
              value: /^(0x)?[a-fA-F0-9]{64}$/,
              message:
                "Invalid private key format (expected 64 hex characters)",
            },
          })}
          className="input-theme mono"
        />
        {errorsPK.privateKey && (
          <p className="mt-1 status-bad text-sm">
            {errorsPK.privateKey.message}
          </p>
        )}
      </div>

      <div className="flex items-start gap-2">
        <input
          type="checkbox"
          id="acknowledgeRisk"
          className="w-4 h-4 mt-0.5 cursor-pointer accent-[var(--accent)]"
          {...registerPK("acknowledgeRisk", {
            required: "You must acknowledge the private key risk to continue",
          })}
        />
        <label htmlFor="acknowledgeRisk" className="text-sm text-soft cursor-pointer">
          I understand that entering a private key can expose my funds if this
          device or network is compromised.
        </label>
      </div>
      {errorsPK.acknowledgeRisk && (
        <p className="mt-1 status-bad text-sm">
          {errorsPK.acknowledgeRisk.message}
        </p>
      )}

      <div className="flex items-center">
        <input
          type="checkbox"
          id="keepLoggedInPK"
          {...registerPK("keepLoggedIn")}
          className="w-4 h-4 cursor-pointer accent-[var(--accent)]"
        />
        <label
          htmlFor="keepLoggedInPK"
          className="ml-2 text-sm text-soft cursor-pointer"
        >
          Keep me logged in (90 days)
        </label>
      </div>

      {signingError && (
        <div className="p-3 alert-error rounded text-sm">
          {signingError}
        </div>
      )}

      <button type="submit" disabled={loading} className="w-full btn-accent">
        {loading ? "Logging in..." : "Login with Private Key"}
      </button>
    </form>
  );

  const renderWalletConnectStep = () => (
    <div className="space-y-4">
      <div>
        <p className="text-sm text-soft mb-2">
          Choose how you want to connect your wallet.
        </p>
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
          WalletConnect disabled: set `VITE_WALLETCONNECT_PROJECT_ID` in
          frontend env to enable QR wallet connections.
        </div>
      )}

      {signingError && (
        <div className="p-3 alert-error rounded text-sm">
          {signingError}
        </div>
      )}

      <button
        type="button"
        onClick={handleConnectWallet}
        disabled={
          isWalletBusy ||
          (walletProviderType === "walletconnect" && !walletConnectEnabled)
        }
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
        <p className="mono text-xs break-all">
          {walletSession?.address}
        </p>
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
        <label
          htmlFor="keepLoggedInWallet"
          className="ml-2 text-sm text-soft cursor-pointer"
        >
          Keep me logged in (90 days)
        </label>
      </div>

      {signingError && (
        <div className="p-3 alert-error rounded text-sm">
          {signingError}
        </div>
      )}

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
        <p className="mono text-xs break-all mb-3">
          {walletSession?.address}
        </p>
        <p className="text-soft mb-2">Challenge:</p>
        <p className="break-words mono text-xs">
          {challenge}
        </p>
      </div>

      {signingError && (
        <div className="p-3 alert-error rounded text-sm">
          {signingError}
        </div>
      )}

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

  return (
    <div>
      {renderTabs()}
      {loginMethod === "privateKey" && renderPrivateKeyForm()}
      {loginMethod === "wallet" && renderWalletFlow()}
    </div>
  );
}
