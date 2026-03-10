import { useEffect, useState, useRef, useCallback } from "react";
import {
  tradesService,
  TradeRecord,
  TradeHistoryResponse,
} from "../services/tradesService";
import {
  portfolioService,
  PortfolioSummary,
} from "../services/portfolioService";
import { stopLossService, StopLossOrder } from "../services/stopLossService";
import {
  takeProfitService,
  TakeProfitOrder,
} from "../services/takeProfitService";
import {
  inverseBotService,
  InverseBotPosition,
  InverseBotSizeOverride,
} from "../services/inverseBotService";
import { analysisService } from "../services/analysisService";
import { useAuthStore } from "../store/authStore";
import { getApiErrorMessage } from "../utils/apiError";
import {
  buildPolymarketEventUrl,
  buildPolygonscanAddressUrl,
  buildPolygonscanTxUrl,
} from "../utils/urlSafety";
import CashOutModal from "./CashOutModal";
import StopLossModal from "./StopLossModal";
import TakeProfitModal from "./TakeProfitModal";

type FilterType = "all" | "buy" | "sell" | "confirmed" | "matched";
type TabType = "history" | "positions";

const PRICE_POLL_INTERVAL = 15_000;
const STOP_LOSS_POLL_INTERVAL = 10_000;

type InverseExplanationView = {
  decision: string;
  why: string;
  confidence: string;
  market_signal: string;
  web_signal: string;
  x_signal: string;
  next_checks: string;
  updated_at: string;
};

