export const MOCK_COPY_STORAGE_KEY = "pm:mock-copy-traders:v1";
export const MOCK_COPY_DEFAULT_CAPITAL = 10_000;
export const MOCK_COPY_TRADERS_CHANGED_EVENT = "pm:mock-copy-traders:changed";

export type MockCopyTrader = {
  wallet: string;
  created_at: string;
  initial_capital: number;
  alias?: string;
};

const normalizeWallet = (wallet: string) => wallet.trim().toLowerCase();

export function loadMockCopyTraders(): MockCopyTrader[] {
  try {
    const raw = localStorage.getItem(MOCK_COPY_STORAGE_KEY);
    if (!raw) return [];
    const parsed = JSON.parse(raw) as MockCopyTrader[];
    if (!Array.isArray(parsed)) return [];
    return parsed
      .filter((t) => typeof t?.wallet === "string")
      .map((t) => ({
        wallet: normalizeWallet(t.wallet),
        created_at: t.created_at || new Date().toISOString(),
        initial_capital: t.initial_capital || MOCK_COPY_DEFAULT_CAPITAL,
        alias:
          typeof t.alias === "string" && t.alias.trim()
            ? t.alias.trim()
            : undefined,
      }));
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
    if (event.key === MOCK_COPY_STORAGE_KEY) {
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

export function hasMockCopyTrader(
  traders: MockCopyTrader[],
  wallet: string,
): boolean {
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
      initial_capital: MOCK_COPY_DEFAULT_CAPITAL,
      alias: alias?.trim() || undefined,
    },
    ...traders,
  ];
}

export function removeMockCopyTrader(
  traders: MockCopyTrader[],
  wallet: string,
): MockCopyTrader[] {
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
