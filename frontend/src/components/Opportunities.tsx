import React, { useCallback, useEffect, useRef, useState } from "react";
import {
  portfolioService,
  PolymarketMarket,
  CombinedMarketsResponse,
} from "../services/portfolioService";
import { marketsService, MarketCategory } from "../services/marketsService";
import {
  analysisService,
  QuickAnalysisRequest,
  QuickGroupAnalysisRequest,
  QuickGroupAnalysisData,
  OpportunityScore,
} from "../services/analysisService";
import { getApiErrorMessage, sanitizeAIError } from "../utils/apiError";
import AnalysisDisplay from "./AnalysisDisplay";
import TradeModal, { TradeModalMarket } from "./TradeModal";
import {
  groupMarketsByEvent,
  EventGroup,
  getSubMarketLabel,
  resolvePrimaryGroupOption,
} from "../utils/groupMarkets";
import EventGroupDetailDrawer from "./EventGroupDetailDrawer";
import MarketBoardCard from "./MarketBoardCard";
import { buildPolymarketEventUrl } from "../utils/urlSafety";
import { useRafBufferedText } from "../hooks/useRafBufferedText";
import {
  FOCUS_MARKET_SEARCH_EVENT,
  loadMarketWatchlist,
  loadRecentTradeMarkets,
  subscribeMarketWatchlist,
  subscribeRecentTradeMarkets,
  toggleMarketWatchlist,
} from "../utils/tradingWorkspace";
import { RecentTradeMarket, TradeTicketOutcome } from "../types/trading";
import { MarketBoardCardVM } from "../types/marketBoard";
import { useRequireAuth } from "../hooks/useRequireAuth";

interface MarketWithAnalysis extends PolymarketMarket {
  analysis?: string;
  analysisLoading?: boolean;
  analysisError?: string;
  /** Direction of last price change: 1 = up, -1 = down, 0 = flat */
  priceDirection?: number;
  /** AI opportunity score from the scanning pipeline */
  opportunityScore?: OpportunityScore;
  /** PNL potential computed on fetch (1 - min(yes,no)) */
  pnl_potential?: number;
}

interface GroupQuickAnalysisState {
  loading: boolean;
  analysis?: string;
  error?: string;
  recommended_option?: string;
  recommended_side?: string;
}

/** How often (ms) to auto-refresh prices */
const PRICE_POLL_INTERVAL = 15_000;
const MIN_VISIBLE_TRADE_PANELS = 10;
const INITIAL_MARKETS_LIMIT = 60;
const MAX_MARKETS_LIMIT = 180;
const PAGE_SIZE = 60;

/** Maps category IDs to sets of Gamma tag slugs for proper tag-based filtering */
const TAG_SLUG_MAP: Record<string, Set<string>> = {
  sports: new Set([
    "sports",
    "nba",
    "nfl",
    "soccer",
    "hockey",
    "nhl",
    "basketball",
    "stanley-cup",
    "nba-champion",
    "nba-finals",
    "2026-fifa-world-cup",
    "fifa-world-cup",
    "world-cup",
  ]),
  politics: new Set([
    "politics",
    "elections",
    "congress",
    "senate-primary",
    "house",
    "president",
    "primaries",
    "primary-elections",
    "us-presidential-election",
    "global-elections",
    "world-elections",
    "republican-primary",
    "us-government",
    "texas-primary",
    "texas-senate",
  ]),
  crypto: new Set([
    "crypto",
    "crypto-prices",
    "airdrops",
    "fdv",
    "exchange",
    "megaeth",
  ]),
  "pop-culture": new Set([
    "pop-culture",
    "celebrities",
    "music",
    "taylor-swift",
    "creators",
    "awards",
    "gta-vi",
    "video-games",
  ]),
  business: new Set([
    "business",
    "finance",
    "economy",
    "stocks",
    "ipos",
    "macro-geopolitics",
    "pre-market",
    "microstrategy",
    "trade-war",
    "taxes",
  ]),
  science: new Set(["science"]),
  technology: new Set([
    "tech",
    "ai",
    "big-tech",
    "openai",
    "gpt-5",
    "sam-altman",
  ]),
  world: new Set([
    "world",
    "world-affairs",
    "geopolitics",
    "foreign-policy",
    "ukraine",
    "ukraine-peace-deal",
    "ukraine-map",
    "russia",
    "russia-capture",
    "china",
    "india",
    "eu",
    "uk",
    "france",
    "middle-east",
    "iran",
    "israel",
    "nato",
    "military-action",
    "immigration",
    "syria",
    "poland",
    "us-iran",
    "trump-zelenskyy",
    "trump-putin",
    "zelensky",
    "putin",
    "security-guarantee",
  ]),
  entertainment: new Set([
    "entertainment",
    "movies",
    "music",
    "awards",
    "creators",
    "celebrities",
  ]),
};

/** Check if a market belongs to a category by its _event_tags */
function marketMatchesCategory(
  market: MarketWithAnalysis,
  categoryId: string,
): boolean {
  const allowed = TAG_SLUG_MAP[categoryId];
  if (!allowed) return false;
  const tags = (market as Record<string, unknown>)._event_tags;
  if (!Array.isArray(tags)) return false;
  return tags.some((t: Record<string, unknown>) => {
    const slug = String(t?.slug || "").toLowerCase();
    return allowed.has(slug);
  });
}

const PREFERRED_ANALYSIS_KEYS = [
  "analysis",
  "recommendation",
  "scan_results",
  "results",
  "error",
];

function stringifyAnalysisData(data: unknown): string {
  if (typeof data === "string") {
    return data;
  }

  if (data && typeof data === "object") {
    const record = data as Record<string, unknown>;

    for (const key of PREFERRED_ANALYSIS_KEYS) {
      const value = record[key];
      if (typeof value === "string" && value.trim().length > 0) {
        return value;
      }
    }
  }

  try {
    return JSON.stringify(data, null, 2);
  } catch {
    return String(data);
  }
}