export default function TradeHistory() {
  const { walletAddress, isAuthenticated } = useAuthStore();
  const [activeTab, setActiveTab] = useState<TabType>("history");

  // --- Trade History State ---
  const [trades, setTrades] = useState<TradeRecord[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [hasMore, setHasMore] = useState(false);
  const [offset, setOffset] = useState(0);
  const [filter, setFilter] = useState<FilterType>("all");
  const [expandedId, setExpandedId] = useState<string | null>(null);
  const LIMIT = 50;

  // --- Positions State ---
  const [portfolio, setPortfolio] = useState<PortfolioSummary | null>(null);
  const [posLoading, setPosLoading] = useState(false);
  const [posError, setPosError] = useState<string | null>(null);
  const [flashIds, setFlashIds] = useState<Set<string>>(new Set());
  const prevPricesRef = useRef<Record<string, number>>({});
  const flashResetTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  // --- Modal state ---
  const [cashOutPosition, setCashOutPosition] = useState<any | null>(null);
  const [stopLossPosition, setStopLossPosition] = useState<any | null>(null);
  const [takeProfitPosition, setTakeProfitPosition] = useState<any | null>(
    null,
  );

  // --- Active stop-losses & take-profits ---
  const [activeStopLosses, setActiveStopLosses] = useState<StopLossOrder[]>([]);
  const [activeTakeProfits, setActiveTakeProfits] = useState<TakeProfitOrder[]>(
    [],
  );
  const [inverseBotPositions, setInverseBotPositions] = useState<
    InverseBotPosition[]
  >([]);
  const [inverseBusyToken, setInverseBusyToken] = useState<string | null>(null);
  const [inverseInfoHoverToken, setInverseInfoHoverToken] = useState<
    string | null
  >(null);
  const [inverseInfoPinnedToken, setInverseInfoPinnedToken] = useState<
    string | null
  >(null);
  const [inverseExplainByToken, setInverseExplainByToken] = useState<
    Record<string, InverseExplanationView>
  >({});
  const [inverseExplainLoadingToken, setInverseExplainLoadingToken] = useState<
    string | null
  >(null);
  const [inverseExplainErrorByToken, setInverseExplainErrorByToken] = useState<
    Record<string, string>
  >({});

  useEffect(
    () => () => {
      if (flashResetTimerRef.current) {
        clearTimeout(flashResetTimerRef.current);
      }
    },
    [],
  );

  useEffect(() => {
    fetchTrades(0);
  }, []);

  // Fetch positions when tab switches to positions
  useEffect(() => {
    if (activeTab === "positions" && !portfolio && isAuthenticated) {
      fetchPositions();
    }
  }, [activeTab, isAuthenticated]);

  // Fetch active stop-losses
  const fetchStopLosses = useCallback(async () => {
    try {
      const orders = await stopLossService.getStopLosses("active");
      setActiveStopLosses(orders);
    } catch {
      // ignore
    }
  }, []);

  const fetchTakeProfits = useCallback(async () => {
    try {
      const orders = await takeProfitService.getTakeProfits("active");
      setActiveTakeProfits(orders);
    } catch {
      // ignore
    }
  }, []);

  const fetchInverseBotPositions = useCallback(async () => {
    try {
      const rows = await inverseBotService.listInverseBotPositions();
      setInverseBotPositions(rows);
    } catch {
      // ignore
    }
  }, []);

  // Poll active stop-losses & take-profits
  useEffect(() => {
    if (!isAuthenticated || activeTab !== "positions") return;
    fetchStopLosses();
    fetchTakeProfits();
    const id = setInterval(() => {
      fetchStopLosses();
      fetchTakeProfits();
    }, STOP_LOSS_POLL_INTERVAL);
    return () => clearInterval(id);
  }, [isAuthenticated, activeTab, fetchStopLosses, fetchTakeProfits]);

  useEffect(() => {
    if (!isAuthenticated || activeTab !== "positions") return;
    fetchInverseBotPositions();
  }, [isAuthenticated, activeTab, fetchInverseBotPositions]);

  /** Find active stop-loss for a position by token_id */
  const getStopLossForPosition = (pos: any): StopLossOrder | undefined => {
    const tokenId = pos.asset_id || pos.token_id || pos.condition_id || "";
    return activeStopLosses.find((sl) => sl.token_id === tokenId);
  };

  const getTakeProfitForPosition = (pos: any): TakeProfitOrder | undefined => {
    const tokenId = pos.asset_id || pos.token_id || pos.condition_id || "";
    return activeTakeProfits.find((tp) => tp.token_id === tokenId);
  };

  const getInverseBotForPosition = (
    pos: any,
  ): InverseBotPosition | undefined => {
    const tokenId = String(pos.asset_id || pos.token_id || "");
    return inverseBotPositions.find((row) => row.token_id === tokenId);
  };

  const upsertInverseBotForPosition = async (
    pos: any,
    overrides?: Partial<{
      enabled: boolean;
      size_mode_override: InverseBotSizeOverride;
      fixed_amount_override: number | null;
    }>,
  ) => {
    const tokenId = String(pos.asset_id || pos.token_id || "");
    const conditionId = String(pos.condition_id || pos.conditionId || "");
    if (!tokenId || !conditionId) {
      setPosError("Missing token or condition id for inverse bot.");
      return null;
    }
    const existing = getInverseBotForPosition(pos);
    const payload = {
      token_id: tokenId,
      condition_id: conditionId,
      market_title: String(pos.title || pos.market || ""),
      outcome: String(pos.outcome || ""),
      enabled: overrides?.enabled ?? existing?.enabled ?? true,
      size_mode_override:
        overrides?.size_mode_override ??
        existing?.size_mode_override ??
        "inherit",
      fixed_amount_override:
        overrides?.fixed_amount_override ??
        existing?.fixed_amount_override ??
        null,
    };
    setInverseBusyToken(tokenId);
    try {
      await inverseBotService.upsertInverseBotPosition(payload);
      await fetchInverseBotPositions();
      return true;
    } catch (err: unknown) {
      setPosError(
        getApiErrorMessage(err, "Failed to update inverse bot settings"),
      );
      return false;
    } finally {
      setInverseBusyToken(null);
    }
  };

  const toggleInverseBot = async (pos: any, enabled: boolean) => {
    const tokenId = String(pos.asset_id || pos.token_id || "");
    const existing = getInverseBotForPosition(pos);
    if (!enabled) {
      setInverseExplainByToken((prev) => {
        const next = { ...prev };
        delete next[tokenId];
        return next;
      });
      setInverseExplainErrorByToken((prev) => {
        const next = { ...prev };
        delete next[tokenId];
        return next;
      });
    }
    if (!enabled && existing?.id) {
      setInverseBusyToken(tokenId);
      try {
        await inverseBotService.disableInverseBotPosition(existing.id);
        await fetchInverseBotPositions();
      } catch (err: unknown) {
        setPosError(getApiErrorMessage(err, "Failed to disable inverse bot"));
      } finally {
        setInverseBusyToken(null);
      }
      return;
    }
    await upsertInverseBotForPosition(pos, { enabled });
  };

  const runInverseNow = async (pos: any) => {
    const tokenId = String(pos.asset_id || pos.token_id || "");
    setInverseExplainByToken((prev) => {
      const next = { ...prev };
      delete next[tokenId];
      return next;
    });
    setInverseExplainErrorByToken((prev) => {
      const next = { ...prev };
      delete next[tokenId];
      return next;
    });
    setInverseBusyToken(tokenId);
    try {
      let rowId = getInverseBotForPosition(pos)?.id;
      if (!rowId) {
        const conditionId = String(pos.condition_id || pos.conditionId || "");
        const created = await inverseBotService.upsertInverseBotPosition({
          token_id: tokenId,
          condition_id: conditionId,
          market_title: String(pos.title || pos.market || ""),
          outcome: String(pos.outcome || ""),
          enabled: true,
          size_mode_override: "inherit",
          fixed_amount_override: null,
        });
        rowId = created.id;
      }
      if (!rowId) {
        throw new Error("Position is not enabled for inverse bot");
      }
      const result = await inverseBotService.evaluateInverseBotPosition(rowId);
      if (!result.success) {
        throw new Error(result.detail || "Manual evaluation failed");
      }
      await fetchInverseBotPositions();
    } catch (err: unknown) {
      setPosError(getApiErrorMessage(err, "Failed to run inverse evaluation"));
    } finally {
      setInverseBusyToken(null);
    }
  };

  const inverseStatusClass = (status: string | null | undefined) => {
    if (status === "cooldown") return "chip-warning";
    if (status === "sell_only") return "chip-danger";
    if (status === "error") return "chip-danger";
    return "chip-success";
  };

  const ensureInverseExplanation = async (
    tokenId: string,
    pos: any,
    inverse?: InverseBotPosition,
  ) => {
    if (!inverse) return;
    if (
      inverseExplainByToken[tokenId] ||
      inverseExplainLoadingToken === tokenId
    ) {
      return;
    }
    if (
      !inverse.last_evaluated_at &&
      inverse.last_confidence == null &&
      !inverse.last_reasoning
    ) {
      setInverseExplainByToken((prev) => ({
        ...prev,
        [tokenId]: {
          decision: "Not evaluated yet",
          why: "Click Run Now and the bot will generate the first evaluation.",
          confidence: "N/A",
          market_signal: "N/A",
          web_signal: "N/A",
          x_signal: "N/A",
          next_checks: "Run a manual evaluation.",
          updated_at: "N/A",
        },
      }));
      return;
    }

    const fallbackExplain: InverseExplanationView = {
      decision: String(inverse.last_recommendation || "hold"),
      why: String(inverse.last_reasoning || "No model reasoning available."),
      confidence:
        inverse.last_confidence != null
          ? `${inverse.last_confidence.toFixed(0)}%`
          : "Unknown",
      market_signal: String(inverse.last_signal || "No signal captured"),
      web_signal: String(inverse.last_web_summary || "No web summary"),
      x_signal: String(inverse.last_x_summary || "No X summary"),
      next_checks: "Monitor next cycle and confirm confidence stays stable.",
      updated_at: inverse.last_evaluated_at
        ? new Date(inverse.last_evaluated_at).toLocaleString()
        : "Unknown",
    };

    const prompt = [
      "Convert this inverse position bot evaluation into strict JSON only.",
      "Do NOT use markdown and do NOT use bullet points.",
      "Use concise plain-English text for each field.",
      "Return exactly this JSON schema:",
      '{"decision":"", "why":"", "confidence":"", "market_signal":"", "web_signal":"", "x_signal":"", "next_checks":"", "updated_at":""}',
      `Market: ${String(pos.title || pos.market || inverse.market_title || "Unknown market")}`,
      `Held outcome: ${String(pos.outcome || inverse.outcome || "Unknown")}`,
      `Bot recommendation: ${String(inverse.last_recommendation || "unknown")}`,
      `Confidence: ${inverse.last_confidence != null ? `${inverse.last_confidence.toFixed(0)}%` : "unknown"}`,
      `Status: ${String(inverse.status || "unknown")}`,
      `Reasoning: ${String(inverse.last_reasoning || "No model reasoning available.")}`,
      `Web summary: ${String(inverse.last_web_summary || "None")}`,
      `X summary: ${String(inverse.last_x_summary || "None")}`,
      `Last evaluated at: ${inverse.last_evaluated_at || "unknown"}`,
    ].join("\n");

    setInverseExplainLoadingToken(tokenId);
    setInverseExplainErrorByToken((prev) => ({ ...prev, [tokenId]: "" }));
    try {
      const resp = await analysisService.quickAnalysis({
        question: prompt,
        current_price: Math.max(
          0,
          Math.min(1, (inverse.last_confidence ?? 50) / 100),
        ),
      });
      const text = String(resp?.data?.analysis || "");
      const match = text.match(/\{[\s\S]*\}/);
      const parsed = match ? JSON.parse(match[0]) : null;
      setInverseExplainByToken((prev) => ({
        ...prev,
        [tokenId]:
          parsed && typeof parsed === "object"
            ? {
                decision: String(parsed.decision || fallbackExplain.decision),
                why: String(parsed.why || fallbackExplain.why),
                confidence: String(
                  parsed.confidence || fallbackExplain.confidence,
                ),
                market_signal: String(
                  parsed.market_signal || fallbackExplain.market_signal,
                ),
                web_signal: String(
                  parsed.web_signal || fallbackExplain.web_signal,
                ),
                x_signal: String(parsed.x_signal || fallbackExplain.x_signal),
                next_checks: String(
                  parsed.next_checks || fallbackExplain.next_checks,
                ),
                updated_at: String(
                  parsed.updated_at || fallbackExplain.updated_at,
                ),
              }
            : fallbackExplain,
      }));
    } catch (err: unknown) {
      setInverseExplainByToken((prev) => ({
        ...prev,
        [tokenId]: fallbackExplain,
      }));
      setInverseExplainErrorByToken((prev) => ({
        ...prev,
        [tokenId]: getApiErrorMessage(err, "Failed to generate explanation"),
      }));
    } finally {
      setInverseExplainLoadingToken((prev) => (prev === tokenId ? null : prev));
    }
  };

  // Real-time price polling for positions
  useEffect(() => {
    if (
      activeTab !== "positions" ||
      !isAuthenticated ||
      !portfolio?.positions?.length
    )
      return;

    const poll = async () => {
      try {
        const { prices } = await portfolioService.refreshPositionPrices();
        if (!prices || Object.keys(prices).length === 0) return;

        setPortfolio((prev) => {
          if (!prev) return prev;
          const changed = new Set<string>();
          const updated = prev.positions.map((pos: any) => {
            const aid = pos.asset_id;
            if (!aid || !(aid in prices)) return pos;
            const newPrice = prices[aid];
            const oldPrice = prevPricesRef.current[aid] ?? pos.curPrice ?? 0;
            if (Math.abs(newPrice - oldPrice) > 0.0001) {
              changed.add(aid);
            }
            prevPricesRef.current[aid] = newPrice;

            const size = Number(pos.size || 0);
            const avgPrice = Number(pos.avgPrice || 0);
            const invested = size * avgPrice;
            const value = size * newPrice;
            return {
              ...pos,
              curPrice: newPrice,
              pnl: +(value - invested).toFixed(4),
            };
          });

          if (changed.size > 0) {
            setFlashIds(changed);
            if (flashResetTimerRef.current) {
              clearTimeout(flashResetTimerRef.current);
            }
            flashResetTimerRef.current = setTimeout(() => {
              setFlashIds(new Set());
              flashResetTimerRef.current = null;
            }, 900);
          }

          let totalInvested = 0;
          let totalValue = 0;
          let active = 0;
          for (const p of updated) {
            const s = Number((p as any).size || 0);
            const ap = Number((p as any).avgPrice || 0);
            const cp = Number((p as any).curPrice || ap);
            totalInvested += s * ap;
            totalValue += s * cp;
            if (s > 0) active++;
          }
          const totalPnl = totalValue - totalInvested;
          const pnlPct =
            totalInvested > 0 ? (totalPnl / totalInvested) * 100 : 0;

          return {
            ...prev,
            positions: updated,
            active_positions: active,
            total_invested: +totalInvested.toFixed(2),
            total_current_value: +totalValue.toFixed(2),
            total_pnl: +totalPnl.toFixed(2),
            pnl_percentage: +pnlPct.toFixed(2),
          };
        });
      } catch {
        // Silently ignore price poll errors
      }
    };

    const id = setInterval(poll, PRICE_POLL_INTERVAL);
    return () => clearInterval(id);
  }, [activeTab, isAuthenticated, portfolio?.positions?.length]);

  const fetchPositions = async () => {
    setPosLoading(true);
    setPosError(null);
    try {
      const summary = await portfolioService.getSummary();
      setPortfolio(summary);
    } catch (err: unknown) {
      setPosError(getApiErrorMessage(err, "Failed to load positions"));
    } finally {
      setPosLoading(false);
    }
  };

  const fetchTrades = async (newOffset: number) => {
    setLoading(true);
    setError(null);
    try {
      const result: TradeHistoryResponse = await tradesService.getTradeHistory(
        LIMIT,
        newOffset,
      );
      if (newOffset === 0) {
        setTrades(result.trades);
      } else {
        setTrades((prev) => [...prev, ...result.trades]);
      }
      setHasMore(result.has_more);
      setOffset(newOffset);
    } catch (err: unknown) {
      setError(getApiErrorMessage(err, "Failed to load trade history"));
    } finally {
      setLoading(false);
    }
  };

  const loadMore = () => fetchTrades(offset + LIMIT);

  const filteredTrades =
    filter === "all"
      ? trades
      : filter === "buy" || filter === "sell"
        ? trades.filter(
            (t) => (t.side || "").toUpperCase() === filter.toUpperCase(),
          )
        : trades.filter(
            (t) => (t.status || "").toUpperCase() === filter.toUpperCase(),
          );

  const formatUSD = (v: number) =>
    `$${Math.abs(v).toLocaleString("en-US", {
      minimumFractionDigits: 2,
      maximumFractionDigits: 2,
    })}`;

  const formatPnL = (value: number) => {
    const prefix = value >= 0 ? "+" : "";
    return `${prefix}${formatUSD(value)}`;
  };

  const formatDate = (ts?: string) => {
    if (!ts) return "—";
    const d = new Date(ts);
    if (isNaN(d.getTime())) return ts;
    return d.toLocaleString("en-US", {
      month: "short",
      day: "numeric",
      hour: "2-digit",
      minute: "2-digit",
    });
  };

  const shortAddr = walletAddress
    ? `${walletAddress.slice(0, 6)}...${walletAddress.slice(-4)}`
    : "";

  const shortHash = (hash?: string) =>
    hash ? `${hash.slice(0, 8)}...${hash.slice(-6)}` : null;

  // Stats
  const totalCost = trades.reduce((s, t) => s + t.size * t.price, 0);
  const totalFees = trades.reduce((s, t) => s + (t.fee || 0), 0);
  const buyCount = trades.filter(
    (t) => (t.side || "").toUpperCase() === "BUY",
  ).length;
  const sellCount = trades.filter(
    (t) => (t.side || "").toUpperCase() === "SELL",
  ).length;
  const uniqueMarkets = new Set(trades.map((t) => t.condition_id || t.market))
    .size;

  if (loading && trades.length === 0 && activeTab === "history") {
    return (
      <div className="space-y-6">
        <h1 className="text-3xl font-bold">Trades</h1>
        <p className="text-soft">Loading trades from Polymarket...</p>
        <div className="space-y-3">
          {[1, 2, 3, 4, 5].map((i) => (
            <div key={i} className="surface-panel p-4 animate-pulse h-20" />
          ))}
        </div>
      </div>
    );
  }

  return (
    <div className="space-y-6">
      {/* Header */}
      <div className="flex justify-between items-start">
        <div>
          <h1 className="text-3xl font-bold mb-2">Trades</h1>
          <p className="text-soft">
            Wallet <span className="mono">{shortAddr}</span>
          </p>
        </div>
        <button
          onClick={() =>
            activeTab === "history" ? fetchTrades(0) : fetchPositions()
          }
          disabled={loading || posLoading}
          className="btn-muted flex items-center gap-1.5"
        >
          <svg
            className={`w-4 h-4 ${loading || posLoading ? "animate-spin" : ""}`}
            fill="none"
            viewBox="0 0 24 24"
            stroke="currentColor"
          >
            <path
              strokeLinecap="round"
              strokeLinejoin="round"
              strokeWidth={2}
              d="M4 4v5h.582m15.356 2A8.001 8.001 0 004.582 9m0 0H9m11 11v-5h-.581m0 0a8.003 8.003 0 01-15.357-2m15.357 2H15"
            />
          </svg>
          Refresh
        </button>
      </div>

      {/* Tabs */}
      <div className="flex gap-1 border-b border-[var(--line)]">
        <button
          onClick={() => setActiveTab("history")}
          className={`px-4 py-2.5 text-sm font-semibold transition border-b-2 -mb-px ${
            activeTab === "history"
              ? "border-[var(--accent)] text-[var(--accent)]"
              : "border-transparent text-soft hover:text-white"
          }`}
        >
          Trade History
          {trades.length > 0 && (
            <span className="ml-1.5 text-xs opacity-60">({trades.length})</span>
          )}
        </button>
        <button
          onClick={() => setActiveTab("positions")}
          className={`px-4 py-2.5 text-sm font-semibold transition border-b-2 -mb-px ${
            activeTab === "positions"
              ? "border-[var(--accent)] text-[var(--accent)]"
              : "border-transparent text-soft hover:text-white"
          }`}
        >
          Your Positions
          {portfolio?.active_positions != null &&
            portfolio.active_positions > 0 && (
              <span className="ml-1.5 text-xs opacity-60">
                ({portfolio.active_positions})
              </span>
            )}
        </button>
      </div>

      {/* ========== POSITIONS TAB ========== */}
      {activeTab === "positions" && (
        <div className="space-y-6">
          {posError && (
            <div className="p-4 alert-error rounded text-sm">{posError}</div>
          )}

          {posLoading && !portfolio ? (
            <div className="space-y-3">
              {[1, 2, 3].map((i) => (
                <div key={i} className="surface-panel p-4 animate-pulse h-32" />
              ))}
            </div>
          ) : portfolio?.positions && portfolio.positions.length > 0 ? (
            <>
              {/* Position Stats */}
              <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
                <StatCard
                  label="Active Positions"
                  value={String(portfolio.active_positions ?? 0)}
                />
                <StatCard
                  label="Total Invested"
                  value={formatUSD(portfolio.total_invested ?? 0)}
                />
                <StatCard
                  label="Current Value"
                  value={formatUSD(portfolio.total_current_value ?? 0)}
                />
                <div className="surface-panel p-3">
                  <p className="text-muted text-[11px] uppercase tracking-wider mb-1">
                    Total P&L
                  </p>
                  <p
                    className={`text-lg font-bold ${(portfolio.total_pnl ?? 0) >= 0 ? "status-good" : "status-bad"}`}
                  >
                    {formatPnL(portfolio.total_pnl ?? 0)}
                    <span className="text-sm ml-1">
                      ({(portfolio.pnl_percentage ?? 0).toFixed(1)}%)
                    </span>
                  </p>
                </div>
              </div>

              {/* Position Cards */}
              <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
                {portfolio.positions.map((pos: any, idx: number) => {
                  const avgPrice = Number(pos.avgPrice || 0);
                  const curPrice = Number(pos.curPrice || avgPrice || 0);
                  const size = Number(pos.size || 0);
                  const pnl = Number(pos.pnl || 0);
                  const invested = avgPrice * size;
                  const pnlPercent = invested > 0 ? (pnl / invested) * 100 : 0;
                  const isFlashing = flashIds.has(pos.asset_id || "");

                  return (
                    <div
                      key={pos.asset_id || idx}
                      className="surface-panel p-5 hover:border-[var(--line-strong)] transition"
                    >
                      {/* Market Title */}
                      <div className="flex justify-between items-start mb-3">
                        <div className="flex-1 min-w-0">
                          <p className="text-sm font-semibold text-white truncate mb-1">
                            {pos.title ||
                              pos.market ||
                              pos.asset ||
                              `Position #${idx + 1}`}
                          </p>
                          {pos.outcome && (
                            <span className="chip chip-accent inline-block">
                              {pos.outcome}
                            </span>
                          )}
                        </div>
                      </div>

                      {/* P&L Display */}
                      <div className="flex items-center justify-between mb-3">
                        <div>
                          <p className="text-xs text-soft mb-0.5">
                            Profit/Loss
                          </p>
                          <div className="flex items-baseline gap-2">
                            <p
                              className={`text-lg font-bold ${pnl >= 0 ? "status-good" : "status-bad"} ${isFlashing ? "price-flash" : ""}`}
                            >
                              {formatPnL(pnl)}
                            </p>
                            <p
                              className={`text-sm font-medium ${pnl >= 0 ? "status-good" : "status-bad"}`}
                            >
                              ({pnlPercent >= 0 ? "+" : ""}
                              {pnlPercent.toFixed(1)}%)
                            </p>
                          </div>
                        </div>
                        <div className="text-right">
                          <p className="text-xs text-soft mb-0.5">Size</p>
                          <p className="text-sm font-medium text-white">
                            {size >= 1 ? size.toFixed(2) : size.toFixed(4)}{" "}
                            shares
                          </p>
                        </div>
                      </div>

                      {/* Price Info */}
                      <div className="flex justify-between text-xs mb-3">
                        <div>
                          <span className="text-muted">Entry: </span>
                          <span className="text-soft font-medium">
                            {(avgPrice * 100).toFixed(1)}¢
                          </span>
                        </div>
                        <div>
                          <span className="text-muted">Current: </span>
                          <span
                            className={`font-medium ${isFlashing ? "price-flash" : "text-soft"}`}
                          >
                            {(curPrice * 100).toFixed(1)}¢
                          </span>
                        </div>
                        <div>
                          <span
                            className={pnl >= 0 ? "status-good" : "status-bad"}
                          >
                            {pnl >= 0 ? "↑" : "↓"}{" "}
                            {(Math.abs(curPrice - avgPrice) * 100).toFixed(1)}¢
                          </span>
                        </div>
                      </div>

                      {/* Stop-loss indicator */}
                      {(() => {
                        const sl = getStopLossForPosition(pos);
                        return sl ? (
                          <div className="mb-3 px-2.5 py-1.5 rounded bg-red-500/10 border border-red-500/20 flex justify-between items-center">
                            <span className="text-xs text-red-400">
                              Stop Loss Active
                            </span>
                            <span className="text-xs mono text-red-300">
                              {(sl.stop_price * 100).toFixed(1)}¢
                            </span>
                          </div>
                        ) : null;
                      })()}

                      {/* Take-profit indicator */}
                      {(() => {
                        const tp = getTakeProfitForPosition(pos);
                        return tp ? (
                          <div className="mb-3 px-2.5 py-1.5 rounded bg-emerald-500/10 border border-emerald-500/20 flex justify-between items-center">
                            <span className="text-xs text-emerald-400">
                              Take Profit Active
                            </span>
                            <span className="text-xs mono text-emerald-300">
                              {(tp.take_profit_price * 100).toFixed(1)}¢
                            </span>
                          </div>
                        ) : null;
                      })()}

                      {(() => {
                        const inverse = getInverseBotForPosition(pos);
                        const tokenId = String(
                          pos.asset_id || pos.token_id || "",
                        );
                        const isBusy = inverseBusyToken === tokenId;
                        const enabled = !!inverse?.enabled;
                        const infoOpen =
                          inverseInfoPinnedToken === tokenId ||
                          inverseInfoHoverToken === tokenId;
                        return (
                          <div className="mb-3 p-3 rounded-lg border border-[var(--line)] bg-[var(--bg-soft)] space-y-2">
                            <div className="flex items-center justify-between">
                              <div className="flex items-center gap-2">
                                <p className="text-xs font-semibold text-soft">
                                  Inverse Position Bot
                                </p>
                                {enabled && (
                                  <div
                                    className="relative"
                                    onMouseEnter={() => {
                                      setInverseInfoHoverToken(tokenId);
                                      ensureInverseExplanation(
                                        tokenId,
                                        pos,
                                        inverse,
                                      );
                                    }}
                                    onMouseLeave={() =>
                                      setInverseInfoHoverToken(null)
                                    }
                                  >
                                    <button
                                      type="button"
                                      title="Explain current evaluation"
                                      onClick={() => {
                                        if (
                                          inverseInfoPinnedToken === tokenId
                                        ) {
                                          setInverseInfoPinnedToken(null);
                                        } else {
                                          setInverseInfoPinnedToken(tokenId);
                                          ensureInverseExplanation(
                                            tokenId,
                                            pos,
                                            inverse,
                                          );
                                        }
                                      }}
                                      className="w-5 h-5 rounded-full border border-[var(--line-strong)] text-[11px] text-soft hover:text-white hover:border-[var(--accent)] transition"
                                    >
                                      i
                                    </button>
                                    {infoOpen && (
                                      <div className="absolute z-20 mt-2 left-0 w-[34rem] max-w-[92vw] surface-panel p-0 shadow-xl overflow-hidden">
                                        <div className="px-3 py-2 border-b border-[var(--line)] bg-[var(--bg-soft)]">
                                          <p className="text-[11px] font-semibold text-soft">
                                            Inverse Bot Evaluation
                                          </p>
                                        </div>
                                        {inverseExplainLoadingToken ===
                                        tokenId ? (
                                          <p className="text-[11px] text-muted p-3">
                                            Generating explanation...
                                          </p>
                                        ) : inverseExplainErrorByToken[
                                            tokenId
                                          ] ? (
                                          <p className="text-[11px] text-red-300 p-3">
                                            {
                                              inverseExplainErrorByToken[
                                                tokenId
                                              ]
                                            }
                                          </p>
                                        ) : (
                                          <div className="overflow-x-auto">
                                            <table className="table-theme text-[11px] w-full">
                                              <tbody>
                                                {Object.entries(
                                                  inverseExplainByToken[
                                                    tokenId
                                                  ] || {
                                                    decision:
                                                      "No explanation available yet.",
                                                    why: "-",
                                                    confidence: "-",
                                                    market_signal: "-",
                                                    web_signal: "-",
                                                    x_signal: "-",
                                                    next_checks: "-",
                                                    updated_at: "-",
                                                  },
                                                ).map(([key, value]) => (
                                                  <tr key={key}>
                                                    <td className="p-2 text-muted capitalize">
                                                      {key.replace(/_/g, " ")}
                                                    </td>
                                                    <td className="p-2 text-soft">
                                                      {String(value)}
                                                    </td>
                                                  </tr>
                                                ))}
                                              </tbody>
                                            </table>
                                          </div>
                                        )}
                                      </div>
                                    )}
                                  </div>
                                )}
                              </div>
                              <label className="flex items-center gap-2 cursor-pointer">
                                <span className="text-xs text-muted">
                                  {enabled ? "On" : "Off"}
                                </span>
                                <input
                                  type="checkbox"
                                  checked={enabled}
                                  onChange={(e) =>
                                    toggleInverseBot(pos, e.target.checked)
                                  }
                                  disabled={isBusy}
                                  className="w-4 h-4 accent-[var(--accent)]"
                                />
                              </label>
                            </div>

                            <div className="flex items-center justify-between gap-2">
                              <span
                                className={`chip ${inverseStatusClass(inverse?.status)}`}
                              >
                                {inverse?.status || "inactive"}
                              </span>
                              <button
                                onClick={() => runInverseNow(pos)}
                                disabled={isBusy || !enabled}
                                className="btn-muted text-xs px-2.5 py-1"
                              >
                                {isBusy ? "Running..." : "Run Now"}
                              </button>
                            </div>

                            <div className="grid grid-cols-2 gap-2">
                              <select
                                value={inverse?.size_mode_override || "inherit"}
                                onChange={(e) =>
                                  upsertInverseBotForPosition(pos, {
                                    enabled: true,
                                    size_mode_override: e.target
                                      .value as InverseBotSizeOverride,
                                  })
                                }
                                disabled={!enabled || isBusy}
                                className="px-2 py-1.5 rounded bg-[var(--bg)] border border-[var(--line)] text-xs"
                              >
                                <option value="inherit">Inherit</option>
                                <option value="full_notional">
                                  Full Notional
                                </option>
                                <option value="fixed_amount">
                                  Fixed Amount
                                </option>
                              </select>
                              {(inverse?.size_mode_override || "inherit") ===
                                "fixed_amount" && (
                                <input
                                  type="number"
                                  min={1}
                                  step={1}
                                  placeholder="USDC"
                                  defaultValue={
                                    inverse?.fixed_amount_override ?? 50
                                  }
                                  onBlur={(e) => {
                                    const v = Number(e.target.value);
                                    if (v >= 1) {
                                      upsertInverseBotForPosition(pos, {
                                        enabled: true,
                                        size_mode_override: "fixed_amount",
                                        fixed_amount_override: v,
                                      });
                                    }
                                  }}
                                  disabled={!enabled || isBusy}
                                  className="px-2 py-1.5 rounded bg-[var(--bg)] border border-[var(--line)] text-xs"
                                />
                              )}
                            </div>

                            <div className="text-[11px] text-muted">
                              Confidence:{" "}
                              {inverse?.last_confidence != null
                                ? `${inverse.last_confidence.toFixed(0)}%`
                                : "—"}
                              {" · "}
                              Last eval:{" "}
                              {inverse?.last_evaluated_at
                                ? new Date(
                                    inverse.last_evaluated_at,
                                  ).toLocaleTimeString()
                                : "—"}
                            </div>
                            {inverse?.last_error && (
                              <p className="text-[11px] text-red-300 truncate">
                                {inverse.last_error}
                              </p>
                            )}
                          </div>
                        );
                      })()}

                      {/* Action Buttons */}
                      <div className="flex gap-2">
                        <button
                          onClick={() => setCashOutPosition(pos)}
                          className="flex-1 btn-success flex items-center justify-center gap-1.5"
                        >
                          Cash Out
                        </button>
                        <button
                          onClick={() => setStopLossPosition(pos)}
                          className="flex-1 btn-danger flex items-center justify-center gap-1.5"
                        >
                          {getStopLossForPosition(pos)
                            ? "Edit Stop Loss"
                            : "Stop Loss"}
                        </button>
                        <button
                          onClick={() => setTakeProfitPosition(pos)}
                          className="flex-1 btn-accent flex items-center justify-center gap-1.5"
                        >
                          {getTakeProfitForPosition(pos)
                            ? "Edit Take Profit"
                            : "Take Profit"}
                        </button>
                      </div>
                    </div>
                  );
                })}
              </div>
            </>
          ) : (
            <div className="text-center py-12 surface-panel">
              <p className="text-soft text-lg mb-2">No positions found</p>
              <p className="text-muted text-sm">
                Start trading on Polymarket to see your positions here.
              </p>
            </div>
          )}
        </div>
      )}

      {/* ========== TRADE HISTORY TAB ========== */}
      {activeTab === "history" && (
        <div className="space-y-6">
          {error && (
            <div className="p-4 alert-error rounded text-sm">{error}</div>
          )}

          {/* Summary Stats */}
          {trades.length > 0 && (
            <div className="grid grid-cols-2 md:grid-cols-5 gap-3">
              <StatCard label="Total Trades" value={String(trades.length)} />
              <StatCard label="Total Cost" value={formatUSD(totalCost)} />
              <StatCard label="Total Fees" value={formatUSD(totalFees)} />
              <StatCard
                label="Buys / Sells"
                value={`${buyCount} / ${sellCount}`}
              />
              <StatCard label="Markets" value={String(uniqueMarkets)} />
            </div>
          )}

          {/* Filters */}
          <div className="flex gap-2 flex-wrap">
            {(
              [
                ["all", "All"],
                ["buy", "Buys"],
                ["sell", "Sells"],
                ["confirmed", "Confirmed"],
                ["matched", "Matched"],
              ] as [FilterType, string][]
            ).map(([key, label]) => (
              <button
                key={key}
                onClick={() => setFilter(key)}
                className={`px-3 py-1.5 rounded text-sm font-semibold transition ${
                  filter === key
                    ? "bg-[var(--accent-soft)] text-[var(--accent)] border border-[#f0b74155]"
                    : "bg-[var(--bg-soft)] text-soft hover:border hover:border-[var(--line-strong)]"
                }`}
              >
                {label}
                {key !== "all" && (
                  <span className="ml-1 opacity-60">
                    (
                    {key === "buy" || key === "sell"
                      ? trades.filter(
                          (t) =>
                            (t.side || "").toUpperCase() === key.toUpperCase(),
                        ).length
                      : trades.filter(
                          (t) =>
                            (t.status || "").toUpperCase() ===
                            key.toUpperCase(),
                        ).length}
                    )
                  </span>
                )}
              </button>
            ))}
          </div>

          {/* Trades Table */}
          {filteredTrades.length > 0 ? (
            <div className="surface-panel overflow-hidden">
              <div className="overflow-x-auto">
                <table className="table-theme text-sm w-full">
                  <thead>
                    <tr>
                      <th className="text-left p-3">Market</th>
                      <th className="text-center p-3">Side</th>
                      <th className="text-right p-3">
                        <span title="Number of outcome shares traded (not dollars)">
                          Shares
                        </span>
                      </th>
                      <th className="text-right p-3">
                        <span title="Price per share">Price</span>
                      </th>
                      <th className="text-right p-3">
                        <span title="Shares × Price = actual dollar cost">
                          Cost
                        </span>
                      </th>
                      <th className="text-right p-3">Fee</th>
                      <th className="text-center p-3">Status</th>
                      <th className="text-right p-3">Time</th>
                    </tr>
                  </thead>
                  <tbody>
                    {filteredTrades.map((trade, idx) => {
                      const total = trade.size * trade.price;
                      const isExpanded =
                        expandedId === (trade.id || String(idx));
                      return (
                        <>
                          <tr
                            key={trade.id || idx}
                            className="transition cursor-pointer hover:bg-[var(--bg-soft)]"
                            onClick={() =>
                              setExpandedId(
                                isExpanded ? null : trade.id || String(idx),
                              )
                            }
                          >
                            <td className="p-3 max-w-[280px]">
                              <p className="text-white truncate text-sm font-medium">
                                {trade.market || "Unknown Market"}
                              </p>
                              <div className="flex items-center gap-2 mt-0.5">
                                {trade.outcome && (
                                  <span className="text-muted text-xs">
                                    {trade.outcome}
                                  </span>
                                )}
                                {trade.trader_side && (
                                  <span className="text-muted text-[10px] uppercase opacity-50">
                                    {trade.trader_side}
                                  </span>
                                )}
                              </div>
                            </td>
                            <td className="p-3 text-center">
                              <span
                                className={`inline-block px-2.5 py-0.5 rounded text-xs font-bold ${
                                  (trade.side || "").toUpperCase() === "BUY"
                                    ? "bg-emerald-500/15 text-emerald-400 border border-emerald-500/25"
                                    : (trade.side || "").toUpperCase() ===
                                        "SELL"
                                      ? "bg-red-500/15 text-red-400 border border-red-500/25"
                                      : "bg-[var(--bg-soft)] text-soft border border-[var(--line)]"
                                }`}
                              >
                                {(trade.side || "—").toUpperCase()}
                              </span>
                            </td>
                            <td
                              className="p-3 text-right mono text-muted text-xs"
                              title="Outcome shares (not dollars)"
                            >
                              {trade.size >= 10
                                ? trade.size.toFixed(0)
                                : trade.size.toFixed(2)}
                            </td>
                            <td className="p-3 text-right mono text-soft">
                              {trade.price > 0
                                ? trade.price >= 0.01
                                  ? `${(trade.price * 100).toFixed(1)}¢`
                                  : `${(trade.price * 100).toFixed(2)}¢`
                                : "—"}
                            </td>
                            <td className="p-3 text-right mono text-white font-semibold">
                              {formatUSD(total)}
                            </td>
                            <td className="p-3 text-right mono text-muted text-xs">
                              {trade.fee > 0 ? formatUSD(trade.fee) : "—"}
                            </td>
                            <td className="p-3 text-center">
                              <span
                                className={`inline-block px-2 py-0.5 rounded text-[10px] uppercase font-bold ${
                                  (trade.status || "").toUpperCase() ===
                                  "CONFIRMED"
                                    ? "bg-emerald-500/15 text-emerald-400"
                                    : (trade.status || "").toUpperCase() ===
                                        "MATCHED"
                                      ? "bg-blue-500/15 text-blue-400"
                                      : (trade.status || "").toUpperCase() ===
                                          "OPEN"
                                        ? "bg-amber-500/15 text-amber-400"
                                        : "bg-[var(--bg-soft)] text-soft"
                                }`}
                              >
                                {trade.status || "—"}
                              </span>
                            </td>
                            <td className="p-3 text-right text-soft text-xs whitespace-nowrap">
                              {formatDate(trade.timestamp)}
                            </td>
                          </tr>

                          {/* Expanded details row */}
                          {isExpanded && (
                            <tr key={`${trade.id || idx}-detail`}>
                              <td
                                colSpan={8}
                                className="px-4 py-3 bg-[var(--bg-soft)] border-t border-b border-[var(--line)]"
                              >
                                <div className="grid grid-cols-2 md:grid-cols-4 gap-3 text-xs">
                                  <div>
                                    <span className="text-muted block mb-0.5">
                                      Breakdown
                                    </span>
                                    <span className="mono text-soft">
                                      {trade.size >= 10
                                        ? trade.size.toFixed(0)
                                        : trade.size.toFixed(4)}{" "}
                                      shares ×{" "}
                                      {trade.price > 0
                                        ? `${(trade.price * 100).toFixed(2)}¢`
                                        : "—"}{" "}
                                      = {formatUSD(trade.size * trade.price)}
                                    </span>
                                  </div>
                                  {trade.transaction_hash && (
                                    <div>
                                      <span className="text-muted block mb-0.5">
                                        Tx Hash
                                      </span>
                                      {buildPolygonscanTxUrl(
                                        trade.transaction_hash,
                                      ) ? (
                                        <a
                                          href={
                                            buildPolygonscanTxUrl(
                                              trade.transaction_hash,
                                            ) || "#"
                                          }
                                          target="_blank"
                                          rel="noopener noreferrer"
                                          className="mono text-[var(--accent)] hover:underline"
                                        >
                                          {shortHash(trade.transaction_hash)}
                                        </a>
                                      ) : (
                                        <span className="mono text-soft">
                                          {shortHash(trade.transaction_hash)}
                                        </span>
                                      )}
                                    </div>
                                  )}
                                  {trade.maker_address && (
                                    <div>
                                      <span className="text-muted block mb-0.5">
                                        Counterparty
                                      </span>
                                      {buildPolygonscanAddressUrl(
                                        trade.maker_address,
                                      ) ? (
                                        <a
                                          href={
                                            buildPolygonscanAddressUrl(
                                              trade.maker_address,
                                            ) || "#"
                                          }
                                          target="_blank"
                                          rel="noopener noreferrer"
                                          className="mono text-soft hover:text-white"
                                        >
                                          {shortHash(trade.maker_address)}
                                        </a>
                                      ) : (
                                        <span className="mono text-soft">
                                          {shortHash(trade.maker_address)}
                                        </span>
                                      )}
                                    </div>
                                  )}
                                  {trade.fee_rate_bps != null && (
                                    <div>
                                      <span className="text-muted block mb-0.5">
                                        Fee Rate
                                      </span>
                                      <span className="mono text-soft">
                                        {(trade.fee_rate_bps / 100).toFixed(1)}%
                                      </span>
                                    </div>
                                  )}
                                  <div>
                                    <span className="text-muted block mb-0.5">
                                      Trade ID
                                    </span>
                                    <span className="mono text-soft text-[11px] break-all">
                                      {trade.id || "—"}
                                    </span>
                                  </div>
                                  {trade.market_slug && (
                                    <div>
                                      <span className="text-muted block mb-0.5">
                                        Polymarket
                                      </span>
                                      {buildPolymarketEventUrl(
                                        trade.market_slug,
                                        trade.market_slug,
                                      ) ? (
                                        <a
                                          href={
                                            buildPolymarketEventUrl(
                                              trade.market_slug,
                                              trade.market_slug,
                                            ) || "#"
                                          }
                                          target="_blank"
                                          rel="noopener noreferrer"
                                          className="text-[var(--accent)] hover:underline"
                                        >
                                          View Market
                                        </a>
                                      ) : (
                                        <span className="text-soft">
                                          Unavailable
                                        </span>
                                      )}
                                    </div>
                                  )}
                                </div>
                              </td>
                            </tr>
                          )}
                        </>
                      );
                    })}
                  </tbody>
                </table>
              </div>

              {/* Load More */}
              {hasMore && (
                <div className="p-3 text-center border-t border-[var(--line)]">
                  <button
                    onClick={loadMore}
                    disabled={loading}
                    className="btn-muted"
                  >
                    {loading ? "Loading..." : "Load More"}
                  </button>
                </div>
              )}
            </div>
          ) : (
            <div className="text-center py-12 surface-panel">
              <p className="text-soft text-lg mb-2">No trades found</p>
              <p className="text-muted text-sm">
                {filter !== "all"
                  ? `No ${filter} trades. Try a different filter.`
                  : "Start trading on Polymarket to see your history here."}
              </p>
            </div>
          )}
        </div>
      )}

      {/* ── Modals ── */}
      {cashOutPosition && (
        <CashOutModal
          position={cashOutPosition}
          onClose={() => setCashOutPosition(null)}
          onSuccess={() => {
            setCashOutPosition(null);
            fetchPositions();
            fetchStopLosses();
          }}
        />
      )}

      {stopLossPosition && (
        <StopLossModal
          position={stopLossPosition}
          existingStopLoss={getStopLossForPosition(stopLossPosition)}
          onClose={() => setStopLossPosition(null)}
          onSuccess={() => {
            setStopLossPosition(null);
            fetchStopLosses();
          }}
        />
      )}

      {takeProfitPosition && (
        <TakeProfitModal
          position={takeProfitPosition}
          existingTakeProfit={getTakeProfitForPosition(takeProfitPosition)}
          onClose={() => setTakeProfitPosition(null)}
          onSuccess={() => {
            setTakeProfitPosition(null);
            fetchTakeProfits();
          }}
        />
      )}
    </div>
  );
}

function StatCard({ label, value }: { label: string; value: string }) {
  return (
    <div className="surface-panel p-3">
      <p className="text-muted text-[11px] uppercase tracking-wider mb-1">
        {label}
      </p>
      <p className="text-lg font-bold text-white">{value}</p>
    </div>
  );
}
