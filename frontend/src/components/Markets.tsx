/**
 * Browsable all-markets board with search, category rail, watchlist filter and infinite scroll.
 *
 * Markets are paged through marketsService.searchMarkets and grouped by event into
 * MarketBoardCardViewModels. Trading opens TradeModal behind useRequireAuth; the event drawer
 * exposes a streamed AI trader analysis popup.
 *
 * @module components/Markets
 */
import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  type KeyboardEvent as ReactKeyboardEvent,
} from "react";
import {
  marketsService,
  MarketCategory,
  BrowseMarket,
  TraderStats,
  TraderAnalysisRequest,
} from "../services/marketsService";
import { getApiErrorMessage } from "../utils/apiError";
import TradeModal, { TradeModalMarket } from "./TradeModal";
import { groupMarketsByEvent, EventGroup, getSubMarketLabel } from "../utils/groupMarkets";
import EventGroupDetailDrawer from "./EventGroupDetailDrawer";
import MarketBoardCard from "./MarketBoardCard";
import { buildPolymarketEventUrl, buildPolygonscanAddressUrl } from "../utils/urlSafety";
import { useRafBufferedText } from "../hooks/useRafBufferedText";
import {
  FOCUS_MARKET_SEARCH_EVENT,
  loadMarketWatchlist,
  loadRecentTradeMarkets,
  subscribeMarketWatchlist,
  subscribeRecentTradeMarkets,
  toggleMarketWatchlist,
} from "../utils/tradingWorkspace";
import { RecentTradeMarket } from "../types/trading";
import { MarketBoardCardVM } from "../types/marketBoard";
import { TradeTicketOutcome } from "../types/trading";
import { useRequireAuth } from "../hooks/useRequireAuth";
const PAGE_SIZE = 60;
const MIN_VISIBLE_TRADE_PANELS = 6;
const MAX_INITIAL_FETCH_PAGES = 1;

// ── Helpers ─────────────────────────────────────────────────────────

function parseOutcomePrices(market: BrowseMarket): { yes: number; no: number } {
  try {
    const bestAsk = market.bestAsk != null ? Number(market.bestAsk) : NaN;
    const bestBid = market.bestBid != null ? Number(market.bestBid) : NaN;
    const lastTrade = market.lastTradePrice != null ? Number(market.lastTradePrice) : NaN;

    let yesPrice = NaN;
    if (!isNaN(bestAsk) && bestAsk > 0 && bestAsk < 1) yesPrice = bestAsk;
    else if (!isNaN(bestBid) && bestBid > 0 && bestBid < 1) yesPrice = bestBid;
    else if (!isNaN(lastTrade) && lastTrade > 0 && lastTrade < 1) yesPrice = lastTrade;

    if (!isNaN(yesPrice)) return { yes: yesPrice, no: Math.round((1 - yesPrice) * 10000) / 10000 };

    const raw = market.outcomePrices;
    if (!raw) return { yes: 0.5, no: 0.5 };
    const prices = typeof raw === "string" ? JSON.parse(raw) : raw;
    return { yes: Number(prices?.[0] ?? 0.5), no: Number(prices?.[1] ?? 0.5) };
  } catch {
    return { yes: 0.5, no: 0.5 };
  }
}

function getPolymarketUrl(market: BrowseMarket): string | null {
  const slug = market.slug || market.market_slug || null;
  return buildPolymarketEventUrl(market._event_slug || slug, slug);
}

function getMarketWatchKey(market: BrowseMarket): string {
  return (
    market._event_slug ||
    market.condition_id ||
    market.market_slug ||
    market.slug ||
    market.question ||
    "unknown-market"
  ).toLowerCase();
}

const fmtUSD2 = (v: number) =>
  `$${Math.abs(v).toLocaleString("en-US", {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  })}`;

const fmtCompactUSD = (value: number) => {
  const n = Math.abs(Number(value || 0));
  if (n >= 1_000_000_000) return `$${(n / 1_000_000_000).toFixed(1)}B`;
  if (n >= 1_000_000) return `$${(n / 1_000_000).toFixed(0)}M`;
  if (n >= 1_000) return `$${(n / 1_000).toFixed(0)}K`;
  return `$${n.toFixed(0)}`;
};

const getCadenceLabel = (endDate?: string) => {
  if (!endDate) return "Active";
  const diffMs = new Date(endDate).getTime() - Date.now();
  const days = Math.ceil(diffMs / 86_400_000);
  if (days <= 1) return "Daily";
  if (days <= 7) return "Weekly";
  if (days <= 35) return "Monthly";
  return "Long-dated";
};

function isPriceDirectionMarket(market: BrowseMarket): boolean {
  const question = String(market.question || market._event_title || "").toLowerCase();
  return question.includes("up or down") || question.includes("minute");
}

// ════════════════════════════════════════════════════════════════════
// Component
// ════════════════════════════════════════════════════════════════════

