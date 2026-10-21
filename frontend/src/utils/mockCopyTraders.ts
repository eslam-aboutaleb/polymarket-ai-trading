/**
 * Local watchlist of Polymarket wallets backed by localStorage.
 *
 * The list is a bookmark store only: it records which wallets the user wants
 * to keep an eye on and, when that is known, when the wallet was added. It
 * never records a balance, a capital figure or a follower return, so nothing
 * stored here can be mistaken for a simulated portfolio. `saveMockCopyTraders`
 * emits a custom event and `subscribeMockCopyTraders` also listens for
 * cross-tab `storage` events, so multiple open tabs stay in sync. All list
 * helpers take and return a fresh array; callers own the state. Wallets are
 * always normalized to trimmed lowercase.
 *
 * @module utils/mockCopyTraders
 */

export const MOCK_COPY_STORAGE_KEY = "pm:mock-copy-traders:v2";
const LEGACY_MOCK_COPY_STORAGE_KEY = "pm:mock-copy-traders:v1";
export const MOCK_COPY_TRADERS_CHANGED_EVENT = "pm:mock-copy-traders:changed";

export type MockCopyTrader = {
  wallet: string;
  /**
   * ISO timestamp of when the wallet was watchlisted, or `null` when the
   * source record carried no usable date. `null` is rendered as "Unknown":
   * a missing date is never back-filled with the current time.
   */
  created_at: string | null;
  alias?: string;
};

const normalizeWallet = (wallet: string) => wallet.trim().toLowerCase();
const isWallet = (wallet: string) => /^0x[a-f0-9]{40}$/.test(wallet);

/**
 * Parse and validate a watchlist payload.
 *
 * Wallets that are not 40-hex addresses are dropped, duplicates are collapsed
 * and entries without a usable `created_at` keep `null` so the UI can say
 * "Unknown" instead of inventing a watch date.
 */
export function parseWatchlist(raw: string | null): MockCopyTrader[] {
  if (!raw) return [];
  let decoded: unknown;
  try {
    decoded = JSON.parse(raw);
  } catch {
    return [];
  }
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
        created_at: Number.isFinite(date) ? new Date(date).toISOString() : null,
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

/**
 * Union two watchlists by normalized wallet, keeping the first occurrence.
 * Used to merge an in-memory list with the persisted one so a concurrent
 * write from another tab cannot drop entries.
 */
function mergeWatchlists(primary: MockCopyTrader[], secondary: MockCopyTrader[]): MockCopyTrader[] {
  const seen = new Set<string>();
  const merged: MockCopyTrader[] = [];
  for (const trader of [...primary, ...secondary]) {
    const wallet = normalizeWallet(trader.wallet);
    if (!isWallet(wallet) || seen.has(wallet)) continue;
    seen.add(wallet);
    merged.push({ ...trader, wallet, created_at: trader.created_at ?? null });
  }
  return merged.slice(0, 500);
}

/** The watchlist currently persisted, validated the same way a load validates. */
function readStoredWatchlist(): MockCopyTrader[] {
  try {
    return parseWatchlist(localStorage.getItem(MOCK_COPY_STORAGE_KEY));
  } catch {
    return [];
  }
}

/** Persist a watchlist and notify the other listeners in this tab. */
function writeWatchlist(traders: MockCopyTrader[]): void {
  localStorage.setItem(MOCK_COPY_STORAGE_KEY, JSON.stringify(traders));
  if (typeof window !== "undefined") {
    window.dispatchEvent(new CustomEvent(MOCK_COPY_TRADERS_CHANGED_EVENT));
  }
}

export function loadMockCopyTraders(): MockCopyTrader[] {
  try {
    const modern = localStorage.getItem(MOCK_COPY_STORAGE_KEY);
    if (modern !== null) return parseWatchlist(modern);
    const legacy = parseWatchlist(localStorage.getItem(LEGACY_MOCK_COPY_STORAGE_KEY));
    // Legacy "initial_capital" was never a cash ledger. Migrate only
    // identification metadata; synthetic P&L must not survive. The legacy key
    // is dropped so the fabricated capital cannot come back on a later load.
    localStorage.setItem(MOCK_COPY_STORAGE_KEY, JSON.stringify(legacy));
    localStorage.removeItem(LEGACY_MOCK_COPY_STORAGE_KEY);
    return legacy;
  } catch {
    return [];
  }
}

/**
 * Persist the watchlist.
 *
 * The list is validated exactly as a load validates it — invalid wallets are
 * dropped and duplicates collapsed — and then merged with the value currently
 * in storage, so a concurrent write from another tab cannot drop entries.
 */
export function saveMockCopyTraders(traders: MockCopyTrader[]): void {
  const validated = parseWatchlist(JSON.stringify(traders));
  writeWatchlist(mergeWatchlists(validated, readStoredWatchlist()));
}

/**
 * Remove a wallet from the persisted watchlist.
 *
 * The removal is applied to the stored list (not to a possibly stale
 * in-memory copy) so entries added by another tab survive. Returns the new
 * persisted list.
 */
export function removeMockCopyTraderAndPersist(wallet: string): MockCopyTrader[] {
  const normalized = normalizeWallet(wallet);
  const remaining = readStoredWatchlist().filter(
    (trader) => normalizeWallet(trader.wallet) !== normalized,
  );
  writeWatchlist(remaining);
  return remaining;
}

/**
 * Serialize the watchlist for export. The payload is the v2 shape: wallets
 * and watch dates only, with no balance, capital or return fields.
 */
export function exportMockCopyTraders(): string {
  return JSON.stringify(loadMockCopyTraders(), null, 2);
}

/**
 * Import a watchlist payload.
 *
 * The payload is validated with the same rules as a stored list (invalid
 * wallets dropped, duplicates collapsed) and merged into the current
 * watchlist, so an import can never delete existing entries.
 */
export function importMockCopyTraders(raw: string): MockCopyTrader[] {
  const merged = mergeWatchlists(parseWatchlist(raw), readStoredWatchlist());
  writeWatchlist(merged);
  return merged;
}

/** Clear the watchlist, including any leftover legacy payload. */
export function resetMockCopyTraders(): void {
  try {
    localStorage.removeItem(MOCK_COPY_STORAGE_KEY);
    localStorage.removeItem(LEGACY_MOCK_COPY_STORAGE_KEY);
  } catch {
    // Storage is unavailable: there is nothing to clear.
  }
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
  // A malformed wallet is rejected here: it must never reach storage, where
  // it would be silently dropped again on the next load.
  if (!isWallet(normalized)) return traders;
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
