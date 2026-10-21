/**
 * Storage contract for the local trader watchlist (issue #37).
 *
 * The watchlist is pure localStorage state, so the contract is exercised
 * directly against the real module in the Node test process: a legacy v1
 * payload is migrated to the v2 shape, invalid wallets are dropped, the
 * legacy key is removed, duplicate wallets are collapsed, a second load
 * returns the same list, and a concurrent write from another tab is merged
 * instead of overwritten.
 */

import { test, expect } from "@playwright/test";
import {
  MOCK_COPY_STORAGE_KEY,
  loadMockCopyTraders,
  saveMockCopyTraders,
  upsertMockCopyTrader,
} from "../../src/utils/mockCopyTraders";

const LEGACY_MOCK_COPY_STORAGE_KEY = "pm:mock-copy-traders:v1";
const WALLET_A = "0x1111111111111111111111111111111111111111";
const WALLET_B = "0x2222222222222222222222222222222222222222";
const INVALID_WALLET = "0xnot-a-wallet";

/**
 * Install a minimal `localStorage` stand-in on the Node global, because the
 * module reads the browser global directly.
 */
function installFakeStorage(): Map<string, string> {
  const store = new Map<string, string>();
  const fake = {
    getItem: (key: string) => (store.has(key) ? (store.get(key) as string) : null),
    setItem: (key: string, value: string) => {
      store.set(key, String(value));
    },
    removeItem: (key: string) => {
      store.delete(key);
    },
    clear: () => store.clear(),
    key: (index: number) => Array.from(store.keys())[index] ?? null,
    get length() {
      return store.size;
    },
  };
  Object.defineProperty(globalThis, "localStorage", {
    value: fake,
    configurable: true,
    writable: true,
  });
  return store;
}

test.describe("Trader watchlist storage", () => {
  let store: Map<string, string>;

  test.beforeEach(() => {
    store = installFakeStorage();
  });

  test("migrates a legacy v1 payload to the v2 shape", () => {
    store.set(
      LEGACY_MOCK_COPY_STORAGE_KEY,
      JSON.stringify([
        { wallet: WALLET_A, created_at: "2024-01-01T00:00:00.000Z", initial_capital: 10000 },
      ]),
    );

    const loaded = loadMockCopyTraders();

    expect(loaded).toHaveLength(1);
    expect(loaded[0].wallet).toBe(WALLET_A);
    expect(loaded[0].created_at).toBe("2024-01-01T00:00:00.000Z");

    // The fabricated capital never survives the migration, in storage or in
    // the migrated record.
    const stored = store.get(MOCK_COPY_STORAGE_KEY) ?? "";
    expect(stored).not.toContain("initial_capital");
    expect(Object.keys(loaded[0])).not.toContain("initial_capital");
  });

  test("removes the legacy key after migrating", () => {
    store.set(
      LEGACY_MOCK_COPY_STORAGE_KEY,
      JSON.stringify([{ wallet: WALLET_A, created_at: "2024-01-01T00:00:00.000Z" }]),
    );

    loadMockCopyTraders();

    expect(store.has(LEGACY_MOCK_COPY_STORAGE_KEY)).toBe(false);
    expect(store.has(MOCK_COPY_STORAGE_KEY)).toBe(true);
  });

  test("drops entries whose wallet is not a valid address", () => {
    store.set(
      MOCK_COPY_STORAGE_KEY,
      JSON.stringify([
        { wallet: WALLET_A, created_at: "2024-01-01T00:00:00.000Z" },
        { wallet: INVALID_WALLET, created_at: "2024-01-01T00:00:00.000Z" },
      ]),
    );

    const loaded = loadMockCopyTraders();

    expect(loaded.map((t) => t.wallet)).toEqual([WALLET_A]);
  });

  test("deduplicates wallets and is idempotent across loads", () => {
    store.set(
      MOCK_COPY_STORAGE_KEY,
      JSON.stringify([
        { wallet: WALLET_A, created_at: "2024-01-01T00:00:00.000Z" },
        {
          wallet: WALLET_A.toUpperCase().replace("0X", "0x"),
          created_at: "2024-02-02T00:00:00.000Z",
        },
        { wallet: WALLET_B, created_at: "2024-01-01T00:00:00.000Z" },
      ]),
    );

    const first = loadMockCopyTraders();
    const second = loadMockCopyTraders();

    expect(first.map((t) => t.wallet)).toEqual([WALLET_A, WALLET_B]);
    expect(second).toEqual(first);
  });

  test("keeps an unknown watch date instead of inventing today", () => {
    store.set(MOCK_COPY_STORAGE_KEY, JSON.stringify([{ wallet: WALLET_A }]));

    const loaded = loadMockCopyTraders();

    expect(loaded[0].created_at).toBeNull();
  });

  test("rejects an invalid wallet instead of persisting it", () => {
    const result = upsertMockCopyTrader([], INVALID_WALLET);

    expect(result).toEqual([]);

    saveMockCopyTraders(result);
    expect(store.get(MOCK_COPY_STORAGE_KEY)).toBe("[]");
    expect(loadMockCopyTraders()).toEqual([]);
  });

  test("merges a concurrent write from another tab instead of overwriting it", () => {
    saveMockCopyTraders([{ wallet: WALLET_A, created_at: "2024-01-01T00:00:00.000Z" }]);

    // Another tab adds a different wallet while this one is not looking.
    store.set(
      MOCK_COPY_STORAGE_KEY,
      JSON.stringify([
        { wallet: WALLET_B, created_at: "2024-01-02T00:00:00.000Z" },
        { wallet: WALLET_A, created_at: "2024-01-01T00:00:00.000Z" },
      ]),
    );

    saveMockCopyTraders([{ wallet: WALLET_A, created_at: "2024-01-01T00:00:00.000Z" }]);

    const loaded = loadMockCopyTraders();
    expect(loaded.map((t) => t.wallet).sort()).toEqual([WALLET_A, WALLET_B]);
  });
});