function parseOutcomePrices(market: MarketWithAnalysis): {
  yes: number;
  no: number;
} {
  try {
    // Prefer live order-book prices over stale outcomePrices
    const bestAsk = market.bestAsk != null ? Number(market.bestAsk) : NaN;
    const bestBid = market.bestBid != null ? Number(market.bestBid) : NaN;
    const lastTrade =
      market.lastTradePrice != null ? Number(market.lastTradePrice) : NaN;

    let yesPrice = NaN;
    if (!isNaN(bestAsk) && bestAsk > 0 && bestAsk < 1) {
      yesPrice = bestAsk;
    } else if (!isNaN(bestBid) && bestBid > 0 && bestBid < 1) {
      yesPrice = bestBid;
    } else if (!isNaN(lastTrade) && lastTrade > 0 && lastTrade < 1) {
      yesPrice = lastTrade;
    }

    if (!isNaN(yesPrice)) {
      return { yes: yesPrice, no: Math.round((1 - yesPrice) * 10000) / 10000 };
    }

    // Fallback to outcomePrices array
    const raw = market.outcomePrices;
    if (!raw) return { yes: 0.5, no: 0.5 };
    const prices = typeof raw === "string" ? JSON.parse(raw) : raw;
    return {
      yes: Number(prices?.[0] || 0.5),
      no: Number(prices?.[1] || 0.5),
    };
  } catch {
    return { yes: 0.5, no: 0.5 };
  }
}

function getPolymarketUrl(market: MarketWithAnalysis): string | null {
  const slug = market.slug || market.market_slug || null;
  return buildPolymarketEventUrl(market._event_slug || slug, slug);
}

function getMarketWatchKey(market: MarketWithAnalysis): string {
  return (
    market._event_slug ||
    market.condition_id ||
    market.conditionId ||
    market.market_slug ||
    market.slug ||
    market.id ||
    market.question ||
    "unknown-market"
  ).toLowerCase();
}