export default function Markets() {
  const { requireAuth } = useRequireAuth();
  // ── Search / browse state ──────────────────────────────────────
  const [categories, setCategories] = useState<MarketCategory[]>([]);
  const [markets, setMarkets] = useState<BrowseMarket[]>([]);
  const [hasMore, setHasMore] = useState(false);
  const [total, setTotal] = useState(0);
  const [loading, setLoading] = useState(true);
  const [loadingMore, setLoadingMore] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const [searchInput, setSearchInput] = useState("");
  const [debouncedQuery, setDebouncedQuery] = useState("");
  const [selectedTag, setSelectedTag] = useState("");
  const [sort, setSort] = useState("volume24hr");
  const [showFavoritesOnly, setShowFavoritesOnly] = useState(false);
  const [watchlistKeys, setWatchlistKeys] = useState<string[]>(loadMarketWatchlist());
  const [recentTrades, setRecentTrades] = useState<RecentTradeMarket[]>(loadRecentTradeMarkets(8));
  const searchInputRef = useRef<HTMLInputElement | null>(null);
  const categoryRailRef = useRef<HTMLDivElement | null>(null);
  const loadMoreSentinelRef = useRef<HTMLDivElement | null>(null);
  const [tradeDefaultOutcome, setTradeDefaultOutcome] = useState<TradeTicketOutcome | undefined>(
    undefined,
  );

  // AI Trader Analysis popup
  const [analysisMarket, setAnalysisMarket] = useState<BrowseMarket | null>(null);
  const [traderStats, setTraderStats] = useState<TraderStats | null>(null);
  const {
    text: streamText,
    append: appendStreamText,
    reset: resetStreamText,
    flushNow: flushStreamTextNow,
  } = useRafBufferedText();
  const [streamDone, setStreamDone] = useState(false);
  const [streamError, setStreamError] = useState<string | null>(null);
  const abortRef = useRef<AbortController | null>(null);
  const streamContainerRef = useRef<HTMLDivElement | null>(null);

  // Trade modal
  const [tradeMarket, setTradeMarket] = useState<TradeModalMarket | null>(null);
  const [selectedGroup, setSelectedGroup] = useState<EventGroup<BrowseMarket> | null>(null);

  const openTradeModal = (market: BrowseMarket, preferredOutcome?: TradeTicketOutcome) => {
    const prices = parseOutcomePrices(market);
    const watchKey = getMarketWatchKey(market);

    // Build outcome tokens – Polymarket markets always have Yes/No tokens
    const yesTokenId = market.tokens?.[0]?.token_id || market.condition_id || "";
    const noTokenId = market.tokens?.[1]?.token_id || "";

    setTradeMarket({
      market_id: market.condition_id || market.market_slug || "",
      title: market.question || market._event_title || "Market",
      image: market.image || market._event_image,
      watch_key: watchKey,
      bestAsk: market.bestAsk,
      bestBid: market.bestBid,
      liquidity: market.liquidity,
      quote_timestamp: new Date().toISOString(),
      tokens: [
        { token_id: yesTokenId, outcome: "Yes", price: prices.yes },
        { token_id: noTokenId, outcome: "No", price: prices.no },
      ],
    });
    setTradeDefaultOutcome(preferredOutcome);
  };

  // ── Load categories on mount ─────────────────────────────────
  useEffect(() => {
    (async () => {
      try {
        const res = await marketsService.getCategories();
        setCategories(res.categories || []);
      } catch {
        /* categories are optional UI sugar */
      }
    })();
  }, []);

  // ── Debounce search input ────────────────────────────────────
  useEffect(() => {
    const id = setTimeout(() => setDebouncedQuery(searchInput.trim()), 400);
    return () => clearTimeout(id);
  }, [searchInput]);

  useEffect(() => subscribeMarketWatchlist(setWatchlistKeys), []);
  useEffect(() => subscribeRecentTradeMarkets((rows) => setRecentTrades(rows.slice(0, 8))), []);

  useEffect(() => {
    const onFocusSearch = () => {
      searchInputRef.current?.focus();
      searchInputRef.current?.select();
    };
    window.addEventListener(FOCUS_MARKET_SEARCH_EVENT, onFocusSearch);
    return () => window.removeEventListener(FOCUS_MARKET_SEARCH_EVENT, onFocusSearch);
  }, []);

  // ── Fetch markets when filters change ────────────────────────
  const fetchMarkets = useCallback(
    async (offset = 0) => {
      if (offset === 0) setLoading(true);
      else setLoadingMore(true);
      setError(null);
      try {
        if (offset === 0) {
          let merged: BrowseMarket[] = [];
          let nextOffset = 0;
          let hasMoreLocal = false;
          let totalLocal = 0;
          let pagesFetched = 0;

          while (pagesFetched < MAX_INITIAL_FETCH_PAGES) {
            const res = await marketsService.searchMarkets({
              q: debouncedQuery || undefined,
              tag: selectedTag || undefined,
              limit: PAGE_SIZE,
              offset: nextOffset,
              sort,
            });

            totalLocal = res.total;
            merged = [...merged, ...res.markets];
            hasMoreLocal = res.has_more;
            pagesFetched += 1;

            const groupedCount = groupMarketsByEvent(merged).length;
            if (
              groupedCount >= MIN_VISIBLE_TRADE_PANELS ||
              !res.has_more ||
              res.markets.length === 0
            ) {
              break;
            }

            nextOffset += res.markets.length;
          }

          setMarkets(merged);
          setHasMore(hasMoreLocal);
          setTotal(totalLocal);
        } else {
          const res = await marketsService.searchMarkets({
            q: debouncedQuery || undefined,
            tag: selectedTag || undefined,
            limit: PAGE_SIZE,
            offset,
            sort,
          });
          setMarkets((prev) => [...prev, ...res.markets]);
          setHasMore(res.has_more);
          setTotal(res.total);
        }
      } catch (err: unknown) {
        setError(getApiErrorMessage(err, "Failed to load markets"));
      } finally {
        setLoading(false);
        setLoadingMore(false);
      }
    },
    [debouncedQuery, selectedTag, sort],
  );

  useEffect(() => {
    fetchMarkets(0);
  }, [fetchMarkets]);

  const loadMoreMarkets = useCallback(() => {
    if (!hasMore || loading || loadingMore) return;
    fetchMarkets(markets.length);
  }, [fetchMarkets, hasMore, loading, loadingMore, markets.length]);

  useEffect(() => {
    if (!hasMore) return;
    const sentinel = loadMoreSentinelRef.current;
    if (!sentinel) return;
    if (typeof IntersectionObserver === "undefined") return;

    const observer = new IntersectionObserver(
      (entries) => {
        const [entry] = entries;
        if (entry?.isIntersecting) {
          loadMoreMarkets();
        }
      },
      {
        root: null,
        rootMargin: "200px 0px",
        threshold: 0.01,
      },
    );

    observer.observe(sentinel);
    return () => observer.disconnect();
  }, [hasMore, loadMoreMarkets, markets.length]);

  // Auto-scroll streaming text inside its own container (not the page)
  useEffect(() => {
    const el = streamContainerRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [streamText]);

  // ── AI Trader Analysis (streaming) ────────────────────────────
  const openTraderAnalysis = useCallback(
    (market: BrowseMarket) => {
      abortRef.current?.abort();
      const prices = parseOutcomePrices(market);
      setAnalysisMarket(market);
      setTraderStats(null);
      resetStreamText();
      setStreamDone(false);
      setStreamError(null);

      const request: TraderAnalysisRequest = {
        condition_id: market.condition_id || "",
        question: market.question || market._event_title || "Unknown market",
        yes_price: prices.yes,
        no_price: prices.no,
        volume_24h: market.volume24hr || market.volume || 0,
        end_date: market.endDate || market.end_date_iso || new Date().toISOString(),
      };

      const controller = marketsService.streamTraderAnalysis(request, {
        onStats: (stats) => setTraderStats(stats),
        onChunk: (text) => appendStreamText(text),
        onDone: () => {
          flushStreamTextNow();
          setStreamDone(true);
        },
        onError: (err) => {
          flushStreamTextNow();
          setStreamError(err);
          setStreamDone(true);
        },
      });
      abortRef.current = controller;
    },
    [appendStreamText, flushStreamTextNow, resetStreamText],
  );

  const closeAnalysis = () => {
    abortRef.current?.abort();
    setAnalysisMarket(null);
    setTraderStats(null);
    resetStreamText();
    setStreamDone(false);
    setStreamError(null);
  };

  const [showCategoryScrollHint, setShowCategoryScrollHint] = useState(false);
  const eventGroups = useMemo(() => groupMarketsByEvent(markets), [markets]);
  const watchlistSet = useMemo(() => new Set(watchlistKeys), [watchlistKeys]);
  const filteredGroups = useMemo(() => {
    if (!showFavoritesOnly) return eventGroups;
    return eventGroups.filter((group) => watchlistSet.has(group.eventSlug.toLowerCase()));
  }, [eventGroups, showFavoritesOnly, watchlistSet]);
  const totalOptions = useMemo(
    () => filteredGroups.reduce((sum, g) => sum + g.markets.length, 0),
    [filteredGroups],
  );
  const favoriteCount = watchlistKeys.length;

  const categoriesWithAll = useMemo(
    () => [
      { id: "", label: "All" },
      ...categories.map((cat) => ({ id: cat.id, label: cat.label })),
    ],
    [categories],
  );

  const refreshCategoryScrollHint = useCallback(() => {
    const rail = categoryRailRef.current;
    if (!rail) return;
    setShowCategoryScrollHint(rail.scrollWidth - rail.clientWidth - rail.scrollLeft > 8);
  }, []);

  useEffect(() => {
    refreshCategoryScrollHint();
    window.addEventListener("resize", refreshCategoryScrollHint);
    return () => window.removeEventListener("resize", refreshCategoryScrollHint);
  }, [refreshCategoryScrollHint, categoriesWithAll.length, selectedTag]);

  const handleCategoryRailKeyDown = (event: ReactKeyboardEvent<HTMLDivElement>) => {
    if (!categoryRailRef.current) return;
    if (event.key === "ArrowRight") {
      event.preventDefault();
      categoryRailRef.current.scrollBy({ left: 180, behavior: "smooth" });
      return;
    }
    if (event.key === "ArrowLeft") {
      event.preventDefault();
      categoryRailRef.current.scrollBy({ left: -180, behavior: "smooth" });
    }
  };

  const scrollCategoryRailRight = () => {
    categoryRailRef.current?.scrollBy({ left: 260, behavior: "smooth" });
  };

  const boardItems = useMemo(() => {
    return filteredGroups.map((group) => {
      const isFavorite = watchlistSet.has(group.eventSlug.toLowerCase());
      const primaryMarket = group.markets[0];

      if (group.isSingle) {
        const market = primaryMarket;
        const prices = parseOutcomePrices(market);
        const endDate = market.endDate || market.end_date_iso;
        const volume = Number(
          market.volume24hr || market.volumeNum || market.volume || group.eventVolume24hr || 0,
        );
        const isDirection = isPriceDirectionMarket(market);
        const card: MarketBoardCardVM = {
          id: group.eventSlug,
          source: "markets",
          variant: isDirection ? "price_direction" : "binary_single",
          title: market.question || group.eventTitle || "Unknown market",
          image: market.image || market._event_image,
          href: getPolymarketUrl(market),
          probability: prices.yes,
          probabilityLabel: isDirection ? "up" : "chance",
          positiveLabel: isDirection ? "Up" : "Yes",
          negativeLabel: isDirection ? "Down" : "No",
          positiveOutcome: "Yes",
          negativeOutcome: "No",
          positiveMeta: `${Math.round(prices.yes * 100)}%`,
          negativeMeta: `${Math.round(prices.no * 100)}%`,
          footerMeta: `${fmtCompactUSD(volume)} Vol.`,
          footerSubMeta: getCadenceLabel(endDate),
          isLive: isDirection,
          isFavorite,
        };
        return {
          group,
          primaryMarket: market,
          rowMarketMap: new Map<string, BrowseMarket>(),
          card,
        };
      }

      const rowMarketMap = new Map<string, BrowseMarket>();
      const rows = group.markets.map((market, index) => {
        const rowId = `${group.eventSlug}:${market.condition_id || market.market_slug || index}`;
        rowMarketMap.set(rowId, market);
        const prices = parseOutcomePrices(market);
        return {
          id: rowId,
          label: getSubMarketLabel(market, group.eventTitle),
          probabilityText: `${Math.round(prices.yes * 100)}%`,
          yesLabel: "Yes",
          noLabel: "No",
        };
      });

      const primaryPrices = parseOutcomePrices(primaryMarket);
      const eventVolume = Number(group.eventVolume || group.eventVolume24hr || 0);
      const primaryEndDate = primaryMarket.endDate || primaryMarket.end_date_iso;
      const card: MarketBoardCardVM = {
        id: group.eventSlug,
        source: "markets",
        variant: "multi_option",
        title: group.eventTitle,
        image: group.eventImage,
        href: getPolymarketUrl(primaryMarket),
        probability: primaryPrices.yes,
        probabilityLabel: "chance",
        rows,
        footerMeta: `${fmtCompactUSD(eventVolume)} Vol.`,
        footerSubMeta: getCadenceLabel(primaryEndDate),
        isFavorite,
      };
      return { group, primaryMarket, rowMarketMap, card };
    });
  }, [filteredGroups, watchlistSet]);

  const openTradeFromRecent = useCallback((row: RecentTradeMarket) => {
    setTradeMarket({
      market_id: row.market_id,
      title: row.title,
      image: row.image,
      watch_key: row.watch_key,
      tokens: row.tokens,
      bestAsk: row.bestAsk,
      bestBid: row.bestBid,
      liquidity: row.liquidity,
      quote_timestamp: row.quote_timestamp || row.last_traded_at,
    });
    setTradeDefaultOutcome(undefined);
  }, []);

  // ════════════════════════════════════════════════════════════════
  // RENDER
  // ════════════════════════════════════════════════════════════════
  return (
    <div className="pm-board space-y-4">
      <section className="pm-board-surface space-y-4">
        <div className="pm-board-header">
          <h1 className="pm-board-title">All markets</h1>
          <div className="pm-board-tools">
            <button
              type="button"
              className="pm-board-icon-btn"
              aria-label="Focus market search"
              onClick={() => searchInputRef.current?.focus()}
            >
              <svg
                className="pm-board-icon"
                viewBox="0 0 24 24"
                fill="none"
                stroke="currentColor"
                strokeWidth="1.9"
              >
                <path
                  strokeLinecap="round"
                  strokeLinejoin="round"
                  d="m21 21-4.35-4.35m1.6-5.4a6.75 6.75 0 1 1-13.5 0 6.75 6.75 0 0 1 13.5 0Z"
                />
              </svg>
            </button>
            <button
              type="button"
              className="pm-board-icon-btn"
              aria-label="Change sorting mode"
              onClick={() =>
                setSort((prev) =>
                  prev === "volume24hr"
                    ? "liquidity"
                    : prev === "liquidity"
                      ? "startDate"
                      : "volume24hr",
                )
              }
            >
              <svg
                className="pm-board-icon"
                viewBox="0 0 24 24"
                fill="none"
                stroke="currentColor"
                strokeWidth="1.8"
              >
                <path strokeLinecap="round" strokeLinejoin="round" d="M4 6h16M7 12h10M10 18h4" />
              </svg>
            </button>
            <button
              type="button"
              className="pm-board-icon-btn"
              aria-label={showFavoritesOnly ? "Show all markets" : "Show watchlist only"}
              onClick={() => setShowFavoritesOnly((prev) => !prev)}
            >
              <svg
                className="pm-board-icon"
                viewBox="0 0 24 24"
                fill="none"
                stroke="currentColor"
                strokeWidth="1.8"
              >
                <path
                  strokeLinecap="round"
                  strokeLinejoin="round"
                  d="M7 4.75A1.75 1.75 0 0 1 8.75 3h6.5A1.75 1.75 0 0 1 17 4.75v15.5a.75.75 0 0 1-1.206.595L12 17.9l-3.794 2.946A.75.75 0 0 1 7 20.25V4.75Z"
                />
              </svg>
            </button>
          </div>
        </div>

        <div className="pm-board-cats-wrap">
          <div
            ref={categoryRailRef}
            className="pm-board-cats"
            tabIndex={0}
            role="listbox"
            aria-label="Market categories"
            onKeyDown={handleCategoryRailKeyDown}
            onScroll={refreshCategoryScrollHint}
          >
            {categoriesWithAll.map((cat) => {
              const active = selectedTag === cat.id;
              return (
                <button
                  key={cat.id || "all"}
                  type="button"
                  className={`pm-chip-cat ${active ? "is-active" : ""}`}
                  aria-pressed={active}
                  onClick={() => setSelectedTag(active ? "" : cat.id)}
                >
                  {cat.label}
                </button>
              );
            })}
          </div>
          {showCategoryScrollHint && (
            <button
              type="button"
              className="pm-cat-scroll-btn"
              onClick={scrollCategoryRailRight}
              aria-label="Scroll categories right"
            >
              <svg
                className="w-4 h-4 mx-auto"
                viewBox="0 0 20 20"
                fill="none"
                stroke="currentColor"
                strokeWidth="1.8"
              >
                <path strokeLinecap="round" strokeLinejoin="round" d="m7 4 6 6-6 6" />
              </svg>
            </button>
          )}
        </div>

        <div className="grid gap-3 md:grid-cols-[minmax(0,1fr)_auto_auto] md:items-center">
          <div className="pm-board-search">
            <svg
              viewBox="0 0 24 24"
              fill="none"
              stroke="currentColor"
              strokeWidth="1.9"
              aria-hidden="true"
            >
              <path
                strokeLinecap="round"
                strokeLinejoin="round"
                d="m21 21-4.35-4.35m1.6-5.4a6.75 6.75 0 1 1-13.5 0 6.75 6.75 0 0 1 13.5 0Z"
              />
            </svg>
            <input
              ref={searchInputRef}
              type="text"
              value={searchInput}
              onChange={(event) => setSearchInput(event.target.value)}
              placeholder="Search markets... (/)"
            />
            {searchInput && (
              <button
                type="button"
                className="pm-board-search-clear"
                aria-label="Clear search"
                onClick={() => setSearchInput("")}
              >
                ✕
              </button>
            )}
          </div>

          <button
            type="button"
            className={`pm-chip-cat ${showFavoritesOnly ? "is-active" : ""}`}
            onClick={() => setShowFavoritesOnly((prev) => !prev)}
            aria-pressed={showFavoritesOnly}
          >
            Watchlist ({favoriteCount})
          </button>

          <select
            value={sort}
            onChange={(event) => setSort(event.target.value)}
            className="h-[2.7rem] rounded-xl border border-[var(--pm-border)] bg-[var(--pm-card)] px-3 text-sm text-[var(--pm-text)] focus:outline-none focus:border-[var(--pm-accent)]"
            aria-label="Sort markets"
          >
            <option value="volume24hr">Volume 24h</option>
            <option value="liquidity">Liquidity</option>
            <option value="startDate">Newest</option>
          </select>
        </div>

        {recentTrades.length > 0 && (
          <div className="rounded-xl border border-[var(--pm-border)] bg-[var(--pm-card)] p-3">
            <div className="mb-2 flex items-center justify-between">
              <h2 className="text-sm font-semibold text-[var(--pm-text)]">Recent traded markets</h2>
              <span className="text-xs text-[var(--pm-text-soft)]">Quick re-entry</span>
            </div>
            <div className="flex gap-2 overflow-x-auto pb-1">
              {recentTrades.slice(0, 8).map((row) => (
                <button
                  key={`${row.market_id}-${row.last_traded_at}`}
                  type="button"
                  onClick={() => requireAuth(() => openTradeFromRecent(row))}
                  className="min-w-[220px] rounded-lg border border-[var(--pm-border)] bg-[var(--pm-bg)] px-3 py-2 text-left transition hover:border-[var(--pm-border-strong)]"
                >
                  <p className="truncate text-xs font-semibold text-[var(--pm-text)]">
                    {row.title}
                  </p>
                  <p className="mt-1 text-[11px] text-[var(--pm-text-soft)]">
                    {new Date(row.last_traded_at).toLocaleString()}
                  </p>
                </button>
              ))}
            </div>
          </div>
        )}

        <p className="text-xs text-[var(--pm-text-soft)]">
          Showing {filteredGroups.length} events / {totalOptions} options
          {total > 0 ? ` · ${total} total options` : ""}
        </p>

        {error && (
          <div className="rounded-lg border border-red-200 bg-red-50 p-3 text-sm text-red-700">
            {error}
          </div>
        )}

        {loading ? (
          <div className="pm-board-grid">
            {Array.from({ length: 8 }).map((_, index) => (
              <div key={index} className="pm-card-skeleton" />
            ))}
          </div>
        ) : markets.length === 0 ? (
          <div className="rounded-xl border border-[var(--pm-border)] bg-[var(--pm-card)] px-6 py-14 text-center">
            <p className="text-lg font-semibold text-[var(--pm-text)]">No markets found</p>
            <p className="mt-1 text-sm text-[var(--pm-text-soft)]">
              {debouncedQuery
                ? `No results for "${debouncedQuery}". Try a different search term.`
                : "Try a different category or check back later."}
            </p>
          </div>
        ) : filteredGroups.length === 0 ? (
          <div className="rounded-xl border border-[var(--pm-border)] bg-[var(--pm-card)] px-6 py-14 text-center">
            <p className="text-lg font-semibold text-[var(--pm-text)]">
              No watchlist markets in this view
            </p>
            <p className="mt-1 text-sm text-[var(--pm-text-soft)]">
              Disable watchlist mode or add markets to your watchlist.
            </p>
          </div>
        ) : (
          <>
            <div className="pm-board-grid">
              {boardItems.map((item) => (
                <MarketBoardCard
                  key={item.card.id}
                  card={item.card}
                  onTrade={(outcome) =>
                    requireAuth(() => openTradeModal(item.primaryMarket, outcome))
                  }
                  onRowTrade={(rowId, outcome) => {
                    const market = item.rowMarketMap.get(rowId);
                    if (market) requireAuth(() => openTradeModal(market, outcome));
                  }}
                  onOpenDetail={() => setSelectedGroup(item.group)}
                  onToggleFavorite={() =>
                    requireAuth(() => toggleMarketWatchlist(item.group.eventSlug))
                  }
                />
              ))}
            </div>

            {hasMore && (
              <div className="pt-1 text-center">
                <div ref={loadMoreSentinelRef} aria-hidden="true" className="h-1 w-full" />
                {loadingMore && (
                  <p className="text-xs text-[var(--pm-text-soft)]">Loading more markets...</p>
                )}
              </div>
            )}
          </>
        )}
      </section>

      <TradeModal
        market={tradeMarket}
        source="markets"
        defaultOutcome={tradeDefaultOutcome}
        onClose={() => {
          setTradeMarket(null);
          setTradeDefaultOutcome(undefined);
        }}
      />

      {selectedGroup && (
        <EventGroupDetailDrawer
          group={selectedGroup}
          parsePrices={parseOutcomePrices}
          onClose={() => setSelectedGroup(null)}
          renderSubMarket={(market, subLabel) => {
            const prices = parseOutcomePrices(market);
            const vol = market.volume24hr || market.volumeNum || market.volume || 0;
            const endDate = market.endDate || market.end_date_iso;
            return (
              <div className="space-y-2">
                <div className="flex items-center justify-between gap-2">
                  <span className="text-sm font-medium text-white truncate">{subLabel}</span>
                  <div className="flex gap-2 flex-shrink-0">
                    <span className="chip chip-success px-2 py-0.5 text-xs">
                      Yes {(prices.yes * 100).toFixed(0)}c
                    </span>
                    <span className="chip chip-danger px-2 py-0.5 text-xs">
                      No {(prices.no * 100).toFixed(0)}c
                    </span>
                  </div>
                </div>
                <div className="flex items-center justify-between">
                  <div className="flex gap-3 text-xs text-muted">
                    {vol > 0 && <span>Vol: {fmtCompactUSD(vol)}</span>}
                    {endDate && <span>Ends: {new Date(endDate).toLocaleDateString()}</span>}
                  </div>
                  <div className="flex gap-2">
                    <button
                      onClick={() => requireAuth(() => openTradeModal(market))}
                      className="btn-success text-xs px-2 py-1"
                    >
                      Trade
                    </button>
                    <button
                      onClick={() => openTraderAnalysis(market)}
                      className="btn-accent text-xs px-2 py-1"
                    >
                      AI Analysis
                    </button>
                  </div>
                </div>
              </div>
            );
          }}
        />
      )}

      {/* ═══════════════════════════════════════════════════════════
          AI TRADER ANALYSIS POPUP (streaming)
         ═══════════════════════════════════════════════════════════ */}
      {analysisMarket && (
        <div
          className="fixed inset-0 modal-overlay z-50 flex items-start justify-center overflow-y-auto pt-8 pb-8 px-4"
          onClick={closeAnalysis}
        >
          <div
            className="surface-panel max-w-3xl w-full p-6 my-auto"
            style={{ marginTop: "2rem", marginBottom: "2rem" }}
            onClick={(e) => e.stopPropagation()}
          >
            {/* Popup header */}
            <div className="flex justify-between items-start gap-3 mb-4">
              <h2 className="text-lg font-bold pr-3 leading-snug">
                {analysisMarket.question || analysisMarket._event_title || "Market"}
              </h2>
              <button
                onClick={closeAnalysis}
                className="btn-muted !px-3 !py-1.5 text-xs flex-shrink-0"
              >
                Close
              </button>
            </div>

            {/* Trader Stats Cards */}
            {traderStats && (
              <div className="grid grid-cols-2 md:grid-cols-4 gap-3 mb-4">
                <StatsCard
                  label="YES Traders"
                  value={String(traderStats.yes_traders)}
                  accent="emerald"
                />
                <StatsCard label="NO Traders" value={String(traderStats.no_traders)} accent="red" />
                <StatsCard
                  label="YES Volume"
                  value={fmtUSD2(traderStats.yes_volume)}
                  accent="emerald"
                />
                <StatsCard label="NO Volume" value={fmtUSD2(traderStats.no_volume)} accent="red" />
              </div>
            )}

            {/* Volume split bar */}
            {traderStats && traderStats.total_trades > 0 && (
              <div className="mb-4">
                <div className="flex items-center justify-between text-xs text-soft mb-1.5">
                  <span>YES {traderStats.side_ratio.yes.toFixed(1)}%</span>
                  <span className="text-muted">{traderStats.total_trades} trades</span>
                  <span>NO {traderStats.side_ratio.no.toFixed(1)}%</span>
                </div>
                <div className="h-2.5 rounded-full overflow-hidden flex bg-[var(--bg-soft)]">
                  <div
                    className="bg-emerald-500 transition-all duration-500"
                    style={{ width: `${traderStats.side_ratio.yes}%` }}
                  />
                  <div
                    className="bg-red-500 transition-all duration-500"
                    style={{ width: `${traderStats.side_ratio.no}%` }}
                  />
                </div>
              </div>
            )}

            {/* Top Traders Table */}
            {traderStats && traderStats.top_traders.length > 0 && (
              <div className="mb-4 overflow-x-auto">
                <table className="table-theme text-xs w-full">
                  <thead>
                    <tr>
                      <th className="text-left p-2">Trader</th>
                      <th className="text-right p-2">YES Vol</th>
                      <th className="text-right p-2">NO Vol</th>
                      <th className="text-right p-2">Total</th>
                      <th className="text-center p-2">Lean</th>
                    </tr>
                  </thead>
                  <tbody>
                    {traderStats.top_traders.map((t, i) => (
                      <tr key={i} className="border-t border-[var(--line)]">
                        <td className="p-2 mono">
                          {buildPolygonscanAddressUrl(t.address) ? (
                            <a
                              href={buildPolygonscanAddressUrl(t.address) || "#"}
                              target="_blank"
                              rel="noopener noreferrer"
                              className="text-soft hover:text-white"
                            >
                              {t.short_address}
                            </a>
                          ) : (
                            <span className="text-soft">{t.short_address}</span>
                          )}
                        </td>
                        <td className="p-2 text-right mono text-emerald-400">
                          {fmtUSD2(t.yes_volume)}
                        </td>
                        <td className="p-2 text-right mono text-red-400">{fmtUSD2(t.no_volume)}</td>
                        <td className="p-2 text-right mono text-white font-medium">
                          {fmtUSD2(t.total_volume)}
                        </td>
                        <td className="p-2 text-center">
                          <span
                            className={`inline-block px-2 py-0.5 rounded text-[10px] font-bold ${t.lean === "YES" ? "bg-emerald-500/15 text-emerald-400" : "bg-red-500/15 text-red-400"}`}
                          >
                            {t.lean}
                          </span>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}

            {/* Status chip */}
            <div className="mb-3">
              {!streamDone ? (
                <span className="text-sm px-3 py-1.5 rounded inline-flex items-center gap-2 chip chip-warning">
                  <span className="animate-spin h-3.5 w-3.5 border-2 border-current border-t-transparent rounded-full inline-block" />
                  Analyzing trader positions...
                </span>
              ) : streamError ? (
                <span className="text-sm px-3 py-1.5 rounded inline-block chip chip-danger">
                  Analysis failed
                </span>
              ) : (
                <span className="text-sm px-3 py-1.5 rounded inline-block chip chip-success">
                  Analysis complete
                </span>
              )}
            </div>

            {/* Analysis content */}
            {streamError ? (
              <div className="p-4 alert-error rounded text-sm">{streamError}</div>
            ) : streamDone && streamText ? (
              <AnalysisTable text={streamText} />
            ) : streamText ? (
              <div
                ref={streamContainerRef}
                className="bg-[var(--bg-elevated)] border border-[var(--line)] rounded-lg p-4 overflow-y-auto max-h-[40vh] min-h-[120px]"
              >
                <div className="flex items-center gap-2 text-muted text-sm mb-3">
                  <span className="animate-spin h-3.5 w-3.5 border-2 border-current border-t-transparent rounded-full inline-block" />
                  Receiving analysis data…
                </div>
                <pre className="text-xs text-muted whitespace-pre-wrap leading-relaxed opacity-50">
                  {streamText.length > 300 ? "…" + streamText.slice(-300) : streamText}
                  <span className="animate-pulse">&#9612;</span>
                </pre>
              </div>
            ) : (
              <div className="flex items-center gap-2 text-muted italic text-sm py-8 justify-center">
                <span className="animate-spin h-4 w-4 border-2 border-current border-t-transparent rounded-full inline-block" />
                Gathering trader data &amp; connecting to AI...
              </div>
            )}
          </div>
        </div>
      )}
    </div>
  );
}

// ── Small helper components ──────────────────────────────────────────

/** Row metadata for the analysis table */
const ANALYSIS_FIELDS: {
  key: string;
  label: string;
  icon: string;
  accent?: string;
}[] = [
  { key: "market_summary", label: "Market Summary", icon: "📋" },
  {
    key: "probability_assessment",
    label: "Probability Assessment",
    icon: "📊",
    accent: "text-[var(--accent)]",
  },
  { key: "confidence_level", label: "Confidence Level", icon: "🎯" },
  { key: "trader_positioning", label: "Trader Positioning", icon: "📈" },
  {
    key: "smart_money_analysis",
    label: "Smart Money Analysis",
    icon: "💰",
  },
  { key: "key_factors", label: "Key Factors", icon: "📌" },
  {
    key: "recommendation",
    label: "Recommendation",
    icon: "⚡",
    accent: "text-[var(--accent)] font-semibold",
  },
  { key: "risk_factors", label: "Risk Factors", icon: "⚠️" },
  { key: "conclusion", label: "Conclusion", icon: "📝" },
];

/**
 * Try to extract a JSON object from (possibly messy) streamed text.
 * The LLM might emit preamble text before or after the JSON block.
 */
function extractJSON(raw: string): Record<string, unknown> | null {
  // Try direct parse first
  try {
    const parsed = JSON.parse(raw.trim());
    if (typeof parsed === "object" && parsed !== null) return parsed;
  } catch {
    // continue
  }

  // Try to find a JSON block within the text (possibly wrapped in ```json ... ```)
  const codeBlock = raw.match(/```(?:json)?\s*([\s\S]*?)```/);
  if (codeBlock) {
    try {
      return JSON.parse(codeBlock[1].trim());
    } catch {
      // continue
    }
  }

  // Find the first { ... } pair greedily
  const start = raw.indexOf("{");
  const end = raw.lastIndexOf("}");
  if (start >= 0 && end > start) {
    try {
      return JSON.parse(raw.slice(start, end + 1));
    } catch {
      // continue
    }
  }
  return null;
}

/** Determine a recommendation badge color */
function recBadge(text: string): { label: string; cls: string } | null {
  const t = text.toUpperCase();
  if (t.includes("BUY YES")) return { label: "BUY YES", cls: "bg-emerald-500/15 text-emerald-400" };
  if (t.includes("BUY NO")) return { label: "BUY NO", cls: "bg-red-500/15 text-red-400" };
  if (t.includes("HOLD")) return { label: "HOLD", cls: "bg-yellow-500/15 text-yellow-400" };
  if (t.includes("SELL")) return { label: "SELL", cls: "bg-red-500/15 text-red-400" };
  return null;
}

/** Determine confidence badge color */
function confBadge(text: string): { label: string; cls: string } | null {
  const t = text.toLowerCase();
  if (t.startsWith("high")) return { label: "High", cls: "bg-emerald-500/15 text-emerald-400" };
  if (t.startsWith("medium") || t.startsWith("moderate"))
    return { label: "Medium", cls: "bg-yellow-500/15 text-yellow-400" };
  if (t.startsWith("low")) return { label: "Low", cls: "bg-red-500/15 text-red-400" };
  return null;
}

/** Renders the parsed JSON analysis as a vertical table with styled rows */
function AnalysisTable({ text }: { text: string }) {
  const parsed = extractJSON(text);

  // If JSON parsing failed, fall back to the AnalysisDisplay-style markdown rendering
  if (!parsed) {
    return (
      <pre className="text-sm text-soft whitespace-pre-wrap leading-relaxed bg-[var(--bg-elevated)] border border-[var(--line)] rounded-lg p-4 max-h-[50vh] overflow-y-auto">
        {text}
      </pre>
    );
  }

  return (
    <div className="space-y-0 border border-[var(--line)] rounded-lg overflow-hidden">
      {ANALYSIS_FIELDS.map((field) => {
        const value = parsed[field.key];
        if (value === undefined || value === null || value === "") return null;

        // Render arrays (key_factors, risk_factors) as bullet lists
        const isArray = Array.isArray(value);
        const isRecommendation = field.key === "recommendation";
        const isConfidence = field.key === "confidence_level";
        const rec = isRecommendation ? recBadge(String(value)) : null;
        const conf = isConfidence ? confBadge(String(value)) : null;

        return (
          <div
            key={field.key}
            className="flex flex-col sm:flex-row border-b border-[var(--line)] last:border-b-0"
          >
            {/* Label column */}
            <div className="sm:w-48 flex-shrink-0 bg-[var(--bg-soft)] px-4 py-3 flex items-start gap-2">
              <span className="text-base flex-shrink-0">{field.icon}</span>
              <span className="text-xs font-semibold text-[var(--text-secondary)] uppercase tracking-wide leading-snug">
                {field.label}
              </span>
            </div>
            {/* Value column */}
            <div className="flex-1 px-4 py-3">
              {isArray ? (
                <ul className="space-y-1.5">
                  {(value as string[]).map((item, i) => (
                    <li key={i} className="flex items-start gap-2 text-sm text-soft">
                      <span
                        className={`mt-1.5 w-1.5 h-1.5 rounded-full flex-shrink-0 ${
                          field.key === "risk_factors" ? "bg-red-400" : "bg-[var(--accent)]"
                        }`}
                      />
                      <span>{item}</span>
                    </li>
                  ))}
                </ul>
              ) : (
                <div className="flex items-start gap-2 flex-wrap">
                  {rec && (
                    <span
                      className={`inline-block px-2.5 py-0.5 rounded text-xs font-bold ${rec.cls}`}
                    >
                      {rec.label}
                    </span>
                  )}
                  {conf && (
                    <span
                      className={`inline-block px-2.5 py-0.5 rounded text-xs font-bold ${conf.cls}`}
                    >
                      {conf.label}
                    </span>
                  )}
                  <p className={`text-sm leading-relaxed ${field.accent || "text-soft"}`}>
                    {String(value)}
                  </p>
                </div>
              )}
            </div>
          </div>
        );
      })}
    </div>
  );
}

function StatsCard({
  label,
  value,
  accent,
}: {
  label: string;
  value: string;
  accent: "emerald" | "red";
}) {
  const colors =
    accent === "emerald"
      ? "border-emerald-500/25 bg-emerald-500/5"
      : "border-red-500/25 bg-red-500/5";
  const textColor = accent === "emerald" ? "text-emerald-400" : "text-red-400";
  return (
    <div className={`rounded-lg border p-3 ${colors}`}>
      <p className="text-muted text-[10px] uppercase tracking-wider mb-1">{label}</p>
      <p className={`text-lg font-bold ${textColor}`}>{value}</p>
    </div>
  );
}
