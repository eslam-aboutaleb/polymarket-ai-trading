import { useEffect, useState, useRef, useCallback, useMemo } from "react";
import {
  portfolioService,
  PortfolioSummary,
  PolymarketMarket,
} from "../services/portfolioService";
import { stopLossService, StopLossOrder } from "../services/stopLossService";
import {
  inverseBotService,
  InverseBotPosition,
  InverseBotSizeOverride,
} from "../services/inverseBotService";
import { emergencyService, ArbitrageOpportunity } from "../services/emergencyService";
import { FollowingFeedEvent, tradesService } from "../services/tradesService";
import { analysisService } from "../services/analysisService";
import { useAuthStore } from "../store/authStore";
import { getApiErrorMessage } from "../utils/apiError";
import CashOutModal from "./CashOutModal";
import StopLossModal from "./StopLossModal";
import PositionAnalysisPopup, {
  PositionForAnalysis,
} from "./PositionAnalysisPopup";

const PRICE_POLL_INTERVAL = 15_000; // 15 seconds
const STOP_LOSS_POLL_INTERVAL = 10_000; // 10 seconds – match backend check
const FOLLOWING_FEED_POLL_INTERVAL = 15_000;

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

export default function Dashboard() {
  const { walletAddress, isAuthenticated } = useAuthStore();
  const [portfolio, setPortfolio] = useState<PortfolioSummary | null>(null);
  const [markets, setMarkets] = useState<PolymarketMarket[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [followingFeed, setFollowingFeed] = useState<FollowingFeedEvent[]>([]);
  const [followingFeedLoading, setFollowingFeedLoading] = useState(false);
  const [followingFeedError, setFollowingFeedError] = useState<string | null>(
    null,
  );
  const [riskOpsBusy, setRiskOpsBusy] = useState<string | null>(null);
  const [riskOpsMessage, setRiskOpsMessage] = useState<string | null>(null);
  const [arbitrageRows, setArbitrageRows] = useState<ArbitrageOpportunity[]>([]);
  const [feedEventTypeFilter, setFeedEventTypeFilter] = useState<
    "all" | "opened" | "closed"
  >("all");
  const [feedWalletFilter, setFeedWalletFilter] = useState("");
  // Track which asset_ids just got a price change for flash animation
  const [flashIds, setFlashIds] = useState<Set<string>>(new Set());
  const prevPricesRef = useRef<Record<string, number>>({});
  const flashResetTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const pricesInFlightRef = useRef(false);
  const followingFeedInFlightRef = useRef(false);
  const stopLossInFlightRef = useRef(false);

  useEffect(
    () => () => {
      if (flashResetTimerRef.current) {
        clearTimeout(flashResetTimerRef.current);
      }
    },
    [],
  );

  useEffect(() => {
    if (isAuthenticated && walletAddress) {
      fetchData();
    }
  }, [walletAddress, isAuthenticated]);

  const refreshPositionPrices = useCallback(async () => {
    if (
      !isAuthenticated ||
      !portfolio?.positions?.length ||
      pricesInFlightRef.current
    ) {
      return;
    }
    pricesInFlightRef.current = true;
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

        // Recalculate summary totals
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
        const pnlPct = totalInvested > 0 ? (totalPnl / totalInvested) * 100 : 0;

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
    } finally {
      pricesInFlightRef.current = false;
    }
  }, [isAuthenticated, portfolio?.positions?.length]);

  const fetchData = async () => {
    setLoading(true);
    setError(null);

    try {
      const [summaryResult, marketsResult] = await Promise.allSettled([
        portfolioService.getSummary(),
        portfolioService.getActiveMarkets(6),
      ]);

      if (summaryResult.status === "fulfilled") {
        setPortfolio(summaryResult.value);
      } else {
        console.error("Failed to load portfolio:", summaryResult.reason);
      }

      if (marketsResult.status === "fulfilled") {
        setMarkets(marketsResult.value.markets || []);
      }
    } catch (err: unknown) {
      setError(getApiErrorMessage(err, "Failed to load dashboard data"));
    } finally {
      setLoading(false);
    }
  };

  const fetchFollowingFeed = useCallback(
    async ({ silent = false }: { silent?: boolean } = {}) => {
      if (!isAuthenticated || followingFeedInFlightRef.current) return;
      followingFeedInFlightRef.current = true;
      if (!silent) {
        setFollowingFeedLoading(true);
      }
      try {
        const rows = await tradesService.getFollowingFeed(
          100,
          feedWalletFilter || undefined,
          feedEventTypeFilter === "all" ? undefined : feedEventTypeFilter,
        );
        setFollowingFeed(rows);
        setFollowingFeedError(null);
      } catch (err: unknown) {
        setFollowingFeedError(
          getApiErrorMessage(err, "Failed to load following feed"),
        );
      } finally {
        followingFeedInFlightRef.current = false;
        if (!silent) {
          setFollowingFeedLoading(false);
        }
      }
    },
    [feedEventTypeFilter, feedWalletFilter, isAuthenticated],
  );

  const refreshArbitrageFeed = useCallback(async () => {
    try {
      const rows = await emergencyService.getArbitrageOpportunities();
      setArbitrageRows(rows || []);
    } catch {
      // ignore
    }
  }, []);

  useEffect(() => {
    if (!isAuthenticated) return;
    void refreshArbitrageFeed();
  }, [isAuthenticated, refreshArbitrageFeed]);

  const handleEmergencyStop = async () => {
    try {
      setRiskOpsBusy("emergency");
      const res = await emergencyService.emergencyStop(true);
      setRiskOpsMessage(res.message || "Emergency stop completed.");
      await fetchData();
      await fetchStopLosses();
    } catch (err: unknown) {
      setRiskOpsMessage(getApiErrorMessage(err, "Emergency stop failed"));
    } finally {
      setRiskOpsBusy(null);
    }
  };

  const handleResumeTrading = async () => {
    try {
      setRiskOpsBusy("resume");
      const res = await emergencyService.resumeTrading();
      setRiskOpsMessage(res.message || "Trading resumed.");
      await fetchData();
    } catch (err: unknown) {
      setRiskOpsMessage(getApiErrorMessage(err, "Resume trading failed"));
    } finally {
      setRiskOpsBusy(null);
    }
  };

  const handleScanArbitrage = async () => {
    try {
      setRiskOpsBusy("scan_arbitrage");
      const res = await emergencyService.scanArbitrage();
      setArbitrageRows(res.opportunities || []);
      setRiskOpsMessage(`Arbitrage scan complete. Found ${res.found} opportunities.`);
    } catch (err: unknown) {
      setRiskOpsMessage(getApiErrorMessage(err, "Arbitrage scan failed"));
    } finally {
      setRiskOpsBusy(null);
    }
  };

  const handleRescoreTraders = async () => {
    try {
      setRiskOpsBusy("rescore");
      const res = await emergencyService.rescoreAllTraders();
      setRiskOpsMessage(res.message || `Rescored ${res.scored} traders.`);
    } catch (err: unknown) {
      setRiskOpsMessage(getApiErrorMessage(err, "Trader rescore failed"));
    } finally {
      setRiskOpsBusy(null);
    }
  };

  const formatUSD = (value: number) =>
    `$${value.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;

  const formatPnL = (value: number) => {
    const prefix = value >= 0 ? "+" : "";
    return `${prefix}${formatUSD(value)}`;
  };

  const shortAddressValue = (value: string) =>
    value.length >= 10 ? `${value.slice(0, 6)}...${value.slice(-4)}` : value;

  // ── Modal state ──
  const [cashOutPosition, setCashOutPosition] = useState<any | null>(null);
  const [stopLossPosition, setStopLossPosition] = useState<any | null>(null);

  // ── AI Analysis popup state ──
  const [analysisPosition, setAnalysisPosition] =
    useState<PositionForAnalysis | null>(null);
  const [analysisClickCoords, setAnalysisClickCoords] = useState<{
    x: number;
    y: number;
  }>({ x: 0, y: 0 });

  // ── Active stop-losses ──
  const [activeStopLosses, setActiveStopLosses] = useState<StopLossOrder[]>([]);
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

  const fetchStopLosses = useCallback(async () => {
    if (stopLossInFlightRef.current) return;
    stopLossInFlightRef.current = true;
    try {
      const orders = await stopLossService.getStopLosses("active");
      setActiveStopLosses(orders);
    } catch {
      // ignore
    } finally {
      stopLossInFlightRef.current = false;
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

  useEffect(() => {
    if (!isAuthenticated) return;
    fetchInverseBotPositions();
  }, [isAuthenticated, fetchInverseBotPositions]);

  useEffect(() => {
    if (!isAuthenticated) return;

    const lastRunAt = {
      prices: 0,
      followingFeed: 0,
      stopLosses: 0,
    };

    const tick = () => {
      if (document.visibilityState !== "visible") return;
      const now = Date.now();

      if (now - lastRunAt.stopLosses >= STOP_LOSS_POLL_INTERVAL) {
        lastRunAt.stopLosses = now;
        void fetchStopLosses();
      }

      if (now - lastRunAt.followingFeed >= FOLLOWING_FEED_POLL_INTERVAL) {
        lastRunAt.followingFeed = now;
        void fetchFollowingFeed({ silent: true });
      }

      if (
        portfolio?.positions?.length &&
        now - lastRunAt.prices >= PRICE_POLL_INTERVAL
      ) {
        lastRunAt.prices = now;
        void refreshPositionPrices();
      }
    };

    const forceRefreshNow = () => {
      if (document.visibilityState !== "visible") return;
      lastRunAt.prices = 0;
      lastRunAt.followingFeed = 0;
      lastRunAt.stopLosses = 0;
      tick();
    };

    void fetchStopLosses();
    void fetchFollowingFeed();
    if (portfolio?.positions?.length) {
      void refreshPositionPrices();
    }

    const id = window.setInterval(tick, 1000);
    document.addEventListener("visibilitychange", forceRefreshNow);
    return () => {
      window.clearInterval(id);
      document.removeEventListener("visibilitychange", forceRefreshNow);
    };
  }, [
    fetchFollowingFeed,
    fetchStopLosses,
    isAuthenticated,
    portfolio?.positions?.length,
    refreshPositionPrices,
  ]);

  /** Find active stop-loss for a position by token_id */
  const getStopLossForPosition = (pos: any): StopLossOrder | undefined => {
    const tokenId = pos.asset_id || pos.token_id || pos.condition_id || "";
    return activeStopLosses.find((sl) => sl.token_id === tokenId);
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
      setError("Missing token or condition id for inverse bot.");
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
      setError(
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
        setError(getApiErrorMessage(err, "Failed to disable inverse bot"));
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
      setError(getApiErrorMessage(err, "Failed to run inverse evaluation"));
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

  const shortAddress = walletAddress
    ? `${walletAddress.slice(0, 6)}...${walletAddress.slice(-4)}`
    : "Not connected";
  const followingFeedWalletOptions = useMemo(
    () =>
      Array.from(
        new Set(followingFeed.map((row) => row.trader_wallet.toLowerCase())),
      ).sort(),
    [followingFeed],
  );

  if (loading) {
    return (
      <div className="space-y-8">
        <div>
          <h1 className="text-4xl font-bold mb-2">Dashboard</h1>
          <p className="text-soft">Loading portfolio data...</p>
        </div>
        <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-4 gap-6">
          {[1, 2, 3, 4].map((i) => (
            <div key={i} className="surface-panel p-6 animate-pulse">
              <div className="h-4 bg-[var(--bg-soft)] rounded w-24 mb-3"></div>
              <div className="h-8 bg-[var(--bg-soft)] rounded w-32"></div>
            </div>
          ))}
        </div>
      </div>
    );
  }

  return (
    <div className="space-y-8">
      {/* Header */}
      <div className="flex justify-between items-start">
        <div>
          <h1 className="text-4xl font-bold mb-2">Dashboard</h1>
          <p className="text-soft">
            Wallet: <span className="mono">{shortAddress}</span>
          </p>
        </div>
        <button onClick={fetchData} className="btn-muted">
          Refresh
        </button>
      </div>

      {error && <div className="p-4 alert-error rounded text-sm">{error}</div>}

      {/* Stats Grid */}
      <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-4 gap-6">
        {/* Portfolio Balance */}
        <div className="surface-panel p-6">
          <h3 className="text-soft text-sm font-medium mb-2">USDC Balance</h3>
          <p className="text-3xl font-bold">
            {formatUSD(portfolio?.usdc_balance ?? 0)}
          </p>
          <p className="text-muted text-sm mt-2">
            {portfolio?.matic_balance?.toFixed(4) ?? "0"} MATIC
          </p>
        </div>

        {/* Positions */}
        <div className="surface-panel p-6">
          <h3 className="text-soft text-sm font-medium mb-2">
            Active Positions
          </h3>
          <p className="text-3xl font-bold">
            {portfolio?.active_positions ?? 0}
          </p>
          <p className="text-muted text-sm mt-2">
            {portfolio?.total_positions ?? 0} total positions
          </p>
        </div>

        {/* Total P&L */}
        <div className="surface-panel p-6">
          <h3 className="text-soft text-sm font-medium mb-2">Total P&L</h3>
          <p
            className={`text-3xl font-bold ${
              (portfolio?.total_pnl ?? 0) >= 0 ? "status-good" : "status-bad"
            }`}
          >
            {formatPnL(portfolio?.total_pnl ?? 0)}
          </p>
          <p className="text-muted text-sm mt-2">
            {(portfolio?.pnl_percentage ?? 0).toFixed(1)}%
          </p>
        </div>

        {/* Win Rate */}
        <div className="surface-panel p-6">
          <h3 className="text-soft text-sm font-medium mb-2">Win Rate</h3>
          <p className="text-3xl font-bold">
            {`${(portfolio?.win_rate ?? 0).toFixed(0)}%`}
          </p>
          <p className="text-muted text-sm mt-2">
            {portfolio?.wins_positions_history ??
              portfolio?.wins_positions ??
              0}
            /
            {portfolio?.total_positions_history ??
              portfolio?.resolved_trades ??
              0}{" "}
            winning closed / total history
          </p>
        </div>
      </div>

      <div className="surface-panel p-6 space-y-3">
        <div className="flex items-center justify-between gap-3 flex-wrap">
          <h2 className="text-xl font-bold">Risk Ops</h2>
          <a href="/ops" className="btn-muted text-xs px-3 py-1.5">
            Open Full Ops Center
          </a>
        </div>

        <div className="flex flex-wrap gap-2">
          <button
            onClick={handleEmergencyStop}
            disabled={riskOpsBusy !== null}
            className="btn-danger text-xs"
          >
            {riskOpsBusy === "emergency"
              ? "Running..."
              : "Emergency Stop + Close Positions"}
          </button>
          <button
            onClick={handleResumeTrading}
            disabled={riskOpsBusy !== null}
            className="btn-success text-xs"
          >
            {riskOpsBusy === "resume" ? "Running..." : "Resume Trading"}
          </button>
          <button
            onClick={handleRescoreTraders}
            disabled={riskOpsBusy !== null}
            className="btn-muted text-xs"
          >
            {riskOpsBusy === "rescore" ? "Running..." : "Rescore Traders"}
          </button>
          <button
            onClick={handleScanArbitrage}
            disabled={riskOpsBusy !== null}
            className="btn-muted text-xs"
          >
            {riskOpsBusy === "scan_arbitrage"
              ? "Scanning..."
              : "Scan Arbitrage Now"}
          </button>
          <button
            onClick={refreshArbitrageFeed}
            disabled={riskOpsBusy !== null}
            className="btn-muted text-xs"
          >
            Refresh Arbitrage Feed
          </button>
        </div>

        {riskOpsMessage && (
          <p className="text-xs text-soft rounded border border-[var(--line)] bg-[var(--bg-soft)] px-3 py-2">
            {riskOpsMessage}
          </p>
        )}

        <p className="text-xs text-soft">
          Arbitrage opportunities tracked: {arbitrageRows.length}
        </p>
      </div>

      <div className="surface-panel p-6">
        <div className="flex items-center justify-between gap-3 flex-wrap mb-4">
          <h2 className="text-xl font-bold">Following Feed</h2>
          <div className="flex items-center gap-2 flex-wrap">
            <select
              value={feedEventTypeFilter}
              onChange={(e) =>
                setFeedEventTypeFilter(
                  e.target.value as "all" | "opened" | "closed",
                )
              }
              className="bg-[var(--bg-soft)] border border-[var(--line)] rounded px-2 py-1.5 text-xs"
            >
              <option value="all">All Events</option>
              <option value="opened">Opened</option>
              <option value="closed">Closed</option>
            </select>
            <select
              value={feedWalletFilter}
              onChange={(e) => setFeedWalletFilter(e.target.value)}
              className="bg-[var(--bg-soft)] border border-[var(--line)] rounded px-2 py-1.5 text-xs mono"
            >
              <option value="">All Traders</option>
              {followingFeedWalletOptions.map((wallet) => (
                <option key={wallet} value={wallet}>
                  {shortAddressValue(wallet)}
                </option>
              ))}
            </select>
            <button
              onClick={() => fetchFollowingFeed()}
              className="btn-muted text-xs px-3 py-1.5"
              disabled={followingFeedLoading}
            >
              {followingFeedLoading ? "Refreshing..." : "Refresh"}
            </button>
          </div>
        </div>

        {followingFeedError && (
          <div className="p-3 alert-error rounded text-xs mb-3">
            {followingFeedError}
          </div>
        )}

        {followingFeed.length === 0 ? (
          <div className="text-soft text-sm py-4">
            No followed-trader events yet.
          </div>
        ) : (
          <div className="space-y-2 max-h-80 overflow-y-auto scroll-soft">
            {followingFeed.map((row) => (
              <div
                key={row.id}
                className="surface-soft p-3 rounded border border-[var(--line)]"
              >
                <div className="flex items-start justify-between gap-3">
                  <div>
                    <p className="text-xs text-white">
                      <span className="mono">
                        {shortAddressValue(row.trader_wallet)}
                      </span>{" "}
                      <span
                        className={`chip ml-2 text-[10px] ${
                          row.event_type === "opened"
                            ? "chip-success"
                            : "chip-danger"
                        }`}
                      >
                        {row.event_type}
                      </span>
                    </p>
                    <p className="text-[11px] text-muted mt-1">
                      Market:{" "}
                      {row.market_id ? shortAddressValue(row.market_id) : "—"} ·
                      Side: {row.side || "—"} · Size: {row.size.toFixed(4)} ·
                      Price: {row.price.toFixed(4)}
                    </p>
                  </div>
                  <p className="text-[11px] text-muted">
                    {new Date(row.created_at).toLocaleTimeString()}
                  </p>
                </div>
              </div>
            ))}
          </div>
        )}
      </div>

      {/* Content Sections */}
      <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
        {/* Positions */}
        <div className="surface-panel p-6">
          <h2 className="text-xl font-bold mb-4">Your Positions</h2>
          {portfolio?.positions && portfolio.positions.length > 0 ? (
            <div className="space-y-3 max-h-80 overflow-y-auto scroll-soft">
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
                    className="p-4 surface-soft hover:border-[var(--line-strong)] transition"
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

                    {/* P&L Display - Polymarket Style */}
                    <div className="flex items-center justify-between mb-3">
                      <div>
                        <p className="text-xs text-soft mb-0.5">Profit/Loss</p>
                        <div className="flex items-baseline gap-2">
                          <p
                            className={`text-lg font-bold ${
                              pnl >= 0 ? "status-good" : "status-bad"
                            } ${isFlashing ? "price-flash" : ""}`}
                          >
                            {formatPnL(pnl)}
                          </p>
                          <p
                            className={`text-sm font-medium ${
                              pnl >= 0 ? "status-good" : "status-bad"
                            }`}
                          >
                            ({pnlPercent >= 0 ? "+" : ""}
                            {pnlPercent.toFixed(1)}%)
                          </p>
                        </div>
                      </div>
                      <div className="text-right">
                        <p className="text-xs text-soft mb-0.5">Size</p>
                        <p className="text-sm font-medium text-white">
                          {size >= 1 ? size.toFixed(2) : size.toFixed(4)} shares
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

                    {/* Active stop-loss badge */}
                    {(() => {
                      const sl = getStopLossForPosition(pos);
                      return sl ? (
                        <div className="mb-3 flex items-center gap-2 px-3 py-1.5 rounded-lg bg-red-500/10 border border-red-500/25 text-xs">
                          <span className="relative flex h-2 w-2">
                            <span className="animate-ping absolute inline-flex h-full w-full rounded-full bg-red-400 opacity-75" />
                            <span className="relative inline-flex rounded-full h-2 w-2 bg-red-500" />
                          </span>
                          <span className="text-red-300 font-medium">
                            Stop loss active at{" "}
                            {(sl.stop_price * 100).toFixed(0)}¢
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
                                      if (inverseInfoPinnedToken === tokenId) {
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
                                          {inverseExplainErrorByToken[tokenId]}
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
                              <option value="fixed_amount">Fixed Amount</option>
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
                        onClick={(e) => {
                          setAnalysisPosition(pos);
                          setAnalysisClickCoords({
                            x: e.clientX,
                            y: e.clientY,
                          });
                        }}
                        className="flex-1 btn-accent flex items-center justify-center gap-1.5 text-xs"
                      >
                        <span>🤖</span> AI Analyze
                      </button>
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
                    </div>
                  </div>
                );
              })}
            </div>
          ) : (
            <div className="text-soft text-center py-8">
              No positions found. Start trading to see your activity here.
            </div>
          )}
        </div>

        {/* Trending Markets */}
        <div className="surface-panel p-6">
          <h2 className="text-xl font-bold mb-4">Trending Markets</h2>
          {markets.length > 0 ? (
            <div className="space-y-3 max-h-80 overflow-y-auto scroll-soft">
              {markets.map((market: any, idx: number) => (
                <div
                  key={idx}
                  className="p-3 surface-soft hover:border-[var(--line-strong)] transition cursor-pointer"
                >
                  <p className="text-sm font-medium text-white mb-1">
                    {market.question || market.title || `Market #${idx + 1}`}
                  </p>
                  <div className="flex justify-between text-xs text-soft">
                    <span>
                      Vol: $
                      {Number(
                        market.volume24hr || market.volume_24hr || 0,
                      ).toLocaleString()}
                    </span>
                    <span>
                      Liq: ${Number(market.liquidity || 0).toLocaleString()}
                    </span>
                    {market.outcomePrices &&
                      (() => {
                        try {
                          const prices =
                            typeof market.outcomePrices === "string"
                              ? JSON.parse(market.outcomePrices)
                              : market.outcomePrices;
                          const yesPrice = Number(prices?.[0] || 0);
                          return yesPrice > 0 ? (
                            <span className="status-good">
                              Yes: {(yesPrice * 100).toFixed(0)}¢
                            </span>
                          ) : null;
                        } catch {
                          return null;
                        }
                      })()}
                  </div>
                </div>
              ))}
            </div>
          ) : (
            <div className="text-soft text-center py-8">
              Loading trending markets...
            </div>
          )}
        </div>
      </div>

      {/* Portfolio Value */}
      {portfolio &&
        (portfolio.total_invested > 0 || portfolio.usdc_balance > 0) && (
          <div className="surface-panel p-6">
            <h2 className="text-xl font-bold mb-4">Portfolio Overview</h2>
            <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
              <div>
                <p className="text-soft text-sm">Total Invested</p>
                <p className="text-lg font-semibold">
                  {formatUSD(portfolio.total_invested)}
                </p>
              </div>
              <div>
                <p className="text-soft text-sm">Current Value</p>
                <p className="text-lg font-semibold">
                  {formatUSD(portfolio.total_current_value)}
                </p>
              </div>
              <div>
                <p className="text-soft text-sm">Available USDC</p>
                <p className="text-lg font-semibold">
                  {formatUSD(portfolio.usdc_balance)}
                </p>
              </div>
              <div>
                <p className="text-soft text-sm">Total Portfolio</p>
                <p className="text-lg font-semibold">
                  {formatUSD(
                    portfolio.usdc_balance + portfolio.total_current_value,
                  )}
                </p>
              </div>
            </div>
          </div>
        )}

      {/* ── Modals ── */}
      {cashOutPosition && (
        <CashOutModal
          position={cashOutPosition}
          onClose={() => setCashOutPosition(null)}
          onSuccess={() => {
            setCashOutPosition(null);
            fetchData();
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

      {analysisPosition && (
        <PositionAnalysisPopup
          position={analysisPosition}
          clickX={analysisClickCoords.x}
          clickY={analysisClickCoords.y}
          onClose={() => setAnalysisPosition(null)}
        />
      )}
    </div>
  );
}