const fmtCompactUSD = (value: number | string | undefined | null) => {
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

function isPriceDirectionMarket(market: MarketWithAnalysis): boolean {
  const question = String(
    market.question || market._event_title || "",
  ).toLowerCase();
  return question.includes("up or down") || question.includes("minute");
}

type SortMode = "ai_score" | "ease" | "pnl" | "risk" | "smart_money";

export default function Opportunities() {
  const { requireAuth } = useRequireAuth();
  const [markets, setMarkets] = useState<MarketWithAnalysis[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  // Streaming detail analysis state
  const [selectedMarket, setSelectedMarket] =
    useState<MarketWithAnalysis | null>(null);
  const {
    text: streamedText,
    append: appendStreamedText,
    reset: resetStreamedText,
    flushNow: flushStreamedTextNow,
  } = useRafBufferedText();
  const [streamDone, setStreamDone] = useState(false);
  const [streamError, setStreamError] = useState<string | null>(null);
  const abortRef = useRef<AbortController | null>(null);
  /** Y-position (viewport %) where the user clicked Full Analysis */
  const [popupOriginY, setPopupOriginY] = useState(50);
  const [lastPriceUpdate, setLastPriceUpdate] = useState<Date | null>(null);
  /** Store previous yes-prices keyed by market question for flash detection */
  const prevPricesRef = useRef<Map<string, number>>(new Map());
  /** Trade modal */
  const [tradeMarket, setTradeMarket] = useState<TradeModalMarket | null>(null);
  /** Selected event group for drawer/modal */
  const [selectedGroup, setSelectedGroup] =
    useState<EventGroup<MarketWithAnalysis> | null>(null);
  /** Group-level quick analysis keyed by event slug */
  const [groupQuickBySlug, setGroupQuickBySlug] = useState<
    Record<string, GroupQuickAnalysisState>
  >({});

  // ── AI Opportunity Scanning ──
  const [aiScanning, setAiScanning] = useState(false);
  const [aiProgress, setAiProgress] = useState<{ done: number; total: number }>(
    { done: 0, total: 0 },
  );
  const [aiScanError, setAiScanError] = useState<string | null>(null);
  const [sortMode, setSortMode] = useState<SortMode>("ease");
  const [searchInput, setSearchInput] = useState("");
  const [categories, setCategories] = useState<MarketCategory[]>([]);
  const [selectedTag, setSelectedTag] = useState("");
  const [showFavoritesOnly, setShowFavoritesOnly] = useState(false);
  const [showCategoryScrollHint, setShowCategoryScrollHint] = useState(false);
  const [watchlistKeys, setWatchlistKeys] = useState<string[]>(
    loadMarketWatchlist(),
  );
  const [recentTrades, setRecentTrades] = useState<RecentTradeMarket[]>(
    loadRecentTradeMarkets(8),
  );
  const [tradeDefaultOutcome, setTradeDefaultOutcome] = useState<
    TradeTicketOutcome | undefined
  >(undefined);
  const searchInputRef = useRef<HTMLInputElement | null>(null);
  const categoryRailRef = useRef<HTMLDivElement | null>(null);
  const aiAbortRef = useRef<AbortController | null>(null);
  const batchAnalyzeAbortRef = useRef<AbortController | null>(null);
  /** Track whether the initial AI scan has already been triggered */
  const aiScanTriggered = useRef(false);

  // ── Infinite-scroll pagination ──
  const [pageOffset, setPageOffset] = useState(0);
  const [hasMore, setHasMore] = useState(true);
  const [loadingMore, setLoadingMore] = useState(false);
  const loadMoreRef = useRef<HTMLDivElement | null>(null);

  const openTradeModal = (
    market: MarketWithAnalysis,
    preferredOutcome?: TradeTicketOutcome,
  ) => {
    // Extract token_ids from clobTokenIds, tokens array, or condition_id
    let yesTokenId = "";
    let noTokenId = "";

    // Try tokens array first (has outcome labels)
    if (Array.isArray(market.tokens) && market.tokens.length >= 2) {
      const yesT =
        market.tokens.find((t) => t.outcome === "Yes") || market.tokens[0];
      const noT =
        market.tokens.find((t) => t.outcome === "No") || market.tokens[1];
      yesTokenId = yesT?.token_id || "";
      noTokenId = noT?.token_id || "";
    }

    // Fallback to clobTokenIds
    if (!yesTokenId) {
      const clob = market.clobTokenIds;
      if (Array.isArray(clob) && clob.length > 0) {
        yesTokenId = clob[0];
        noTokenId = clob[1] || "";
      } else if (typeof clob === "string") {
        try {
          const arr = JSON.parse(clob);
          yesTokenId = arr[0] || "";
          noTokenId = arr[1] || "";
        } catch {
          yesTokenId = clob;
        }
      }
    }

    if (!yesTokenId)
      yesTokenId = market.condition_id || market.conditionId || market.id || "";

    const prices = parseOutcomePrices(market);
    setTradeMarket({
      market_id: market.condition_id || market.conditionId || market.id || "",
      title: market.question || "Unknown Market",
      image: market.image || market._event_image,
      watch_key: getMarketWatchKey(market),
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

  useEffect(() => {
    fetchMarkets();
  }, []);

  useEffect(() => {
    (async () => {
      try {
        const res = await marketsService.getCategories();
        setCategories(res.categories || []);
      } catch {
        /* optional UI enhancement */
      }
    })();
  }, []);

  useEffect(() => subscribeMarketWatchlist(setWatchlistKeys), []);
  useEffect(
    () =>
      subscribeRecentTradeMarkets((rows) => setRecentTrades(rows.slice(0, 8))),
    [],
  );

  useEffect(() => {
    const onFocusSearch = () => {
      searchInputRef.current?.focus();
      searchInputRef.current?.select();
    };
    window.addEventListener(FOCUS_MARKET_SEARCH_EVENT, onFocusSearch);
    return () =>
      window.removeEventListener(FOCUS_MARKET_SEARCH_EVENT, onFocusSearch);
  }, []);

  // Auto-poll prices every PRICE_POLL_INTERVAL ms
  useEffect(() => {
    const id = setInterval(refreshPrices, PRICE_POLL_INTERVAL);
    return () => clearInterval(id);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Clear flash after 1.2 s
  useEffect(() => {
    if (!markets.some((m) => m.priceDirection)) return;
    const t = setTimeout(() => {
      setMarkets((prev) => prev.map((m) => ({ ...m, priceDirection: 0 })));
    }, 1200);
    return () => clearTimeout(t);
  }, [lastPriceUpdate]);

  // Auto-scroll inside the modal as streaming text comes in
  const modalBodyRef = useRef<HTMLDivElement | null>(null);
  useEffect(() => {
    const el = modalBodyRef.current;
    if (el) {
      el.scrollTo({ top: el.scrollHeight, behavior: "smooth" });
    }
  }, [streamedText]);

  const fetchMarkets = async () => {
    setLoading(true);
    setError(null);
    setPageOffset(0);
    setHasMore(true);
    try {
      const result: CombinedMarketsResponse =
        await portfolioService.getCombinedMarkets(PAGE_SIZE, 0);
      const fresh = (result.markets || []) as MarketWithAnalysis[];

      // Seed the previous-price map
      prevPricesRef.current.clear();
      fresh.forEach((m) => {
        const key = m.question || m.id || "";
        const p = parseOutcomePrices(m);
        prevPricesRef.current.set(key, p.yes);
      });
      setMarkets(fresh);
      setPageOffset(result.count);
      setHasMore(result.has_more);
      setLastPriceUpdate(new Date());
    } catch (err: unknown) {
      setError(getApiErrorMessage(err, "Failed to load markets"));
    } finally {
      setLoading(false);
    }
  };

  /** Analyze a specific batch of markets (triggered after infinite-scroll load) */
  const analyzeNewMarkets = useCallback((newMarkets: MarketWithAnalysis[]) => {
    if (newMarkets.length === 0) return;

    // Cancel any running batch analysis
    batchAnalyzeAbortRef.current?.abort();

    // Serialize market data for the backend
    const marketData = newMarkets.map((m) => ({
      question: m.question,
      description: m.question,
      condition_id: m.condition_id || m.conditionId || m.id || "",
      conditionId: m.conditionId || m.condition_id,
      outcomePrices: m.outcomePrices,
      bestAsk: m.bestAsk,
      bestBid: m.bestBid,
      lastTradePrice: m.lastTradePrice,
      volume24hr: m.volume24hr || m.volumeNum,
      liquidity: m.liquidity,
      endDateIso: m.end_date_iso || m.endDate,
      end_date_iso: m.end_date_iso,
      _event_title: m._event_title,
      _event_slug: m._event_slug,
      _event_image: m._event_image,
      _event_volume: m._event_volume,
      _event_liquidity: m._event_liquidity,
      _event_volume_24hr: m._event_volume_24hr,
      groupItemTitle: m.groupItemTitle,
      slug: m.slug,
      image: m.image,
    }));

    const ctrl = analysisService.streamAnalyzeMarkets(marketData, {
      onStatus: (_status: string, _total?: number) => {
        setAiScanning(true);
        if (_total)
          setAiProgress((p) => ({ ...p, total: (p.total || 0) + _total }));
      },
      onMarketScored: (
        marketId: string,
        scores: OpportunityScore,
        _doneCount: number,
        total: number,
      ) => {
        setAiProgress((p) => ({
          done: (p.done || 0) + 1,
          total: p.total || total,
        }));
        // Merge scores into the matching market — triggers re-sort automatically
        setMarkets((prev) =>
          prev.map((m) => {
            const mId = m.condition_id || m.conditionId || m.id || "";
            if (mId === marketId || m.question === marketId) {
              return {
                ...m,
                opportunityScore: scores,
                pnl_potential: scores.pnl_potential ?? m.pnl_potential,
              };
            }
            return m;
          }),
        );
      },
      onDone: (_total: number) => {
        setAiScanning(false);
      },
      onError: (err: string) => {
        console.warn("Batch analyze error:", err);
        setAiScanning(false);
      },
    });
    batchAnalyzeAbortRef.current = ctrl;
  }, []);

  /** Load next page of markets (infinite scroll) */
  const loadMoreMarkets = useCallback(async () => {
    if (loadingMore || !hasMore) return;
    setLoadingMore(true);
    try {
      const result: CombinedMarketsResponse =
        await portfolioService.getCombinedMarkets(PAGE_SIZE, pageOffset);
      const incoming = (result.markets || []) as MarketWithAnalysis[];
      if (incoming.length === 0) {
        setHasMore(false);
        return;
      }
      // Deduplicate against existing markets
      const existingIds = new Set(
        markets.map((m) => m.condition_id || m.conditionId || m.id || ""),
      );
      const newMarkets = incoming.filter((m) => {
        const cid = m.condition_id || m.conditionId || m.id || "";
        return !existingIds.has(cid);
      });
      // Seed price map for new markets
      newMarkets.forEach((m) => {
        const key = m.question || m.id || "";
        const p = parseOutcomePrices(m);
        prevPricesRef.current.set(key, p.yes);
      });
      setMarkets((prev) => [...prev, ...newMarkets]);
      setPageOffset((prev) => prev + result.count);
      setHasMore(result.has_more);

      // Auto-analyze the newly loaded markets with AI
      if (newMarkets.length > 0) {
        analyzeNewMarkets(newMarkets);
      }
    } catch {
      // Silently fail — user can scroll down again to retry
    } finally {
      setLoadingMore(false);
    }
  }, [loadingMore, hasMore, pageOffset, markets, analyzeNewMarkets]);

  /** Trigger the AI opportunity scanning pipeline (SSE stream) */
  const triggerAiScan = useCallback((forceRefresh = false) => {
    // Cancel any running scan
    aiAbortRef.current?.abort();
    setAiScanning(true);
    setAiScanError(null);
    setAiProgress({ done: 0, total: 0 });

    const ctrl = analysisService.streamOpportunityScan(
      { force_refresh: forceRefresh },
      {
        onStatus: (_status: string, total?: number) => {
          if (total) setAiProgress((p) => ({ ...p, total }));
        },
        onMarketScored: (
          marketId: string,
          scores: OpportunityScore,
          doneCount: number,
          total: number,
        ) => {
          setAiProgress({ done: doneCount, total });
          // Merge scores into the matching market
          setMarkets((prev) =>
            prev.map((m) => {
              const mId = m.condition_id || m.conditionId || m.id || "";
              if (mId === marketId || m.question === marketId) {
                return {
                  ...m,
                  opportunityScore: scores,
                  pnl_potential: scores.pnl_potential ?? m.pnl_potential,
                };
              }
              return m;
            }),
          );
        },
        onDone: (_total: number) => {
          setAiScanning(false);
        },
        onError: (err: string) => {
          setAiScanError(err);
          setAiScanning(false);
        },
      },
    );
    aiAbortRef.current = ctrl;
  }, []);

  // Auto-trigger AI scan once markets are loaded
  useEffect(() => {
    if (!loading && markets.length > 0 && !aiScanTriggered.current) {
      aiScanTriggered.current = true;
      triggerAiScan(false);
    }
  }, [loading, markets.length, triggerAiScan]);

  // Cleanup AI scan on unmount
  useEffect(() => {
    return () => {
      aiAbortRef.current?.abort();
      batchAnalyzeAbortRef.current?.abort();
    };
  }, []);

  // ── Infinite scroll via IntersectionObserver ──
  useEffect(() => {
    const sentinel = loadMoreRef.current;
    if (!sentinel) return;
    const observer = new IntersectionObserver(
      (entries) => {
        if (entries[0].isIntersecting && hasMore && !loadingMore && !loading) {
          loadMoreMarkets();
        }
      },
      { rootMargin: "400px" },
    );
    observer.observe(sentinel);
    return () => observer.disconnect();
  }, [hasMore, loadingMore, loading, loadMoreMarkets]);

  /** Lightweight price-only refresh — merges into existing state */
  const refreshPrices = async () => {
    try {
      const resp = await portfolioService.refreshPrices(20);
      const incoming = resp.markets;
      if (!incoming || incoming.length === 0) return;

      // Build lookup by question (most stable key across CLOB/Gamma)
      const priceByQ = new Map<string, (typeof incoming)[0]>();
      for (const m of incoming) {
        priceByQ.set(m.question, m);
      }

      setMarkets((prev) =>
        prev.map((market) => {
          const key = market.question || "";
          const tick = priceByQ.get(key);
          if (!tick) return market;

          const oldYes = prevPricesRef.current.get(key) ?? 0;
          // Build a temporary market-like object for parseOutcomePrices
          const tickAsMarket = {
            outcomePrices: tick.outcomePrices as string[] | undefined,
            bestAsk: tick.bestAsk,
            bestBid: tick.bestBid,
            lastTradePrice: tick.lastTradePrice,
          } as MarketWithAnalysis;
          const newP = parseOutcomePrices(tickAsMarket);
          const dir = newP.yes > oldYes ? 1 : newP.yes < oldYes ? -1 : 0;
          prevPricesRef.current.set(key, newP.yes);

          return {
            ...market,
            outcomePrices: tick.outcomePrices as string[] | undefined,
            bestAsk: tick.bestAsk,
            bestBid: tick.bestBid,
            lastTradePrice: tick.lastTradePrice,
            slug: tick.slug || market.slug,
            volume24hr: tick.volume24hr ?? market.volume24hr,
            liquidity: tick.liquidity ?? market.liquidity,
            priceDirection: dir,
          };
        }),
      );
      setLastPriceUpdate(new Date());
    } catch {
      // Silently ignore poll errors — we'll retry next interval
    }
  };

  const handleQuickAnalysis = useCallback(
    async (market: MarketWithAnalysis) => {
      const marketKey = market.question || market.id || "";
      setMarkets((prev) =>
        prev.map((m) =>
          (m.question || m.id || "") === marketKey
            ? { ...m, analysisLoading: true, analysisError: undefined }
            : m,
        ),
      );

      try {
        const prices = parseOutcomePrices(market);
        const request: QuickAnalysisRequest = {
          question: market.question || "Unknown market",
          current_price: prices.yes,
        };
        const result = await analysisService.quickAnalysis(request);
        setMarkets((prev) =>
          prev.map((m) =>
            (m.question || m.id || "") === marketKey
              ? {
                  ...m,
                  analysis: stringifyAnalysisData(result.data),
                  analysisLoading: false,
                }
              : m,
          ),
        );
      } catch (err: unknown) {
        setMarkets((prev) =>
          prev.map((m) =>
            (m.question || m.id || "") === marketKey
              ? {
                  ...m,
                  analysisLoading: false,
                  analysisError: getApiErrorMessage(err, "Analysis failed"),
                }
              : m,
          ),
        );
      }
    },
    [],
  );

  const buildGroupQuickRequest = useCallback(
    (group: EventGroup<MarketWithAnalysis>): QuickGroupAnalysisRequest => {
      const toNumber = (v: unknown): number => {
        const n = typeof v === "string" ? parseFloat(v) : Number(v);
        return Number.isFinite(n) ? n : 0;
      };

      const sub_markets = group.markets.slice(0, 20).map((m) => {
        const p = parseOutcomePrices(m);
        return {
          label: m.groupItemTitle || m.question || "Option",
          question: m.question || "",
          yes_price: p.yes,
          no_price: p.no,
          volume_24h: toNumber(m.volume24hr || m.volumeNum || 0),
          liquidity: toNumber(m.liquidity || 0),
          condition_id: m.condition_id || m.conditionId || m.id || "",
        };
      });

      return {
        event_title: group.eventTitle,
        event_slug: group.eventSlug,
        event_volume: toNumber(group.eventVolume),
        event_liquidity: toNumber(group.eventLiquidity),
        sub_markets,
      };
    },
    [],
  );

  const handleQuickGroupAnalysis = useCallback(
    async (group: EventGroup<MarketWithAnalysis>) => {
      const slug = group.eventSlug;
      setGroupQuickBySlug((prev) => ({
        ...prev,
        [slug]: {
          ...prev[slug],
          loading: true,
          error: undefined,
        },
      }));

      try {
        const request = buildGroupQuickRequest(group);
        const result = await analysisService.quickGroupAnalysis(request);
        const data = (result.data || {}) as QuickGroupAnalysisData;
        const text =
          typeof data.analysis === "string" && data.analysis.trim()
            ? data.analysis
            : stringifyAnalysisData(result.data);

        setGroupQuickBySlug((prev) => ({
          ...prev,
          [slug]: {
            loading: false,
            analysis: text,
            error: undefined,
            recommended_option: data.recommended_option || "",
            recommended_side: data.recommended_side || "",
          },
        }));
      } catch (err: unknown) {
        setGroupQuickBySlug((prev) => ({
          ...prev,
          [slug]: {
            ...prev[slug],
            loading: false,
            error: getApiErrorMessage(err, "Group quick analysis failed"),
          },
        }));
      }
    },
    [buildGroupQuickRequest],
  );

  /** Open the analysis in a new browser tab with full app chrome */
  const openInNewWindow = () => {
    if (!selectedMarket) return;
    const prices = parseOutcomePrices(selectedMarket);
    const params = new URLSearchParams({
      market_title: selectedMarket.question || "Unknown",
      market_description: selectedMarket.question || "",
      yes_price: String(prices.yes),
      no_price: String(prices.no),
      volume_24h: String(selectedMarket.volume24hr || 0),
      end_date: selectedMarket.endDate || new Date().toISOString(),
      include_research: "true",
    });
    window.open(`/analysis?${params.toString()}`, "_blank");
  };

  /** Open popup immediately and stream the analysis */
  const handleDetailAnalysis = (
    market: MarketWithAnalysis,
    e?: React.MouseEvent,
  ) => {
    // Cancel any previous stream
    abortRef.current?.abort();

    // Record click position so the popup appears near it
    if (e) {
      const yPct = Math.round((e.clientY / window.innerHeight) * 100);
      // Clamp between 15% and 75% so the panel doesn't go offscreen
      setPopupOriginY(Math.max(15, Math.min(75, yPct)));
    } else {
      setPopupOriginY(50);
    }

    // Open popup right away with empty content
    setSelectedMarket(market);
    resetStreamedText();
    setStreamDone(false);
    setStreamError(null);

    const prices = parseOutcomePrices(market);

    const controller = analysisService.streamMarketAnalysis(
      {
        market_title: market.question || "Unknown",
        market_description: market.question || "",
        yes_price: prices.yes,
        no_price: prices.no,
        volume_24h: market.volume24hr || 0,
        end_date: market.endDate || new Date().toISOString(),
        include_research: true,
      },
      {
        onChunk: (text) => appendStreamedText(text),
        onDone: () => {
          flushStreamedTextNow();
          setStreamDone(true);
        },
        onError: (err) => {
          flushStreamedTextNow();
          setStreamError(sanitizeAIError(err));
          setStreamDone(true);
        },
      },
    );

    abortRef.current = controller;
  };

  const closeDetailPopup = () => {
    abortRef.current?.abort();
    setSelectedMarket(null);
    resetStreamedText();
    setStreamDone(false);
    setStreamError(null);
  };

  // ── Sort & filter markets ──
  const sortedMarkets = React.useMemo(() => {
    const copy = [...markets];
    copy.sort((a, b) => {
      const aScore = a.opportunityScore;
      const bScore = b.opportunityScore;
      if (!aScore && !bScore) return 0;
      if (!aScore) return 1;
      if (!bScore) return -1;
      switch (sortMode) {
        case "ai_score":
          return (bScore.ai_score ?? 0) - (aScore.ai_score ?? 0);
        case "ease":
          return (
            (bScore.ease_score ?? bScore.ai_score ?? 0) -
            (aScore.ease_score ?? aScore.ai_score ?? 0)
          );
        case "pnl":
          return (bScore.pnl_potential ?? 0) - (aScore.pnl_potential ?? 0);
        case "risk": {
          const riskOrder: Record<string, number> = {
            low: 0,
            medium: 1,
            high: 2,
          };
          return (
            (riskOrder[aScore.risk_level ?? "high"] ?? 2) -
            (riskOrder[bScore.risk_level ?? "high"] ?? 2)
          );
        }
        case "smart_money": {
          const smartMoneyOrder: Record<string, number> = {
            bullish: 2,
            neutral: 1,
            bearish: 0,
          };
          return (
            (smartMoneyOrder[bScore.smart_money_signal ?? "neutral"] ?? 1) -
            (smartMoneyOrder[aScore.smart_money_signal ?? "neutral"] ?? 1)
          );
        }
        default:
          return (bScore.ai_score ?? 0) - (aScore.ai_score ?? 0);
      }
    });
    return copy;
  }, [markets, sortMode]);

  const eventGroups = React.useMemo(
    () => groupMarketsByEvent(sortedMarkets),
    [sortedMarkets],
  );
  const watchlistSet = React.useMemo(
    () => new Set(watchlistKeys),
    [watchlistKeys],
  );
  const normalizedSearch = searchInput.trim().toLowerCase();
  const categoryLabelById = React.useMemo(
    () =>
      Object.fromEntries(
        categories.map((cat) => [cat.id, cat.label.toLowerCase()]),
      ) as Record<string, string>,
    [categories],
  );

  const filteredEventGroups = React.useMemo(
    () =>
      eventGroups.filter((group) => {
        const isFavorite = watchlistSet.has(group.eventSlug.toLowerCase());
        if (showFavoritesOnly && !isFavorite) return false;

        if (selectedTag) {
          // Use proper tag-based filtering (matches backend TAG_SLUG_MAP)
          const categoryHit = group.markets.some((market) =>
            marketMatchesCategory(market, selectedTag),
          );
          if (!categoryHit) {
            // Fallback: text match on title/question for markets without tags
            const normalizedCat = (
              categoryLabelById[selectedTag] || selectedTag
            ).toLowerCase();
            const title = group.eventTitle.toLowerCase();
            const textHit =
              title.includes(normalizedCat) ||
              title.includes(selectedTag.toLowerCase()) ||
              group.markets.some((market) => {
                const question = String(market.question || "").toLowerCase();
                const label = String(market.groupItemTitle || "").toLowerCase();
                return (
                  question.includes(normalizedCat) ||
                  label.includes(normalizedCat) ||
                  question.includes(selectedTag.toLowerCase()) ||
                  label.includes(selectedTag.toLowerCase())
                );
              });
            if (!textHit) return false;
          }
        }

        if (!normalizedSearch) return true;
        const titleHit = group.eventTitle
          .toLowerCase()
          .includes(normalizedSearch);
        if (titleHit) return true;

        return group.markets.some((market) => {
          const question = String(market.question || "").toLowerCase();
          const label = String(market.groupItemTitle || "").toLowerCase();
          return (
            question.includes(normalizedSearch) ||
            label.includes(normalizedSearch)
          );
        });
      }),
    [
      eventGroups,
      normalizedSearch,
      showFavoritesOnly,
      watchlistSet,
      categoryLabelById,
      selectedTag,
    ],
  );

  const visibleOptionsCount = React.useMemo(
    () =>
      filteredEventGroups.reduce((sum, group) => sum + group.markets.length, 0),
    [filteredEventGroups],
  );
  const favoriteCount = watchlistKeys.length;
  const categoriesWithAll = React.useMemo(
    () => [
      { id: "", label: "All" },
      ...categories.map((cat) => ({ id: cat.id, label: cat.label })),
    ],
    [categories],
  );

  const refreshCategoryScrollHint = useCallback(() => {
    const rail = categoryRailRef.current;
    if (!rail) return;
    setShowCategoryScrollHint(
      rail.scrollWidth - rail.clientWidth - rail.scrollLeft > 8,
    );
  }, []);

  useEffect(() => {
    refreshCategoryScrollHint();
    window.addEventListener("resize", refreshCategoryScrollHint);
    return () =>
      window.removeEventListener("resize", refreshCategoryScrollHint);
  }, [refreshCategoryScrollHint, categoriesWithAll.length, selectedTag]);

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

  useEffect(() => {
    const activeSlugs = new Set(eventGroups.map((group) => group.eventSlug));
    setGroupQuickBySlug((prev) => {
      const next: Record<string, GroupQuickAnalysisState> = {};
      let changed = false;
      for (const [slug, state] of Object.entries(prev)) {
        if (activeSlugs.has(slug)) {
          next[slug] = state;
        } else {
          changed = true;
        }
      }
      return changed ? next : prev;
    });
  }, [eventGroups]);

  const bestGroupScore = (group: EventGroup<MarketWithAnalysis>) => {
    let best: OpportunityScore | undefined;
    for (const market of group.markets) {
      if (
        market.opportunityScore &&
        (!best ||
          (market.opportunityScore.ai_score ?? 0) > (best.ai_score ?? 0))
      ) {
        best = market.opportunityScore;
      }
    }
    return best;
  };

  const recLabel = (rec: string) => {
    const labels: Record<string, string> = {
      strong_buy: "Strong Buy",
      buy: "Buy",
      hold: "Hold",
      avoid: "Avoid",
    };
    return labels[rec] ?? rec.replace(/_/g, " ");
  };

  const selectedGroupBest = selectedGroup
    ? bestGroupScore(selectedGroup)
    : null;
  const selectedGroupPrimary = selectedGroup
    ? resolvePrimaryGroupOption(selectedGroup, {
        parsePrices: parseOutcomePrices,
        preferredOptionLabel: selectedGroupBest?.recommended_option,
      })
    : null;

  const boardItems = React.useMemo(() => {
    return filteredEventGroups.map((group) => {
      const isFavorite = watchlistSet.has(group.eventSlug.toLowerCase());
      const groupBest = bestGroupScore(group);
      const primaryResolved = resolvePrimaryGroupOption(group, {
        parsePrices: parseOutcomePrices,
        preferredOptionLabel: groupBest?.recommended_option,
      });
      const primaryMarket = primaryResolved?.market || group.markets[0];
      const primaryPrices = parseOutcomePrices(primaryMarket);

      if (group.isSingle) {
        const market = primaryMarket;
        const endDate = market.endDate || market.end_date_iso;
        const score = market.opportunityScore;
        const card: MarketBoardCardVM = {
          id: group.eventSlug,
          source: "opportunities",
          variant: isPriceDirectionMarket(market)
            ? "price_direction"
            : "binary_single",
          title: market.question || group.eventTitle,
          image: market.image || market._event_image,
          href: getPolymarketUrl(market),
          probability: primaryPrices.yes,
          probabilityLabel: isPriceDirectionMarket(market) ? "up" : "chance",
          positiveLabel: isPriceDirectionMarket(market) ? "Up" : "Yes",
          negativeLabel: isPriceDirectionMarket(market) ? "Down" : "No",
          positiveOutcome: "Yes",
          negativeOutcome: "No",
          positiveMeta: `${Math.round(primaryPrices.yes * 100)}%`,
          negativeMeta: `${Math.round(primaryPrices.no * 100)}%`,
          footerMeta: `${fmtCompactUSD(market.volume24hr || market.volumeNum || 0)} Vol.`,
          footerSubMeta: getCadenceLabel(endDate),
          isLive: Boolean(market.priceDirection),
          isFavorite,
          aiSummary: score
            ? {
                score: score.ease_score ?? score.ai_score ?? 0,
                recommendation: score.edge_estimate
                  ? `${recLabel(score.recommendation ?? "hold")} (${Math.round((score.edge_estimate ?? 0) * 100)}% edge)`
                  : recLabel(score.recommendation ?? "hold"),
                risk: (score.risk_level || "unknown").toUpperCase(),
              }
            : undefined,
        };
        return {
          group,
          card,
          primaryMarket: market,
          rowMarketMap: new Map<string, MarketWithAnalysis>(),
        };
      }

      const rowMarketMap = new Map<string, MarketWithAnalysis>();
      const rows = group.markets.map((market, index) => {
        const rowId = `${group.eventSlug}:${market.condition_id || market.conditionId || market.id || index}`;
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

      const card: MarketBoardCardVM = {
        id: group.eventSlug,
        source: "opportunities",
        variant: "multi_option",
        title: group.eventTitle,
        image: group.eventImage,
        href: getPolymarketUrl(primaryMarket),
        probability: primaryPrices.yes,
        probabilityLabel: "chance",
        rows,
        footerMeta: `${fmtCompactUSD(group.eventVolume || group.eventVolume24hr)} Vol.`,
        footerSubMeta: `${group.markets.length} options`,
        isFavorite,
        aiSummary: groupBest
          ? {
              score: groupBest.ease_score ?? groupBest.ai_score ?? 0,
              recommendation: groupBest.edge_estimate
                ? `${recLabel(groupBest.recommendation ?? "hold")} (${Math.round((groupBest.edge_estimate ?? 0) * 100)}% edge)`
                : recLabel(groupBest.recommendation ?? "hold"),
              risk: (groupBest.risk_level || "unknown").toUpperCase(),
            }
          : undefined,
      };

      return { group, card, primaryMarket, rowMarketMap };
    });
  }, [filteredEventGroups, watchlistSet]);

  return (
    <div className="pm-board space-y-4">
      <section className="pm-board-surface space-y-4">
        <div className="pm-board-header">
          <h1 className="pm-board-title">Easy Trades</h1>
          <div className="pm-board-tools">
            <button
              type="button"
              className="pm-board-icon-btn"
              aria-label="Focus easy trade search"
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
                setSortMode((prev) =>
                  prev === "ease"
                    ? "ai_score"
                    : prev === "ai_score"
                      ? "pnl"
                      : prev === "pnl"
                        ? "risk"
                        : prev === "risk"
                          ? "smart_money"
                          : "ease",
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
                <path
                  strokeLinecap="round"
                  strokeLinejoin="round"
                  d="M4 6h16M7 12h10M10 18h4"
                />
              </svg>
            </button>
            <button
              type="button"
              className="pm-board-icon-btn"
              aria-label={
                showFavoritesOnly
                  ? "Show all easy trades"
                  : "Show watchlist only"
              }
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
            aria-label="Easy trade categories"
            onKeyDown={(event) => {
              if (!categoryRailRef.current) return;
              if (event.key === "ArrowRight") {
                event.preventDefault();
                categoryRailRef.current.scrollBy({
                  left: 180,
                  behavior: "smooth",
                });
              } else if (event.key === "ArrowLeft") {
                event.preventDefault();
                categoryRailRef.current.scrollBy({
                  left: -180,
                  behavior: "smooth",
                });
              }
            }}
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
              aria-label="Scroll categories right"
              onClick={() =>
                categoryRailRef.current?.scrollBy({
                  left: 260,
                  behavior: "smooth",
                })
              }
            >
              <svg
                className="w-4 h-4 mx-auto"
                viewBox="0 0 20 20"
                fill="none"
                stroke="currentColor"
                strokeWidth="1.8"
              >
                <path
                  strokeLinecap="round"
                  strokeLinejoin="round"
                  d="m7 4 6 6-6 6"
                />
              </svg>
            </button>
          )}
        </div>

        <div className="grid gap-3 md:grid-cols-[minmax(0,1fr)_auto_auto_auto] md:items-center">
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
              placeholder="Search easy trades... (/)"
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
            value={sortMode}
            onChange={(event) => setSortMode(event.target.value as SortMode)}
            className="h-[2.7rem] rounded-xl border border-[var(--pm-border)] bg-[var(--pm-card)] px-3 text-sm text-[var(--pm-text)] focus:outline-none focus:border-[var(--pm-accent)]"
            aria-label="Sort easy trades"
          >
            <option value="ease">Easiest first</option>
            <option value="ai_score">AI score</option>
            <option value="pnl">Edge potential</option>
            <option value="risk">Lowest risk</option>
            <option value="smart_money">Smart money</option>
          </select>

          <button
            type="button"
            onClick={() => triggerAiScan(true)}
            disabled={aiScanning || markets.length === 0}
            className="h-[2.7rem] rounded-xl border border-[var(--pm-border)] bg-[var(--pm-card)] px-4 text-sm font-semibold text-[var(--pm-text)] transition hover:border-[var(--pm-border-strong)] disabled:opacity-60"
          >
            {aiScanning
              ? `Finding easy trades ${aiProgress.done}/${aiProgress.total || "…"}`
              : "Find Easy Trades"}
          </button>
        </div>

        {recentTrades.length > 0 && (
          <div className="rounded-xl border border-[var(--pm-border)] bg-[var(--pm-card)] p-3">
            <div className="mb-2 flex items-center justify-between">
              <h2 className="text-sm font-semibold text-[var(--pm-text)]">
                Recent traded markets
              </h2>
              <span className="text-xs text-[var(--pm-text-soft)]">
                Quick re-entry
              </span>
            </div>
            <div className="flex gap-2 overflow-x-auto pb-1">
              {recentTrades.slice(0, 8).map((row) => (
                <button
                  key={`${row.market_id}-${row.last_traded_at}`}
                  type="button"
                  onClick={() => openTradeFromRecent(row)}
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
          {filteredEventGroups.length} events · {visibleOptionsCount} options
          {aiScanning
            ? ` · Finding easy trades ${aiProgress.done}/${aiProgress.total || "?"}…`
            : markets.some((market) => market.opportunityScore)
              ? " · Sorted by ease of trade"
              : ""}
        </p>

        {aiScanning && aiProgress.total > 0 && (
          <div className="space-y-1">
            <div className="h-2 w-full overflow-hidden rounded-full bg-[#e7ebf3]">
              <div
                className="h-full rounded-full bg-gradient-to-r from-[#4c6fff] to-[#4cc9f0] transition-all duration-300"
                style={{
                  width: `${(aiProgress.done / aiProgress.total) * 100}%`,
                }}
              />
            </div>
            <p className="text-xs text-[var(--pm-text-soft)]">
              Finding easy trades: {aiProgress.done} of {aiProgress.total}
            </p>
          </div>
        )}

        {aiScanError && (
          <div className="rounded-lg border border-red-200 bg-red-50 p-3 text-sm text-red-700">
            AI scan error: {aiScanError}
          </div>
        )}

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
            <p className="text-lg font-semibold text-[var(--pm-text)]">
              No markets found
            </p>
            <p className="mt-1 text-sm text-[var(--pm-text-soft)]">
              No active markets to scan right now. Check back later.
            </p>
          </div>
        ) : filteredEventGroups.length === 0 ? (
          <div className="rounded-xl border border-[var(--pm-border)] bg-[var(--pm-card)] px-6 py-14 text-center">
            <p className="text-lg font-semibold text-[var(--pm-text)]">
              No easy trades match current filters
            </p>
            <p className="mt-1 text-sm text-[var(--pm-text-soft)]">
              Adjust search/category or disable watchlist mode.
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
                    requireAuth(() =>
                      openTradeModal(item.primaryMarket, outcome),
                    )
                  }
                  onRowTrade={(rowId, outcome) => {
                    const market = item.rowMarketMap.get(rowId);
                    if (market)
                      requireAuth(() => openTradeModal(market, outcome));
                  }}
                  onOpenDetail={() => setSelectedGroup(item.group)}
                  onToggleFavorite={() =>
                    requireAuth(() =>
                      toggleMarketWatchlist(item.group.eventSlug),
                    )
                  }
                />
              ))}
            </div>

            {/* Infinite-scroll sentinel */}
            <div ref={loadMoreRef} className="flex justify-center py-6">
              {loadingMore && (
                <div className="flex items-center gap-2 text-sm text-[var(--pm-text-soft)]">
                  <svg
                    className="animate-spin h-4 w-4"
                    viewBox="0 0 24 24"
                    fill="none"
                  >
                    <circle
                      className="opacity-25"
                      cx="12"
                      cy="12"
                      r="10"
                      stroke="currentColor"
                      strokeWidth="4"
                    />
                    <path
                      className="opacity-75"
                      fill="currentColor"
                      d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z"
                    />
                  </svg>
                  Loading more markets…
                </div>
              )}
              {!hasMore && markets.length > 0 && (
                <p className="text-xs text-[var(--pm-text-soft)]">
                  All {markets.length} markets loaded
                </p>
              )}
            </div>
          </>
        )}
      </section>

      {selectedMarket && (
        <div
          className="fixed inset-0 modal-overlay z-50 flex justify-center p-4"
          style={{
            alignItems: "flex-start",
            paddingTop: `${popupOriginY * 0.55}vh`,
          }}
          onClick={closeDetailPopup}
        >
          <div
            ref={modalBodyRef}
            className="surface-panel max-w-3xl w-full max-h-[82vh] overflow-y-auto p-6 scroll-soft"
            onClick={(event) => event.stopPropagation()}
          >
            <div className="flex justify-between items-start gap-3 mb-4">
              <h2 className="text-xl font-bold pr-3 leading-snug">
                {selectedMarket.question || "Market analysis"}
              </h2>
              <div className="flex items-center gap-2 flex-shrink-0">
                <button
                  onClick={openInNewWindow}
                  className="btn-muted !px-3 !py-1.5 text-xs flex items-center gap-1.5"
                  title="Open in new window"
                >
                  <svg
                    className="w-3.5 h-3.5"
                    fill="none"
                    viewBox="0 0 24 24"
                    stroke="currentColor"
                    strokeWidth={2}
                  >
                    <path
                      strokeLinecap="round"
                      strokeLinejoin="round"
                      d="M13.5 6H5.25A2.25 2.25 0 003 8.25v10.5A2.25 2.25 0 005.25 21h10.5A2.25 2.25 0 0018 18.75V10.5m-10.5 6L21 3m0 0h-5.25M21 3v5.25"
                    />
                  </svg>
                  New Window
                </button>
                <button
                  onClick={closeDetailPopup}
                  className="btn-muted !px-3 !py-1.5 text-xs"
                >
                  Close
                </button>
              </div>
            </div>

            <div className="mb-3">
              {!streamDone ? (
                <span className="text-sm px-3 py-1.5 rounded inline-flex items-center gap-2 chip chip-warning">
                  <span className="animate-spin h-3.5 w-3.5 border-2 border-current border-t-transparent rounded-full inline-block"></span>
                  Streaming analysis…
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

            {streamError ? (
              <div className="p-4 alert-error rounded text-sm">
                {streamError}
              </div>
            ) : streamedText ? (
              <AnalysisDisplay text={streamedText} streaming={!streamDone} />
            ) : (
              <div className="flex items-center gap-2 text-muted italic text-sm py-8 justify-center">
                <span className="animate-spin h-4 w-4 border-2 border-current border-t-transparent rounded-full inline-block" />
                Connecting to AI backend…
              </div>
            )}
          </div>
        </div>
      )}

      {lastPriceUpdate && (
        <p className="text-xs text-[var(--pm-text-soft)]">
          Prices auto-refresh every {PRICE_POLL_INTERVAL / 1000}s · last update{" "}
          {lastPriceUpdate.toLocaleTimeString()}
        </p>
      )}

      {selectedGroup && (
        <EventGroupDetailDrawer
          group={selectedGroup}
          parsePrices={parseOutcomePrices}
          onClose={() => setSelectedGroup(null)}
          headerBadge={
            selectedGroupBest ? (
              <div className="flex flex-col items-end gap-1">
                <span className="chip chip-success px-2 py-0.5 text-[10px]">
                  AI {(selectedGroupBest.ai_score ?? 0).toFixed(0)}
                </span>
                {(selectedGroupPrimary?.label ||
                  selectedGroupBest.recommended_option) && (
                  <span className="text-[10px] text-emerald-300 max-w-[140px] truncate">
                    {selectedGroupPrimary?.label ||
                      selectedGroupBest.recommended_option}
                  </span>
                )}
              </div>
            ) : null
          }
          renderSubMarket={(market, subLabel) => {
            const prices = parseOutcomePrices(market);
            const score = market.opportunityScore;
            const endDate = market.endDate || market.end_date_iso;
            return (
              <div className="space-y-2">
                <div className="flex items-center justify-between gap-2">
                  <span className="text-sm font-medium text-white truncate">
                    {subLabel}
                  </span>
                  <div className="flex items-center gap-2">
                    <span className="chip chip-success px-2 py-0.5 text-xs">
                      Yes {(prices.yes * 100).toFixed(1)}c
                    </span>
                    <span className="chip chip-danger px-2 py-0.5 text-xs">
                      No {(prices.no * 100).toFixed(1)}c
                    </span>
                  </div>
                </div>

                {score && (
                  <div className="flex flex-wrap items-center gap-2 text-xs">
                    <span className="chip chip-success px-2 py-0.5">
                      AI {(score.ai_score ?? 0).toFixed(0)}
                    </span>
                    <span className="chip chip-warning px-2 py-0.5">
                      {recLabel(score.recommendation ?? "hold")}
                    </span>
                    <span className="chip chip-danger px-2 py-0.5">
                      {(score.risk_level || "unknown").toUpperCase()} risk
                    </span>
                  </div>
                )}

                <div className="flex items-center justify-between">
                  <div className="flex gap-3 text-xs text-muted">
                    <span>Vol: {fmtCompactUSD(market.volume24hr || 0)}</span>
                    {endDate && (
                      <span>
                        Ends: {new Date(endDate).toLocaleDateString()}
                      </span>
                    )}
                  </div>
                  <div className="flex gap-2">
                    <button
                      onClick={(event) => handleDetailAnalysis(market, event)}
                      className="btn-accent text-xs px-2 py-1"
                    >
                      AI Analysis
                    </button>
                    <button
                      onClick={() => requireAuth(() => openTradeModal(market))}
                      className="btn-success text-xs px-2 py-1"
                    >
                      Trade
                    </button>
                  </div>
                </div>
              </div>
            );
          }}
        />
      )}

      <TradeModal
        market={tradeMarket}
        source="opportunities"
        defaultOutcome={tradeDefaultOutcome}
        onClose={() => {
          setTradeMarket(null);
          setTradeDefaultOutcome(undefined);
        }}
      />
    </div>
  );
}
