import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  marketsService,
  MarketCategory,
  BrowseMarket,
  TraderStats,
  TraderAnalysisRequest,
} from "../services/marketsService";
import { getApiErrorMessage } from "../utils/apiError";
import TradeModal, { TradeModalMarket } from "./TradeModal";
import { groupMarketsByEvent, EventGroup } from "../utils/groupMarkets";
import EventGroupPanel from "./EventGroupPanel";
import EventGroupDetailDrawer from "./EventGroupDetailDrawer";
import {
  buildPolymarketEventUrl,
  buildPolygonscanAddressUrl,
} from "../utils/urlSafety";
import { useRafBufferedText } from "../hooks/useRafBufferedText";

const PAGE_SIZE = 60;
const MIN_VISIBLE_TRADE_PANELS = 10;
const MAX_INITIAL_FETCH_PAGES = 4;

// ── Helpers ─────────────────────────────────────────────────────────

function parseOutcomePrices(market: BrowseMarket): { yes: number; no: number } {
  try {
    const bestAsk = market.bestAsk != null ? Number(market.bestAsk) : NaN;
    const bestBid = market.bestBid != null ? Number(market.bestBid) : NaN;
    const lastTrade =
      market.lastTradePrice != null ? Number(market.lastTradePrice) : NaN;

    let yesPrice = NaN;
    if (!isNaN(bestAsk) && bestAsk > 0 && bestAsk < 1) yesPrice = bestAsk;
    else if (!isNaN(bestBid) && bestBid > 0 && bestBid < 1) yesPrice = bestBid;
    else if (!isNaN(lastTrade) && lastTrade > 0 && lastTrade < 1)
      yesPrice = lastTrade;

    if (!isNaN(yesPrice))
      return { yes: yesPrice, no: Math.round((1 - yesPrice) * 10000) / 10000 };

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

const fmtUSD = (v: number) =>
  `$${Math.abs(v).toLocaleString("en-US", {
    minimumFractionDigits: 0,
    maximumFractionDigits: 0,
  })}`;

const fmtUSD2 = (v: number) =>
  `$${Math.abs(v).toLocaleString("en-US", {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  })}`;

// ════════════════════════════════════════════════════════════════════
// Component
// ════════════════════════════════════════════════════════════════════

export default function Markets() {
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

  // AI Trader Analysis popup
  const [analysisMarket, setAnalysisMarket] = useState<BrowseMarket | null>(
    null,
  );
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

  // Event group detail drawer
  const [selectedGroup, setSelectedGroup] =
    useState<EventGroup<BrowseMarket> | null>(null);

  const openTradeModal = (market: BrowseMarket) => {
    const prices = parseOutcomePrices(market);

    // Build outcome tokens – Polymarket markets always have Yes/No tokens
    const yesTokenId =
      market.tokens?.[0]?.token_id || market.condition_id || "";
    const noTokenId = market.tokens?.[1]?.token_id || "";

    setTradeMarket({
      market_id: market.condition_id || market.market_slug || "",
      title: market.question || market._event_title || "Market",
      image: market.image || market._event_image,
      tokens: [
        { token_id: yesTokenId, outcome: "Yes", price: prices.yes },
        { token_id: noTokenId, outcome: "No", price: prices.no },
      ],
    });
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

  // Auto-scroll streaming text inside its own container (not the page)
  useEffect(() => {
    const el = streamContainerRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [streamText]);

  // ── AI Trader Analysis (streaming) ────────────────────────────
  const openTraderAnalysis = useCallback((market: BrowseMarket) => {
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
      end_date:
        market.endDate || market.end_date_iso || new Date().toISOString(),
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
  }, [appendStreamText, flushStreamTextNow, resetStreamText]);

  const closeAnalysis = () => {
    abortRef.current?.abort();
    setAnalysisMarket(null);
    setTraderStats(null);
    resetStreamText();
    setStreamDone(false);
    setStreamError(null);
  };

  const eventGroups = useMemo(() => groupMarketsByEvent(markets), [markets]);
  const totalOptions = useMemo(
    () => eventGroups.reduce((sum, g) => sum + g.markets.length, 0),
    [eventGroups],
  );

  // ════════════════════════════════════════════════════════════════
  // RENDER
  // ════════════════════════════════════════════════════════════════
  return (
    <div className="space-y-6">
      {/* ── Header ─────────────────────────────────────────────── */}
      <div>
        <h1 className="text-3xl font-bold mb-1">Markets</h1>
        <p className="text-soft">
          Browse &amp; search all active Polymarket markets
        </p>
      </div>

      {/* ── Search bar ─────────────────────────────────────────── */}
      <div className="relative">
        <svg
          className="absolute left-3.5 top-1/2 -translate-y-1/2 w-5 h-5 text-soft pointer-events-none"
          fill="none"
          viewBox="0 0 24 24"
          stroke="currentColor"
          strokeWidth={2}
        >
          <path
            strokeLinecap="round"
            strokeLinejoin="round"
            d="M21 21l-5.197-5.197m0 0A7.5 7.5 0 105.196 5.196a7.5 7.5 0 0010.607 10.607z"
          />
        </svg>
        <input
          type="text"
          placeholder="Search markets... e.g. Bitcoin, Trump, Olympics"
          value={searchInput}
          onChange={(e) => setSearchInput(e.target.value)}
          className="w-full pl-11 pr-10 py-3 bg-[var(--bg-soft)] border border-[var(--line)] rounded-lg text-white placeholder:text-[var(--text-muted)] focus:outline-none focus:border-[var(--accent)] transition"
        />
        {searchInput && (
          <button
            onClick={() => setSearchInput("")}
            className="absolute right-3 top-1/2 -translate-y-1/2 text-soft hover:text-white transition"
          >
            <svg
              className="w-5 h-5"
              fill="none"
              viewBox="0 0 24 24"
              stroke="currentColor"
              strokeWidth={2}
            >
              <path
                strokeLinecap="round"
                strokeLinejoin="round"
                d="M6 18L18 6M6 6l12 12"
              />
            </svg>
          </button>
        )}
      </div>

      {/* ── Category chips + sort ──────────────────────────────── */}
      <div className="flex flex-wrap items-center gap-2">
        <button
          onClick={() => setSelectedTag("")}
          className={`px-3 py-1.5 rounded-full text-xs font-medium border transition ${
            selectedTag === ""
              ? "bg-[var(--accent)] text-black border-[var(--accent)]"
              : "border-[var(--line)] text-soft hover:text-white hover:border-[var(--line-strong)]"
          }`}
        >
          All
        </button>
        {categories.map((cat) => (
          <button
            key={cat.id}
            onClick={() => setSelectedTag(selectedTag === cat.id ? "" : cat.id)}
            className={`px-3 py-1.5 rounded-full text-xs font-medium border transition ${
              selectedTag === cat.id
                ? "bg-[var(--accent)] text-black border-[var(--accent)]"
                : "border-[var(--line)] text-soft hover:text-white hover:border-[var(--line-strong)]"
            }`}
          >
            {cat.label}
          </button>
        ))}

        {/* Sort dropdown */}
        <div className="ml-auto">
          <select
            value={sort}
            onChange={(e) => setSort(e.target.value)}
            className="bg-[var(--bg-soft)] border border-[var(--line)] rounded px-2 py-1.5 text-xs text-soft focus:outline-none focus:border-[var(--accent)]"
          >
            <option value="volume24hr">Volume 24h</option>
            <option value="liquidity">Liquidity</option>
            <option value="startDate">Newest</option>
          </select>
        </div>
      </div>

      {error && <div className="p-4 alert-error rounded text-sm">{error}</div>}

      {/* ── Loading skeleton ───────────────────────────────────── */}
      {loading ? (
        <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4 items-stretch">
          {Array.from({ length: 6 }).map((_, i) => (
            <div key={i} className="surface-panel p-5 animate-pulse space-y-3">
              <div className="flex gap-3">
                <div className="w-10 h-10 rounded bg-[var(--bg-soft)]" />
                <div className="flex-1 space-y-2">
                  <div className="h-3 bg-[var(--bg-soft)] rounded w-3/4" />
                  <div className="h-3 bg-[var(--bg-soft)] rounded w-1/2" />
                </div>
              </div>
              <div className="flex gap-2">
                <div className="h-6 bg-[var(--bg-soft)] rounded w-16" />
                <div className="h-6 bg-[var(--bg-soft)] rounded w-16" />
              </div>
              <div className="h-3 bg-[var(--bg-soft)] rounded w-2/3" />
            </div>
          ))}
        </div>
      ) : markets.length === 0 ? (
        /* ── Empty state ──────────────────────────────────────── */
        <div className="text-center py-16 surface-panel">
          <svg
            className="w-12 h-12 mx-auto text-soft mb-4"
            fill="none"
            viewBox="0 0 24 24"
            stroke="currentColor"
            strokeWidth={1.5}
          >
            <path
              strokeLinecap="round"
              strokeLinejoin="round"
              d="M21 21l-5.197-5.197m0 0A7.5 7.5 0 105.196 5.196a7.5 7.5 0 0010.607 10.607z"
            />
          </svg>
          <p className="text-soft text-lg mb-1">No markets found</p>
          <p className="text-muted text-sm">
            {debouncedQuery
              ? `No results for "${debouncedQuery}". Try a different search term.`
              : "Try a different category or check back later."}
          </p>
        </div>
      ) : (
        <>
          {/* ── Results count ─────────────────────────────────── */}
          <p className="text-xs text-muted">
            Showing {eventGroups.length} events / {totalOptions} options
            {total > 0 ? ` · ${total} total options` : ""}
          </p>

          {/* ── Market grid ───────────────────────────────────── */}
          <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4 items-stretch">
            {eventGroups.map((group) => {
              // ── Single-market event → regular card (no grouping chrome) ──
              if (group.isSingle) {
                const market = group.markets[0];
                const prices = parseOutcomePrices(market);
                const vol =
                  market.volume24hr || market.volumeNum || market.volume || 0;
                const liq = market.liquidity || 0;
                const endDate = market.endDate || market.end_date_iso;
                const imgUrl = market.image || market._event_image;
                const polyUrl = getPolymarketUrl(market);
                return (
                  <div
                    key={group.eventSlug}
                    className="surface-panel p-5 hover:border-[var(--line-strong)] transition group flex flex-col w-full h-[320px]"
                  >
                    <div
                      className={`flex gap-3 mb-3 ${polyUrl ? "cursor-pointer hover:opacity-80" : ""}`}
                    >
                      {imgUrl && (
                        <img
                          src={imgUrl}
                          alt=""
                          className="w-10 h-10 rounded object-cover flex-shrink-0 mt-0.5"
                          onError={(e) => {
                            (e.target as HTMLImageElement).style.display =
                              "none";
                          }}
                        />
                      )}
                      <h3 className="text-sm font-medium text-white leading-snug line-clamp-3 group-hover:text-blue-400 transition-colors">
                        {polyUrl ? (
                          <a
                            href={polyUrl}
                            target="_blank"
                            rel="noopener noreferrer"
                            className="hover:opacity-80"
                          >
                            {market.question ||
                              market._event_title ||
                              "Unknown Market"}
                            <svg
                              className="inline-block w-3 h-3 ml-1 opacity-0 group-hover:opacity-60 transition-opacity"
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
                          </a>
                        ) : (
                          market.question || market._event_title || "Unknown Market"
                        )}
                      </h3>
                    </div>
                    <div className="flex gap-3 mb-3">
                      <span className="chip chip-success px-2.5 py-1">
                        Yes {(prices.yes * 100).toFixed(0)}c
                      </span>
                      <span className="chip chip-danger px-2.5 py-1">
                        No {(prices.no * 100).toFixed(0)}c
                      </span>
                    </div>
                    <div className="flex gap-4 text-xs text-soft mb-4">
                      {vol > 0 && <span>Vol: {fmtUSD(vol)}</span>}
                      {liq > 0 && <span>Liq: {fmtUSD(liq)}</span>}
                      {endDate && (
                        <span>
                          Ends: {new Date(endDate).toLocaleDateString()}
                        </span>
                      )}
                    </div>
                    <div className="flex gap-2 mt-auto">
                      <button
                        onClick={() => openTradeModal(market)}
                        className="btn-success text-xs flex items-center gap-1.5"
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
                            d="M12 6v12m-3-2.818l.879.659c1.171.879 3.07.879 4.242 0 1.172-.879 1.172-2.303 0-3.182C13.536 12.219 12.768 12 12 12c-.725 0-1.45-.22-2.003-.659-1.106-.879-1.106-2.303 0-3.182s2.9-.879 4.006 0l.415.33M21 12a9 9 0 11-18 0 9 9 0 0118 0z"
                          />
                        </svg>
                        Trade
                      </button>
                      <button
                        onClick={() => openTraderAnalysis(market)}
                        className="btn-accent text-xs flex items-center gap-1.5"
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
                            d="M9.813 15.904L9 18.75l-.813-2.846a4.5 4.5 0 00-3.09-3.09L2.25 12l2.846-.813a4.5 4.5 0 003.09-3.09L9 5.25l.813 2.846a4.5 4.5 0 003.09 3.09L15.75 12l-2.846.813a4.5 4.5 0 00-3.09 3.09zM18.259 8.715L18 9.75l-.259-1.035a3.375 3.375 0 00-2.455-2.456L14.25 6l1.036-.259a3.375 3.375 0 002.455-2.456L18 2.25l.259 1.035a3.375 3.375 0 002.455 2.456L21.75 6l-1.036.259a3.375 3.375 0 00-2.455 2.456zM16.894 20.567L16.5 21.75l-.394-1.183a2.25 2.25 0 00-1.423-1.423L13.5 18.75l1.183-.394a2.25 2.25 0 001.423-1.423l.394-1.183.394 1.183a2.25 2.25 0 001.423 1.423l1.183.394-1.183.394a2.25 2.25 0 00-1.423 1.423z"
                          />
                        </svg>
                        AI Analysis
                      </button>
                    </div>
                  </div>
                );
              }

              // ── Multi-market event → compact group card (opens drawer) ──
              return (
                <EventGroupPanel
                  key={group.eventSlug}
                  group={group}
                  parsePrices={parseOutcomePrices}
                  onSelect={() => setSelectedGroup(group)}
                  onTradePrimary={openTradeModal}
                  onAnalyzePrimary={openTraderAnalysis}
                  isSelected={selectedGroup?.eventSlug === group.eventSlug}
                />
              );
            })}
          </div>

          {/* ── Load More ─────────────────────────────────────── */}
          {hasMore && (
            <div className="text-center pt-2">
              <button
                onClick={() => fetchMarkets(markets.length)}
                disabled={loadingMore}
                className="btn-muted px-6 py-2 text-sm"
              >
                {loadingMore ? (
                  <span className="flex items-center gap-2">
                    <span className="animate-spin h-4 w-4 border-2 border-current border-t-transparent rounded-full" />
                    Loading...
                  </span>
                ) : (
                  "Load More"
                )}
              </button>
            </div>
          )}
        </>
      )}

      {/* Trade Modal */}
      <TradeModal market={tradeMarket} onClose={() => setTradeMarket(null)} />

      {/* ═══ Event Group Detail Drawer ═══ */}
      {selectedGroup && (
        <EventGroupDetailDrawer
          group={selectedGroup}
          parsePrices={parseOutcomePrices}
          onClose={() => setSelectedGroup(null)}
          renderSubMarket={(market, subLabel) => {
            const prices = parseOutcomePrices(market);
            const vol =
              market.volume24hr || market.volumeNum || market.volume || 0;
            const endDate = market.endDate || market.end_date_iso;
            return (
              <div className="space-y-2">
                <div className="flex items-center justify-between gap-2">
                  <span className="text-sm font-medium text-white truncate">
                    {subLabel}
                  </span>
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
                    {vol > 0 && <span>Vol: {fmtUSD(vol)}</span>}
                    {endDate && (
                      <span>
                        Ends: {new Date(endDate).toLocaleDateString()}
                      </span>
                    )}
                  </div>
                  <div className="flex gap-2">
                    <button
                      onClick={() => openTradeModal(market)}
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
                {analysisMarket.question ||
                  analysisMarket._event_title ||
                  "Market"}
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
                <StatsCard
                  label="NO Traders"
                  value={String(traderStats.no_traders)}
                  accent="red"
                />
                <StatsCard
                  label="YES Volume"
                  value={fmtUSD2(traderStats.yes_volume)}
                  accent="emerald"
                />
                <StatsCard
                  label="NO Volume"
                  value={fmtUSD2(traderStats.no_volume)}
                  accent="red"
                />
              </div>
            )}

            {/* Volume split bar */}
            {traderStats && traderStats.total_trades > 0 && (
              <div className="mb-4">
                <div className="flex items-center justify-between text-xs text-soft mb-1.5">
                  <span>YES {traderStats.side_ratio.yes.toFixed(1)}%</span>
                  <span className="text-muted">
                    {traderStats.total_trades} trades
                  </span>
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
                        <td className="p-2 text-right mono text-red-400">
                          {fmtUSD2(t.no_volume)}
                        </td>
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
              <div className="p-4 alert-error rounded text-sm">
                {streamError}
              </div>
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
                  {streamText.length > 300
                    ? "…" + streamText.slice(-300)
                    : streamText}
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
  if (t.includes("BUY YES"))
    return { label: "BUY YES", cls: "bg-emerald-500/15 text-emerald-400" };
  if (t.includes("BUY NO"))
    return { label: "BUY NO", cls: "bg-red-500/15 text-red-400" };
  if (t.includes("HOLD"))
    return { label: "HOLD", cls: "bg-yellow-500/15 text-yellow-400" };
  if (t.includes("SELL"))
    return { label: "SELL", cls: "bg-red-500/15 text-red-400" };
  return null;
}

/** Determine confidence badge color */
function confBadge(text: string): { label: string; cls: string } | null {
  const t = text.toLowerCase();
  if (t.startsWith("high"))
    return { label: "High", cls: "bg-emerald-500/15 text-emerald-400" };
  if (t.startsWith("medium") || t.startsWith("moderate"))
    return { label: "Medium", cls: "bg-yellow-500/15 text-yellow-400" };
  if (t.startsWith("low"))
    return { label: "Low", cls: "bg-red-500/15 text-red-400" };
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
                    <li
                      key={i}
                      className="flex items-start gap-2 text-sm text-soft"
                    >
                      <span
                        className={`mt-1.5 w-1.5 h-1.5 rounded-full flex-shrink-0 ${
                          field.key === "risk_factors"
                            ? "bg-red-400"
                            : "bg-[var(--accent)]"
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
                  <p
                    className={`text-sm leading-relaxed ${field.accent || "text-soft"}`}
                  >
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
      <p className="text-muted text-[10px] uppercase tracking-wider mb-1">
        {label}
      </p>
      <p className={`text-lg font-bold ${textColor}`}>{value}</p>
    </div>
  );
}
