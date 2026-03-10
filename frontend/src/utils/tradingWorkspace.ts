import { RecentTradeMarket } from "../types/trading";

export const FOCUS_MARKET_SEARCH_EVENT = "pm:focus-market-search";

export const MARKET_WATCHLIST_STORAGE_KEY = "pm:market-watchlist:v1";
export const MARKET_WATCHLIST_CHANGED_EVENT = "pm:market-watchlist:changed";

export const RECENT_TRADE_MARKETS_STORAGE_KEY = "pm:recent-trade-markets:v1";
export const RECENT_TRADE_MARKETS_CHANGED_EVENT =
  "pm:recent-trade-markets:changed";

const MAX_RECENT_MARKETS = 12;

const normalizeKey = (value: unknown): string | null => {
  if (typeof value !== "string") return null;
  const trimmed = value.trim();
  return trimmed ? trimmed.toLowerCase() : null;
};

const normalizeRecentTrade = (value: unknown): RecentTradeMarket | null => {
  if (!value || typeof value !== "object") return null;
  const row = value as Partial<RecentTradeMarket>;
  const marketId = typeof row.market_id === "string" ? row.market_id.trim() : "";
  const title = typeof row.title === "string" ? row.title.trim() : "";
  if (!marketId || !title || !Array.isArray(row.tokens) || row.tokens.length === 0) {
    return null;
  }

  const tokens = row.tokens
    .filter((t) => t && typeof t.token_id === "string" && typeof t.outcome === "string")
    .map((t) => ({
      token_id: t.token_id,
      outcome: t.outcome,
      price: Number(t.price) || 0,
    }));

  if (!tokens.length) return null;

  return {
    market_id: marketId,
    title,
    image: typeof row.image === "string" ? row.image : undefined,
    watch_key: typeof row.watch_key === "string" ? row.watch_key : undefined,
    tokens,
    bestAsk: row.bestAsk,
    bestBid: row.bestBid,
    liquidity: Number.isFinite(Number(row.liquidity))
      ? Number(row.liquidity)
      : undefined,
    quote_timestamp:
      typeof row.quote_timestamp === "string" ? row.quote_timestamp : undefined,
    source:
      row.source === "opportunities" || row.source === "dashboard"
        ? row.source
        : "markets",
    last_traded_at:
      typeof row.last_traded_at === "string"
        ? row.last_traded_at
        : new Date().toISOString(),
  };
};

export function loadMarketWatchlist(): string[] {
  if (typeof window === "undefined") return [];
  try {
    const raw = localStorage.getItem(MARKET_WATCHLIST_STORAGE_KEY);
    if (!raw) return [];
    const parsed = JSON.parse(raw);
    if (!Array.isArray(parsed)) return [];

    const seen = new Set<string>();
    const next: string[] = [];
    for (const item of parsed) {
      const key = normalizeKey(item);
      if (!key || seen.has(key)) continue;
      seen.add(key);
      next.push(key);
    }
    return next;
  } catch {
    return [];
  }
}

export function saveMarketWatchlist(keys: string[]): string[] {
  const deduped = Array.from(new Set(keys.map((k) => normalizeKey(k)).filter(Boolean))) as string[];
  if (typeof window !== "undefined") {
    localStorage.setItem(MARKET_WATCHLIST_STORAGE_KEY, JSON.stringify(deduped));
    window.dispatchEvent(new CustomEvent(MARKET_WATCHLIST_CHANGED_EVENT));
  }
  return deduped;
}

export function toggleMarketWatchlist(key: string): string[] {
  const normalized = normalizeKey(key);
  if (!normalized) return loadMarketWatchlist();
  const current = loadMarketWatchlist();
  return current.includes(normalized)
    ? saveMarketWatchlist(current.filter((item) => item !== normalized))
    : saveMarketWatchlist([normalized, ...current]);
}

export function subscribeMarketWatchlist(
  onChange: (keys: string[]) => void,
): () => void {
  if (typeof window === "undefined") return () => {};

  const onCustom = () => onChange(loadMarketWatchlist());
  const onStorage = (event: StorageEvent) => {
    if (event.key === MARKET_WATCHLIST_STORAGE_KEY) {
      onChange(loadMarketWatchlist());
    }
  };

  window.addEventListener(MARKET_WATCHLIST_CHANGED_EVENT, onCustom);
  window.addEventListener("storage", onStorage);

  return () => {
    window.removeEventListener(MARKET_WATCHLIST_CHANGED_EVENT, onCustom);
    window.removeEventListener("storage", onStorage);
  };
}

export function loadRecentTradeMarkets(limit: number = MAX_RECENT_MARKETS): RecentTradeMarket[] {
  if (typeof window === "undefined") return [];
  try {
    const raw = localStorage.getItem(RECENT_TRADE_MARKETS_STORAGE_KEY);
    if (!raw) return [];
    const parsed = JSON.parse(raw);
    if (!Array.isArray(parsed)) return [];

    const normalized = parsed
      .map(normalizeRecentTrade)
      .filter((row): row is RecentTradeMarket => !!row)
      .sort(
        (a, b) =>
          new Date(b.last_traded_at).getTime() -
          new Date(a.last_traded_at).getTime(),
      );

    return normalized.slice(0, Math.max(1, limit));
  } catch {
    return [];
  }
}

export function saveRecentTradeMarkets(rows: RecentTradeMarket[]): RecentTradeMarket[] {
  const normalized = rows
    .map(normalizeRecentTrade)
    .filter((row): row is RecentTradeMarket => !!row)
    .sort(
      (a, b) =>
        new Date(b.last_traded_at).getTime() - new Date(a.last_traded_at).getTime(),
    )
    .slice(0, MAX_RECENT_MARKETS);

  if (typeof window !== "undefined") {
    localStorage.setItem(RECENT_TRADE_MARKETS_STORAGE_KEY, JSON.stringify(normalized));
    window.dispatchEvent(new CustomEvent(RECENT_TRADE_MARKETS_CHANGED_EVENT));
  }
  return normalized;
}

export function pushRecentTradeMarket(
  row: Omit<RecentTradeMarket, "last_traded_at"> & { last_traded_at?: string },
): RecentTradeMarket[] {
  const normalized = normalizeRecentTrade({
    ...row,
    last_traded_at: row.last_traded_at || new Date().toISOString(),
  });
  if (!normalized) return loadRecentTradeMarkets();

  const current = loadRecentTradeMarkets(MAX_RECENT_MARKETS);
  const deduped = current.filter((entry) => entry.market_id !== normalized.market_id);
  return saveRecentTradeMarkets([normalized, ...deduped]);
}

export function subscribeRecentTradeMarkets(
  onChange: (rows: RecentTradeMarket[]) => void,
): () => void {
  if (typeof window === "undefined") return () => {};

  const onCustom = () => onChange(loadRecentTradeMarkets());
  const onStorage = (event: StorageEvent) => {
    if (event.key === RECENT_TRADE_MARKETS_STORAGE_KEY) {
      onChange(loadRecentTradeMarkets());
    }
  };

  window.addEventListener(RECENT_TRADE_MARKETS_CHANGED_EVENT, onCustom);
  window.addEventListener("storage", onStorage);

  return () => {
    window.removeEventListener(RECENT_TRADE_MARKETS_CHANGED_EVENT, onCustom);
    window.removeEventListener("storage", onStorage);
  };
}
