import React, { useCallback, useEffect, useRef, useState } from "react";
import {
  portfolioService,
  PolymarketMarket,
} from "../services/portfolioService";
import {
  analysisService,
  AnalysisResponse,
  QuickAnalysisRequest,
  QuickGroupAnalysisRequest,
  QuickGroupAnalysisData,
  OpportunityScore,
} from "../services/analysisService";
import { getApiErrorMessage, sanitizeAIError } from "../utils/apiError";
import AnalysisDisplay, { AnalysisDisplayInline } from "./AnalysisDisplay";
import TradeModal, { TradeModalMarket } from "./TradeModal";
import {
  groupMarketsByEvent,
  EventGroup,
  resolvePrimaryGroupOption,
} from "../utils/groupMarkets";
import EventGroupPanel from "./EventGroupPanel";
import EventGroupDetailDrawer from "./EventGroupDetailDrawer";
import { buildPolymarketEventUrl } from "../utils/urlSafety";
import { useRafBufferedText } from "../hooks/useRafBufferedText";

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

type SortMode = "ai_score" | "pnl" | "risk" | "smart_money";

export default function Opportunities() {
  const [markets, setMarkets] = useState<MarketWithAnalysis[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [scanResult, setScanResult] = useState<AnalysisResponse | null>(null);
  const [scanning, setScanning] = useState(false);
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
  const [sortMode, setSortMode] = useState<SortMode>("ai_score");
  const aiAbortRef = useRef<AbortController | null>(null);
  /** Track whether the initial AI scan has already been triggered */
  const aiScanTriggered = useRef(false);

  const openTradeModal = (market: MarketWithAnalysis) => {
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
      tokens: [
        { token_id: yesTokenId, outcome: "Yes", price: prices.yes },
        { token_id: noTokenId, outcome: "No", price: prices.no },
      ],
    });
  };

  useEffect(() => {
    fetchMarkets();
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
    try {
      let limit = INITIAL_MARKETS_LIMIT;
      let fresh: MarketWithAnalysis[] = [];

      while (limit <= MAX_MARKETS_LIMIT) {
        const result = await portfolioService.getActiveMarkets(limit);
        fresh = (result.markets || []) as MarketWithAnalysis[];
        const groupedCount = groupMarketsByEvent(fresh).length;
        if (
          groupedCount >= MIN_VISIBLE_TRADE_PANELS ||
          fresh.length < limit ||
          limit === MAX_MARKETS_LIMIT
        ) {
          break;
        }
        limit += 30;
      }

      // Seed the previous-price map
      prevPricesRef.current.clear();
      fresh.forEach((m) => {
        const key = m.question || m.id || "";
        const p = parseOutcomePrices(m);
        prevPricesRef.current.set(key, p.yes);
      });
      setMarkets(fresh);
      setLastPriceUpdate(new Date());
    } catch (err: unknown) {
      setError(getApiErrorMessage(err, "Failed to load markets"));
    } finally {
      setLoading(false);
    }
  };

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
    };
  }, []);

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

  const handleScanAll = async () => {
    if (markets.length === 0) return;
    setScanning(true);
    setScanResult(null);
    setError(null);

    try {
      const marketsToScan = markets.slice(0, 20).map((m) => {
        const prices = parseOutcomePrices(m);
        return {
          title: m.question || "Unknown",
          yes_price: prices.yes,
          no_price: prices.no,
          volume_24h: m.volume24hr || 0,
          liquidity: m.liquidity || 0,
        };
      });
      const result = await analysisService.scanMarkets({
        markets: marketsToScan,
      });
      setScanResult(result);
    } catch (err: unknown) {
      const message = getApiErrorMessage(err, "Scan failed");
      setScanResult({
        success: false,
        data: { error: message },
        timestamp: new Date().toISOString(),
      });
      setError(message);
    } finally {
      setScanning(false);
    }
  };

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
    // Markets with AI scores float to top, unscored sink to bottom
    copy.sort((a, b) => {
      const aScore = a.opportunityScore;
      const bScore = b.opportunityScore;
      // Both unscored — keep original order
      if (!aScore && !bScore) return 0;
      if (!aScore) return 1;
      if (!bScore) return -1;
      switch (sortMode) {
        case "ai_score":
          return (bScore.ai_score ?? 0) - (aScore.ai_score ?? 0);
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
          const smOrder: Record<string, number> = {
            bullish: 2,
            neutral: 1,
            bearish: 0,
          };
          return (
            (smOrder[bScore.smart_money_signal ?? "neutral"] ?? 1) -
            (smOrder[aScore.smart_money_signal ?? "neutral"] ?? 1)
          );
        }
        default:
          return (bScore.ai_score ?? 0) - (aScore.ai_score ?? 0);
      }
    });
    return copy;
  }, [markets, sortMode]);

  // Group sorted markets by event for rendering
  const eventGroups = React.useMemo(
    () => groupMarketsByEvent(sortedMarkets),
    [sortedMarkets],
  );

  useEffect(() => {
    const activeSlugs = new Set(eventGroups.map((g) => g.eventSlug));
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

  /** Best AI score among an event group's sub-markets (for header badge) */
  const bestGroupScore = (group: EventGroup<MarketWithAnalysis>) => {
    let best: OpportunityScore | undefined;
    for (const m of group.markets) {
      if (
        m.opportunityScore &&
        (!best || (m.opportunityScore.ai_score ?? 0) > (best.ai_score ?? 0))
      ) {
        best = m.opportunityScore;
      }
    }
    return best;
  };

  // Helpers for rendering AI badges
  const scoreColor = (score: number) => {
    if (score >= 75)
      return "text-green-300 bg-green-500/20 border-green-500/30";
    if (score >= 50)
      return "text-yellow-300 bg-yellow-500/20 border-yellow-500/30";
    return "text-red-300 bg-red-500/20 border-red-500/30";
  };
  const riskEmoji = (level: string) => {
    if (level === "low") return "🟢";
    if (level === "medium") return "🟡";
    return "🔴";
  };
  const recColor = (rec: string) => {
    const r = rec.toLowerCase().replace(/_/g, " ");
    if (r.includes("strong buy") || r === "strong_buy") return "chip-success";
    if (r.includes("buy")) return "chip-success";
    if (r.includes("hold")) return "chip-warning";
    return "chip-danger";
  };
  const recLabel = (rec: string) => {
    const m: Record<string, string> = {
      strong_buy: "Strong Buy",
      buy: "Buy",
      hold: "Hold",
      avoid: "Avoid",
    };
    return m[rec] ?? rec;
  };

  const selectedGroupBest = selectedGroup ? bestGroupScore(selectedGroup) : null;
  const selectedGroupPrimary = selectedGroup
    ? resolvePrimaryGroupOption(selectedGroup, {
        parsePrices: parseOutcomePrices,
        preferredOptionLabel: selectedGroupBest?.recommended_option,
      })
    : null;

  if (loading) {
    return (
      <div className="space-y-6">
        <h1 className="text-3xl font-bold">Opportunities</h1>
        <p className="text-soft">Scanning markets for opportunities...</p>
        <div className="grid grid-cols-1 md:grid-cols-2 gap-4 items-start">
          {[1, 2, 3, 4].map((i) => (
            <div key={i} className="surface-panel p-6 animate-pulse">
              <div className="h-4 bg-[var(--bg-soft)] rounded w-3/4 mb-3"></div>
              <div className="h-3 bg-[var(--bg-soft)] rounded w-1/2 mb-2"></div>
              <div className="h-3 bg-[var(--bg-soft)] rounded w-1/3"></div>
            </div>
          ))}
        </div>
      </div>
    );
  }

  return (
    <div className="space-y-6">
      <div className="flex justify-between items-start">
        <div>
          <h1 className="text-3xl font-bold mb-2">Opportunities</h1>
          <p className="text-soft">
            {eventGroups.length} events · {markets.length} options
            {aiScanning
              ? ` · AI analyzing ${aiProgress.done}/${aiProgress.total || "?"}…`
              : markets.some((m) => m.opportunityScore)
                ? " · Sorted by AI analysis"
                : ' · Click "Analyze" for AI insights'}
          </p>
        </div>
        <div className="flex gap-3 items-center">
          {/* Sort dropdown */}
          <select
            value={sortMode}
            onChange={(e) => setSortMode(e.target.value as SortMode)}
            className="bg-[var(--bg-soft)] border border-[var(--line)] text-sm rounded px-2 py-1.5 text-[var(--text-secondary)]"
          >
            <option value="ai_score">Sort: AI Score</option>
            <option value="pnl">Sort: PNL Potential</option>
            <option value="risk">Sort: Lowest Risk</option>
            <option value="smart_money">Sort: Smart Money</option>
          </select>
          <button onClick={() => fetchMarkets()} className="btn-muted">
            Refresh
          </button>
          <button
            onClick={() => triggerAiScan(true)}
            disabled={aiScanning || markets.length === 0}
            className="btn-accent flex items-center gap-2"
          >
            {aiScanning && (
              <span className="animate-spin h-4 w-4 border-2 border-[#171b24] border-t-transparent rounded-full inline-block"></span>
            )}
            {aiScanning
              ? `Analyzing ${aiProgress.done}/${aiProgress.total || "…"}`
              : "Re-analyze All"}
          </button>
        </div>
      </div>

      {/* AI Scan Progress Bar */}
      {aiScanning && aiProgress.total > 0 && (
        <div className="space-y-1">
          <div className="w-full bg-[var(--bg-soft)] rounded-full h-2 overflow-hidden">
            <div
              className="h-full bg-gradient-to-r from-blue-500 to-purple-500 rounded-full transition-all duration-500"
              style={{
                width: `${(aiProgress.done / aiProgress.total) * 100}%`,
              }}
            />
          </div>
          <p className="text-xs text-muted">
            AI scanning markets: {aiProgress.done} of {aiProgress.total}{" "}
            analyzed
          </p>
        </div>
      )}

      {aiScanError && (
        <div className="p-3 alert-error rounded text-sm">
          AI scan error: {aiScanError}
        </div>
      )}

      {error && <div className="p-4 alert-error rounded text-sm">{error}</div>}

      {scanResult && (
        <div
          className={`p-5 rounded-lg border ${scanResult.success ? "bg-[var(--accent-soft)] border-[#f0b74166]" : "alert-error"}`}
        >
          <div className="flex justify-between items-center mb-3">
            <h2 className="text-lg font-semibold">AI Market Scan Results</h2>
            <span className="text-xs text-muted">
              via {scanResult.backend || "AI"} ·{" "}
              {new Date(scanResult.timestamp).toLocaleTimeString()}
            </span>
          </div>
          <div className="text-sm text-soft">
            {scanResult.success
              ? (() => {
                  const txt = stringifyAnalysisData(scanResult.data);
                  return <AnalysisDisplay text={txt} />;
                })()
              : stringifyAnalysisData(
                  (scanResult.data as Record<string, unknown>)?.error ||
                    "Scan failed",
                )}
          </div>
        </div>
      )}

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

            {/* Status chip */}
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

            {/* Streamed content */}
            {streamError ? (
              <div className="p-4 alert-error rounded text-sm">
                {streamError}
              </div>
            ) : streamedText ? (
              <>
                <AnalysisDisplay text={streamedText} streaming={!streamDone} />
              </>
            ) : (
              <div className="flex items-center gap-2 text-muted italic text-sm py-8 justify-center">
                <span className="animate-spin h-4 w-4 border-2 border-current border-t-transparent rounded-full inline-block" />
                Connecting to AI backend…
              </div>
            )}
          </div>
        </div>
      )}

      {/* Live-update timestamp */}
      {lastPriceUpdate && (
        <p className="text-xs text-muted -mt-3">
          Prices auto-refresh every {PRICE_POLL_INTERVAL / 1000}s · last update{" "}
          {lastPriceUpdate.toLocaleTimeString()}
        </p>
      )}

      <div className="grid grid-cols-1 md:grid-cols-2 gap-4 items-stretch">
        {eventGroups.map((group) => {
          // ── Single-market event → full rich card (unchanged from before) ──
          if (group.isSingle) {
            const market = group.markets[0];
            const prices = parseOutcomePrices(market);
            const flash =
              market.priceDirection === 1
                ? "ring-1 ring-green-400/60"
                : market.priceDirection === -1
                  ? "ring-1 ring-red-400/60"
                  : "";
            const polyUrl = getPolymarketUrl(market);
            const score = market.opportunityScore;
            const pnlPct =
              score?.pnl_potential ?? 1 - Math.min(prices.yes, prices.no);
            const highPnl = prices.yes < 0.9 && prices.no < 0.9;
            return (
              <div
                key={group.eventSlug}
                className={`surface-panel p-5 hover:border-[var(--line-strong)] transition-all duration-500 group relative flex flex-col w-full h-[460px] ${flash} ${highPnl ? "border-l-2 border-l-purple-500/60" : ""}`}
              >
                {score && (
                  <div
                    className={`absolute top-3 right-3 text-xs font-bold px-2 py-1 rounded border ${scoreColor(score.ai_score ?? 0)}`}
                  >
                    {(score.ai_score ?? 0).toFixed(0)} / 100
                  </div>
                )}
                {aiScanning && !score && (
                  <div className="absolute top-3 right-3">
                    <span className="animate-pulse text-xs text-muted italic">
                      scanning…
                    </span>
                  </div>
                )}

                <div className="block mb-3">
                  <h3 className="text-sm font-medium text-white leading-snug group-hover:text-blue-400 transition-colors pr-16">
                    {polyUrl ? (
                      <a
                        href={polyUrl}
                        target="_blank"
                        rel="noopener noreferrer"
                        className="cursor-pointer hover:opacity-80"
                      >
                        {market.question || "Unknown Market"}
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
                      market.question || "Unknown Market"
                    )}
                  </h3>
                </div>

                <div className="flex-1 min-h-0 overflow-y-auto pr-1 mb-3 space-y-3">
                  <div className="flex gap-3 flex-wrap">
                    <span
                      className={`chip chip-success px-2.5 py-1 tabular-nums transition-colors duration-500 ${
                        market.priceDirection === 1
                          ? "!bg-green-500/20 !text-green-300"
                          : ""
                      }`}
                    >
                      Yes {(prices.yes * 100).toFixed(1)}¢
                      {market.priceDirection === 1 && " ▲"}
                      {market.priceDirection === -1 && " ▼"}
                    </span>
                    <span
                      className={`chip chip-danger px-2.5 py-1 tabular-nums transition-colors duration-500 ${
                        market.priceDirection === -1
                          ? "!bg-red-500/20 !text-red-300"
                          : ""
                      }`}
                    >
                      No {(prices.no * 100).toFixed(1)}¢
                    </span>
                    {highPnl && (
                      <span className="chip px-2 py-1 text-purple-300 bg-purple-500/15 border border-purple-500/30 text-[10px] font-semibold">
                        High PNL {(pnlPct * 100).toFixed(0)}%
                      </span>
                    )}
                  </div>

                  <div className="flex gap-4 text-xs text-soft flex-wrap">
                    <span>
                      Vol: ${Number(market.volume24hr || 0).toLocaleString()}
                    </span>
                    <span>
                      Liq: ${Number(market.liquidity || 0).toLocaleString()}
                    </span>
                    {market.endDate && (
                      <span>
                        Ends: {new Date(market.endDate).toLocaleDateString()}
                      </span>
                    )}
                  </div>

                  {score && (
                    <div className="p-3 rounded bg-[var(--bg-soft)] border border-[var(--line)] space-y-2">
                      <div className="flex flex-wrap gap-2 text-xs">
                        <span title="Risk level">
                          {riskEmoji(score.risk_level ?? "high")}{" "}
                          {(score.risk_level ?? "unknown").toUpperCase()} risk
                        </span>
                        <span className="text-muted">•</span>
                        <span title="Credibility">
                          🎯 Credibility{" "}
                          {(score.credibility_score ?? 0).toFixed(0)}/10
                        </span>
                        <span className="text-muted">•</span>
                        <span title="Smart money signal">
                          🐋 Smart Money:{" "}
                          {(score.smart_money_signal ?? "neutral").toUpperCase()}
                        </span>
                      </div>
                      {score.smart_money_summary && (
                        <p className="text-xs text-soft leading-relaxed">
                          {score.smart_money_summary}
                        </p>
                      )}
                      <div className="flex items-center gap-2">
                        <span
                          className={`chip px-2 py-0.5 text-xs font-semibold ${recColor(score.recommendation ?? "hold")}`}
                        >
                          {recLabel(score.recommendation ?? "hold")}
                        </span>
                        {score.recommended_side && (
                          <span className="text-xs text-muted">
                            →{" "}
                            <strong className="text-white">
                              {score.recommended_side}
                            </strong>
                          </span>
                        )}
                      </div>
                      {score.reasoning && (
                        <p className="text-xs text-muted leading-relaxed line-clamp-2">
                          {score.reasoning}
                        </p>
                      )}
                    </div>
                  )}

                  {market.analysis && (
                    <div className="p-3 bg-[var(--accent-soft)] border border-[#f0b74166] rounded">
                      <AnalysisDisplayInline text={market.analysis} />
                    </div>
                  )}
                  {market.analysisError && (
                    <div className="p-3 alert-error rounded text-xs">
                      {market.analysisError}
                    </div>
                  )}
                </div>

                <div className="flex gap-2 mt-auto">
                  <button
                    onClick={() => handleQuickAnalysis(market)}
                    disabled={market.analysisLoading}
                    className="btn-muted text-xs flex items-center gap-1.5"
                  >
                    {market.analysisLoading ? (
                      <>
                        <span className="animate-spin h-3 w-3 border border-[var(--text-secondary)] border-t-transparent rounded-full inline-block"></span>
                        Analyzing...
                      </>
                    ) : (
                      "Quick Analyze"
                    )}
                  </button>
                  <button
                    onClick={(e) => handleDetailAnalysis(market, e)}
                    className="btn-accent text-xs"
                  >
                    Full Analysis
                  </button>
                  <button
                    onClick={() => openTradeModal(market)}
                    className="btn-success text-xs flex items-center gap-1"
                  >
                    <svg
                      className="w-3 h-3"
                      fill="none"
                      viewBox="0 0 24 24"
                      stroke="currentColor"
                      strokeWidth={2}
                    >
                      <path
                        strokeLinecap="round"
                        strokeLinejoin="round"
                        d="M12 6v12m-3-2.818.879.659c1.171.879 3.07.879 4.242 0 1.172-.879 1.172-2.303 0-3.182C13.536 12.219 12.768 12 12 12c-.725 0-1.45-.22-2.003-.659-1.106-.879-1.106-2.303 0-3.182s2.9-.879 4.006 0l.415.33M21 12a9 9 0 1 1-18 0 9 9 0 0 1 18 0Z"
                      />
                    </svg>
                    Trade
                  </button>
                </div>
              </div>
            );
          }

          // ── Multi-market event → collapsible group panel ──
          const groupBest = bestGroupScore(group);
          const primaryInGroup = resolvePrimaryGroupOption(group, {
            parsePrices: parseOutcomePrices,
            preferredOptionLabel: groupBest?.recommended_option,
          });
          const displayAiPick =
            primaryInGroup?.label || groupBest?.recommended_option;
          const groupQuick = groupQuickBySlug[group.eventSlug];
          const anyHighPnl = group.markets.some((m) => {
            const p = parseOutcomePrices(m);
            return p.yes < 0.9 && p.no < 0.9;
          });
          return (
            <EventGroupPanel
              key={group.eventSlug}
              group={group}
              parsePrices={parseOutcomePrices}
              onSelect={() => setSelectedGroup(group)}
              preferredOptionLabel={groupBest?.recommended_option}
              onTradePrimary={openTradeModal}
              onAnalyzePrimary={(market) => handleDetailAnalysis(market)}
              onQuickAnalyzeGroup={() => handleQuickGroupAnalysis(group)}
              quickAnalyzeLoading={groupQuick?.loading}
              quickAnalyzeError={groupQuick?.error}
              quickAnalyzeText={groupQuick?.analysis}
              quickCtaLabel="Quick Analyze"
              analyzeCtaLabel="AI Analysis"
              isSelected={selectedGroup?.eventSlug === group.eventSlug}
              className={`${anyHighPnl ? "border-l-2 border-l-purple-500/60" : ""} h-[460px]`}
              headerBadge={
                <>
                  {anyHighPnl && (
                    <span className="chip px-2 py-0.5 text-purple-300 bg-purple-500/15 border border-purple-500/30 text-[10px] font-semibold mr-1">
                      High PNL
                    </span>
                  )}
                  {groupBest && (
                    <div className="flex flex-col items-end gap-0.5">
                      <span
                        className={`text-xs font-bold px-2 py-1 rounded border ${scoreColor(groupBest.ai_score ?? 0)}`}
                      >
                        AI: {(groupBest.ai_score ?? 0).toFixed(0)}
                      </span>
                      {displayAiPick && (
                        <span
                          className="text-[9px] text-emerald-400 font-medium truncate max-w-[120px]"
                          title={`AI pick: ${displayAiPick}`}
                        >
                          ★ {displayAiPick}
                        </span>
                      )}
                    </div>
                  )}
                  {aiScanning && !groupBest && (
                    <span className="animate-pulse text-xs text-muted italic">
                      scanning…
                    </span>
                  )}
                </>
              }
            />
          );
        })}
      </div>

      {markets.length === 0 && !loading && (
        <div className="text-center py-12 text-soft">
          <p className="text-lg mb-2">No active markets found</p>
          <p className="text-sm text-muted">
            Check back later or refresh to try again.
          </p>
        </div>
      )}

      {selectedGroup && (
        <EventGroupDetailDrawer
          group={selectedGroup}
          parsePrices={parseOutcomePrices}
          onClose={() => setSelectedGroup(null)}
          headerBadge={
            <>
              {selectedGroup.markets.some((m) => {
                const p = parseOutcomePrices(m);
                return p.yes < 0.9 && p.no < 0.9;
              }) && (
                <span className="chip px-2 py-0.5 text-purple-300 bg-purple-500/15 border border-purple-500/30 text-[10px] font-semibold mr-1">
                  High PNL
                </span>
              )}
              {selectedGroupBest && (
                <div className="flex flex-col items-end gap-0.5">
                  <span
                    className={`text-xs font-bold px-2 py-1 rounded border ${scoreColor(selectedGroupBest.ai_score ?? 0)}`}
                  >
                    AI: {(selectedGroupBest.ai_score ?? 0).toFixed(0)}
                  </span>
                  {(selectedGroupPrimary?.label ||
                    selectedGroupBest.recommended_option) && (
                    <span
                      className="text-[9px] text-emerald-400 font-medium truncate max-w-[120px]"
                      title={`AI pick: ${selectedGroupPrimary?.label || selectedGroupBest.recommended_option}`}
                    >
                      ★{" "}
                      {selectedGroupPrimary?.label ||
                        selectedGroupBest.recommended_option}
                    </span>
                  )}
                </div>
              )}
            </>
          }
          renderSubMarket={(market, subLabel) => {
            const prices = parseOutcomePrices(market);
            const score = market.opportunityScore;
            const highPnl = prices.yes < 0.9 && prices.no < 0.9;
            return (
              <div className="space-y-2">
                {/* Sub-market label + prices */}
                <div className="flex items-center justify-between gap-2">
                  <div className="flex items-center gap-2 min-w-0">
                    <span className="text-sm font-medium text-white truncate">
                      {subLabel}
                    </span>
                    {highPnl && (
                      <span className="chip px-1.5 py-0.5 text-purple-300 bg-purple-500/15 border border-purple-500/30 text-[9px] font-semibold flex-shrink-0">
                        PNL
                      </span>
                    )}
                  </div>
                  <div className="flex items-center gap-2 flex-shrink-0">
                    {score && (
                      <span
                        className={`text-[10px] font-bold px-1.5 py-0.5 rounded border ${scoreColor(score.ai_score ?? 0)}`}
                      >
                        {(score.ai_score ?? 0).toFixed(0)}
                      </span>
                    )}
                    <span className="chip chip-success px-2 py-0.5 text-xs">
                      Yes {(prices.yes * 100).toFixed(1)}¢
                    </span>
                    <span className="chip chip-danger px-2 py-0.5 text-xs">
                      No {(prices.no * 100).toFixed(1)}¢
                    </span>
                  </div>
                </div>

                {/* Compact AI row for sub-markets */}
                {score && (
                  <div className="flex flex-wrap items-center gap-2 text-xs">
                    <span>
                      {riskEmoji(score.risk_level ?? "high")}{" "}
                      {(score.risk_level ?? "?").toUpperCase()}
                    </span>
                    <span className="text-muted">•</span>
                    <span>
                      🐋 {(score.smart_money_signal ?? "neutral").toUpperCase()}
                    </span>
                    <span
                      className={`chip px-1.5 py-0.5 text-[10px] font-semibold ${recColor(score.recommendation ?? "hold")}`}
                    >
                      {recLabel(score.recommendation ?? "hold")}
                    </span>
                    {score.recommended_side && (
                      <span className="text-muted text-[10px]">
                        → {score.recommended_side}
                      </span>
                    )}
                  </div>
                )}

                {/* Actions */}
                <div className="flex items-center justify-between">
                  <div className="flex gap-3 text-xs text-muted">
                    <span>
                      Vol: ${Number(market.volume24hr || 0).toLocaleString()}
                    </span>
                    {market.endDate && (
                      <span>
                        Ends: {new Date(market.endDate).toLocaleDateString()}
                      </span>
                    )}
                  </div>
                  <div className="flex gap-2">
                    <button
                      onClick={() => handleQuickAnalysis(market)}
                      disabled={market.analysisLoading}
                      className="btn-muted text-xs px-2 py-1"
                    >
                      {market.analysisLoading ? "…" : "Analyze"}
                    </button>
                    <button
                      onClick={(e) => handleDetailAnalysis(market, e)}
                      className="btn-accent text-xs px-2 py-1"
                    >
                      Full
                    </button>
                    <button
                      onClick={() => openTradeModal(market)}
                      className="btn-success text-xs px-2 py-1"
                    >
                      Trade
                    </button>
                  </div>
                </div>

                {/* Inline analysis if expanded */}
                {market.analysis && (
                  <div className="p-2 bg-[var(--accent-soft)] border border-[#f0b74166] rounded text-xs">
                    <AnalysisDisplayInline text={market.analysis} />
                  </div>
                )}
                {market.analysisError && (
                  <div className="p-2 alert-error rounded text-xs">
                    {market.analysisError}
                  </div>
                )}
              </div>
            );
          }}
        />
      )}

      <TradeModal market={tradeMarket} onClose={() => setTradeMarket(null)} />
    </div>
  );
}
