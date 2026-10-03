/**
 * Browser wallet connection layer for EIP-1193 providers (injected wallets and WalletConnect).
 *
 * Exports `connectInjectedWallet` and `connectWalletConnect`, both returning a normalized
 * `WalletSession` with `personal_sign`, disconnect, and account/disconnect subscriptions. WalletConnect
 * targets Polygon chain 137 and requires `VITE_WALLETCONNECT_PROJECT_ID`. Provider rejections
 * (EIP-1193 code 4001) are normalised into a single user-facing message, and failed WalletConnect
 * attempts disconnect the provider before rethrowing.
 *
 * @module services/walletConnector
 */

import EthereumProvider from "@walletconnect/ethereum-provider";

export type WalletProviderType = "injected" | "walletconnect";

type ProviderEventHandler = (...args: unknown[]) => void;

interface Eip1193Provider {
  request: (args: { method: string; params?: unknown[] }) => Promise<unknown>;
  on?: (event: string, handler: ProviderEventHandler) => void;
  removeListener?: (event: string, handler: ProviderEventHandler) => void;
  disconnect?: () => Promise<void> | void;
  providers?: Eip1193Provider[];
  isMetaMask?: boolean;
}

declare global {
  interface Window {
    ethereum?: Eip1193Provider;
  }
}

export interface WalletSession {
  address: string;
  providerType: WalletProviderType;
  signMessage: (message: string) => Promise<string>;
  disconnect: () => Promise<void>;
  onAccountsChanged: (handler: (accounts: string[]) => void) => () => void;
  onDisconnect: (handler: () => void) => () => void;
}

export const NO_INJECTED_WALLET_MESSAGE =
  "No browser wallet detected. Install MetaMask/Rabby or use WalletConnect.";
export const MISSING_WALLETCONNECT_PROJECT_ID_MESSAGE =
  "WalletConnect is not configured. Set VITE_WALLETCONNECT_PROJECT_ID.";
export const WALLET_REQUEST_REJECTED_MESSAGE = "Request rejected in wallet.";

function normalizeAddress(address: string): string {
  return address.trim().toLowerCase();
}

function asStringArray(value: unknown): string[] {
  if (!Array.isArray(value)) {
    return [];
  }
  return value.filter((item): item is string => typeof item === "string");
}

function pickAddress(accountsLike: unknown): string | null {
  const accounts = asStringArray(accountsLike);
  if (accounts.length === 0) {
    return null;
  }
  const candidate = normalizeAddress(accounts[0]);
  return /^0x[a-f0-9]{40}$/.test(candidate) ? candidate : null;
}

function isWalletRejectError(error: unknown): boolean {
  if (!error || typeof error !== "object") {
    return false;
  }
  const code = (error as { code?: unknown }).code;
  if (code === 4001 || code === "4001") {
    return true;
  }

  const message = (error as { message?: unknown }).message;
  if (typeof message !== "string") {
    return false;
  }
  return /rejected|denied|user rejected|user denied/i.test(message);
}

function getInjectedProvider(): Eip1193Provider | null {
  if (!window.ethereum) {
    return null;
  }
  const providers = Array.isArray(window.ethereum.providers) ? window.ethereum.providers : [];
  if (providers.length > 0) {
    return providers.find((p) => p.isMetaMask) ?? providers[0];
  }
  return window.ethereum;
}

function subscribe(
  provider: Eip1193Provider,
  event: string,
  handler: ProviderEventHandler,
): () => void {
  if (typeof provider.on !== "function") {
    return () => {};
  }
  provider.on(event, handler);
  return () => {
    if (typeof provider.removeListener === "function") {
      provider.removeListener(event, handler);
    }
  };
}

function buildSession(
  provider: Eip1193Provider,
  providerType: WalletProviderType,
  address: string,
): WalletSession {
  const normalizedAddress = normalizeAddress(address);

  return {
    address: normalizedAddress,
    providerType,
    async signMessage(message: string): Promise<string> {
      const signature = await provider.request({
        method: "personal_sign",
        params: [message, normalizedAddress],
      });
      if (typeof signature !== "string" || signature.length === 0) {
        throw new Error("Wallet returned an invalid signature.");
      }
      return signature;
    },
    async disconnect(): Promise<void> {
      if (providerType !== "walletconnect") {
        return;
      }
      if (typeof provider.disconnect !== "function") {
        return;
      }
      await provider.disconnect();
    },
    onAccountsChanged(handler: (accounts: string[]) => void): () => void {
      const onAccountsChanged = (accountsLike: unknown) => {
        handler(asStringArray(accountsLike));
      };
      return subscribe(provider, "accountsChanged", onAccountsChanged);
    },
    onDisconnect(handler: () => void): () => void {
      return subscribe(provider, "disconnect", () => handler());
    },
  };
}

function coerceWalletError(error: unknown): Error {
  if (isWalletRejectError(error)) {
    return new Error(WALLET_REQUEST_REJECTED_MESSAGE);
  }
  if (error instanceof Error) {
    return error;
  }
  return new Error("Wallet interaction failed. Please try again.");
}

export async function connectInjectedWallet(): Promise<WalletSession> {
  const provider = getInjectedProvider();
  if (!provider) {
    throw new Error(NO_INJECTED_WALLET_MESSAGE);
  }

  try {
    const accountsLike = await provider.request({ method: "eth_requestAccounts" });
    const address = pickAddress(accountsLike);
    if (!address) {
      throw new Error("No wallet account returned by provider.");
    }
    return buildSession(provider, "injected", address);
  } catch (error: unknown) {
    throw coerceWalletError(error);
  }
}

export async function connectWalletConnect(): Promise<WalletSession> {
  const projectId = import.meta.env.VITE_WALLETCONNECT_PROJECT_ID?.trim();
  if (!projectId) {
    throw new Error(MISSING_WALLETCONNECT_PROJECT_ID_MESSAGE);
  }

  let provider: EthereumProvider | null = null;
  try {
    provider = await EthereumProvider.init({
      projectId,
      chains: [137],
      showQrModal: true,
      rpcMap: {
        137: "https://polygon-rpc.com",
      },
    });

    await provider.connect();
    const accountsLike = await provider.request({ method: "eth_accounts" });
    const address = pickAddress(accountsLike);
    if (!address) {
      throw new Error("No wallet account returned by WalletConnect.");
    }

    return buildSession(provider as unknown as Eip1193Provider, "walletconnect", address);
  } catch (error: unknown) {
    if (provider) {
      try {
        await provider.disconnect();
      } catch {
        // Ignore cleanup errors during failed connection attempts.
      }
    }
    throw coerceWalletError(error);
  }
}
