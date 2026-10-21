/**
 * Local (simulation-only) copy-trader list backed by localStorage.
 *
 * Stores wallet, creation time, starting capital, and optional alias. `saveMockCopyTraders` emits a
 * custom event and `subscribeMockCopyTraders` also listens for cross-tab `storage` events, so
 * multiple open tabs stay in sync. All list helpers take and return a fresh array; callers own the
 * state. Wallets are always normalized to trimmed lowercase.
 *
 * @module utils/mockCopyTraders
 */

export const MOCK_COPY_STORAGE_KEY = "pm:mock-copy-traders:v2";
const LEGACY_MOCK_COPY_STORAGE_KEY = "pm:mock-copy-traders:v1";
export const MOCK_COPY_TRADERS_CHANGED_EVENT = "pm:mock-copy-traders:changed";

export type MockCopyTrader = {
  wallet: string;
  created_at: string;
  alias?: string;
};

const normalizeWallet = (wallet: string) => wallet.trim().toLowerCase();
const isWallet = (wallet: string) => /^0x[a-f0-9]{40}$/.test(wallet);

function parseWatchlist(raw: string | null): MockCopyTrader[] {
  if (!raw) return [];
  const decoded: unknown = JSON.parse(raw);
  if (!Array.isArray(decoded)) return [];
  const seen = new Set<string>();
  return decoded
    .filter((item): item is Record<string, unknown> => !!item && typeof item === "object")
    .map((item) => {
      const wallet = normalizeWallet(String(item.wallet ?? ""));
      const alias = typeof item.alias === "string" ? item.alias.trim().slice(0, 100) : "";
      const date = typeof item.created_at === "string" ? Date.parse(item.created_at) : NaN;
      return {
        wallet,
        created_at: Number.isFinite(date) ? new Date(date).toISOString() : new Date().toISOString(),
        alias: alias || undefined,
      };
    })
    .filter((item) => {
      if (!isWallet(item.wallet) || seen.has(item.wallet)) return false;
      seen.add(item.wallet);
      return true;
    })
    .slice(0, 500);
}

export function loadMockCopyTraders(): MockCopyTrader[] {
  try {
    const modern = localStorage.getItem(MOCK_COPY_STORAGE_KEY);
    if (modern !== null) return parseWatchlist(modern);
    const legacy = parseWatchlist(localStorage.getItem(LEGACY_MOCK_COPY_STORAGE_KEY));
    // Legacy "initial_capital" was never a cash ledger. Migrate only
    // identification metadata; synthetic P&L must not survive.
    localStorage.setItem(MOCK_COPY_STORAGE_KEY, JSON.stringify(legacy));
    return legacy;
  } catch {
    return [];
  }
}

export function saveMockCopyTraders(traders: MockCopyTrader[]): void {
  localStorage.setItem(MOCK_COPY_STORAGE_KEY, JSON.stringify(traders));
  if (typeof window !== "undefined") {
    window.dispatchEvent(new CustomEvent(MOCK_COPY_TRADERS_CHANGED_EVENT));
  }
}

export function subscribeMockCopyTraders(
  onChange: (traders: MockCopyTrader[]) => void,
): () => void {
  if (typeof window === "undefined") return () => {};

  const handleChanged = () => onChange(loadMockCopyTraders());
  const handleStorage = (event: StorageEvent) => {
    if (event.key === MOCK_COPY_STORAGE_KEY || event.key === LEGACY_MOCK_COPY_STORAGE_KEY) {
      onChange(loadMockCopyTraders());
    }
  };

  window.addEventListener(MOCK_COPY_TRADERS_CHANGED_EVENT, handleChanged);
  window.addEventListener("storage", handleStorage);

  return () => {
    window.removeEventListener(MOCK_COPY_TRADERS_CHANGED_EVENT, handleChanged);
    window.removeEventListener("storage", handleStorage);
  };
}

export function hasMockCopyTrader(traders: MockCopyTrader[], wallet: string): boolean {
  const normalized = normalizeWallet(wallet);
  return traders.some((t) => normalizeWallet(t.wallet) === normalized);
}

export function upsertMockCopyTrader(
  traders: MockCopyTrader[],
  wallet: string,
  alias?: string | null,
): MockCopyTrader[] {
  const normalized = normalizeWallet(wallet);
  if (hasMockCopyTrader(traders, normalized)) return traders;
  return [
    {
      wallet: normalized,
      created_at: new Date().toISOString(),
      alias: alias?.trim() || undefined,
    },
    ...traders,
  ];
}

export function removeMockCopyTrader(traders: MockCopyTrader[], wallet: string): MockCopyTrader[] {
  const normalized = normalizeWallet(wallet);
  return traders.filter((t) => normalizeWallet(t.wallet) !== normalized);
}

export function setMockCopyTraderAlias(
  traders: MockCopyTrader[],
  wallet: string,
  alias: string,
): MockCopyTrader[] {
  const normalized = normalizeWallet(wallet);
  return traders.map((t) =>
    normalizeWallet(t.wallet) === normalized
      ? {
          ...t,
          alias: alias.trim() ? alias : undefined,
        }
      : t,
  );
}
