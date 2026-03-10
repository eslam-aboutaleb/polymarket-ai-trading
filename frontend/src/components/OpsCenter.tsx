import { useEffect, useMemo, useState } from "react";
import {
  analysisService,
  CopyTradeEvalRequest,
  RiskAssessmentRequest,
  TradePlanRequest,
  TraderAnalysisRequest,
} from "../services/analysisService";
import {
  binanceSignalsService,
  BinanceDashboardData,
} from "../services/binanceSignalsService";
import {
  emergencyService,
  ArbitrageOpportunity,
  TraderQuality,
} from "../services/emergencyService";
import { marketsService, MarketCategory } from "../services/marketsService";
import { getApiErrorMessage } from "../utils/apiError";

const JSON_PRETTY_SPACE = 2;

export default function OpsCenter() {
  const [error, setError] = useState<string | null>(null);
  const [success, setSuccess] = useState<string | null>(null);

  // ── Binance data ────────────────────────────────────────────────
  const [binanceBusy, setBinanceBusy] = useState(false);
  const [binanceDashboard, setBinanceDashboard] =
    useState<BinanceDashboardData | null>(null);
  const [signalChain, setSignalChain] = useState("ethereum");
  const [signalLimit, setSignalLimit] = useState(10);
  const [smartMoneySignals, setSmartMoneySignals] = useState<any[]>([]);
  const [activeBuySignals, setActiveBuySignals] = useState<any[]>([]);
  const [socialHype, setSocialHype] = useState<any[]>([]);
  const [trendingTokens, setTrendingTokens] = useState<any[]>([]);
  const [smartMoneyInflow, setSmartMoneyInflow] = useState<any[]>([]);
  const [pnlLeaderboard, setPnlLeaderboard] = useState<any[]>([]);
  const [tokenQuery, setTokenQuery] = useState("");
  const [tokenSearchRows, setTokenSearchRows] = useState<any[]>([]);
  const [tokenAddress, setTokenAddress] = useState("");
  const [tokenChain, setTokenChain] = useState("ethereum");
  const [tokenData, setTokenData] = useState<Record<string, unknown> | null>(
    null,
  );

  // ── Risk ops ────────────────────────────────────────────────────
  const [riskBusy, setRiskBusy] = useState<string | null>(null);
  const [arbitrageRows, setArbitrageRows] = useState<ArbitrageOpportunity[]>([]);
  const [qualityWallet, setQualityWallet] = useState("");
  const [traderQuality, setTraderQuality] = useState<TraderQuality | null>(null);

  // ── Analysis lab ────────────────────────────────────────────────
  const [analysisBusy, setAnalysisBusy] = useState<string | null>(null);
  const [analysisOutput, setAnalysisOutput] = useState<string>("");
  const [analysisHealth, setAnalysisHealth] = useState<Record<string, unknown> | null>(null);

  const [marketScanJson, setMarketScanJson] = useState(
    JSON.stringify(
      [
        {
          question: "Will BTC be above 120k by year-end?",
          outcomePrices: ["0.46", "0.54"],
          volume24hr: 1500000,
          liquidity: 700000,
          condition_id: "sample-condition-id",
          _event_title: "Bitcoin Year-End Price",
          _event_slug: "bitcoin-year-end-price",
        },
      ],
      null,
      JSON_PRETTY_SPACE,
    ),
  );

  const [riskForm, setRiskForm] = useState<RiskAssessmentRequest>({
    market_title: "Bitcoin above 120k by year-end",
    position_size: 100,
    entry_price: 0.46,
    days_to_expiry: 120,
    correlation_info: "",
  });

  const [tradePlanForm, setTradePlanForm] = useState<TradePlanRequest>({
    action: "buy_yes",
    market_title: "Bitcoin above 120k by year-end",
    target_size: 100,
    current_price: 0.46,
    order_book: {},
  });

  const [traderForm, setTraderForm] = useState<TraderAnalysisRequest>({
    wallet_address: "",
    display_name: "",
    total_pnl: 0,
    win_rate: 0,
    trade_count: 0,
    markets_traded: 0,
    recent_trades_json: "[]",
  });

  const [copyEvalForm, setCopyEvalForm] = useState<CopyTradeEvalRequest>({
    trader_wallet: "",
    trader_stats: "",
    market_id: "",
    market_title: "",
    trade_side: "BUY",
    trade_size: 100,
    current_price: 0.5,
    user_risk_profile: "max_position_daily_loss",
  });

  // ── Markets browse by category ──────────────────────────────────
  const [categories, setCategories] = useState<MarketCategory[]>([]);
  const [selectedTag, setSelectedTag] = useState("");
  const [categoryMarkets, setCategoryMarkets] = useState<any[]>([]);
  const [categoryBusy, setCategoryBusy] = useState(false);

  useEffect(() => {
    void (async () => {
      try {
        const [catRes, arbRows, dash] = await Promise.all([
          marketsService.getCategories(),
          emergencyService.getArbitrageOpportunities(),
          binanceSignalsService.getDashboard(),
        ]);
        setCategories(catRes.categories || []);
        setArbitrageRows(arbRows || []);
        setBinanceDashboard(dash);
        if ((catRes.categories || [])[0]?.id) {
          setSelectedTag(catRes.categories[0].id);
        }
      } catch (err: unknown) {
        setError(getApiErrorMessage(err, "Failed to load Ops Center data"));
      }
    })();
  }, []);

  const setNoticeError = (err: unknown, fallback: string) => {
    setSuccess(null);
    setError(getApiErrorMessage(err, fallback));
  };

  const setNoticeSuccess = (message: string) => {
    setError(null);
    setSuccess(message);
  };

  // ── Binance actions ──────────────────────────────────────────────
  const loadBinanceDashboard = async () => {
    try {
      setBinanceBusy(true);
      const data = await binanceSignalsService.getDashboard();
      setBinanceDashboard(data);
      setNoticeSuccess("Binance dashboard refreshed.");
    } catch (err: unknown) {
      setNoticeError(err, "Failed to load Binance dashboard");
    } finally {
      setBinanceBusy(false);
    }
  };

  const loadSignalsAndRankings = async () => {
    try {
      setBinanceBusy(true);
      const [
        smartMoney,
        activeBuys,
        social,
        trending,
        inflow,
        pnl,
      ] = await Promise.all([
        binanceSignalsService.getSmartMoneySignals(signalChain, signalLimit),
        binanceSignalsService.getActiveBuySignals(signalChain, signalLimit),
        binanceSignalsService.getSocialHype(signalLimit),
        binanceSignalsService.getTrendingTokens(signalLimit),
        binanceSignalsService.getSmartMoneyInflow(signalLimit),
        binanceSignalsService.getPnlLeaderboard("7d", signalLimit),
      ]);
      setSmartMoneySignals(smartMoney);
      setActiveBuySignals(activeBuys);
      setSocialHype(social);
      setTrendingTokens(trending);
      setSmartMoneyInflow(inflow);
      setPnlLeaderboard(pnl);
      setNoticeSuccess("Loaded Binance signal/ranking endpoints.");
    } catch (err: unknown) {
      setNoticeError(err, "Failed to load Binance signals");
    } finally {
      setBinanceBusy(false);
    }
  };

  const runTokenSearch = async () => {
    if (!tokenQuery.trim()) return;
    try {
      setBinanceBusy(true);
      const rows = await binanceSignalsService.searchToken(tokenQuery.trim());
      setTokenSearchRows(rows || []);
      setNoticeSuccess("Token search completed.");
    } catch (err: unknown) {
      setNoticeError(err, "Token search failed");
    } finally {
      setBinanceBusy(false);
    }
  };

  const runTokenData = async () => {
    if (!tokenAddress.trim()) return;
    try {
      setBinanceBusy(true);
      const data = await binanceSignalsService.getTokenData(
        tokenAddress.trim(),
        tokenChain,
      );
      setTokenData(data || {});
      setNoticeSuccess("Token data loaded.");
    } catch (err: unknown) {
      setNoticeError(err, "Token data request failed");
    } finally {
      setBinanceBusy(false);
    }
  };

  // ── Risk ops actions ─────────────────────────────────────────────
  const runEmergencyStop = async () => {
    try {
      setRiskBusy("emergency_stop");
      const res = await emergencyService.emergencyStop(true);
      setNoticeSuccess(res.message || "Emergency stop executed.");
    } catch (err: unknown) {
      setNoticeError(err, "Emergency stop failed");
    } finally {
      setRiskBusy(null);
    }
  };

  const runResumeTrading = async () => {
    try {
      setRiskBusy("resume");
      const res = await emergencyService.resumeTrading();
      setNoticeSuccess(res.message || "Trading resumed.");
    } catch (err: unknown) {
      setNoticeError(err, "Resume trading failed");
    } finally {
      setRiskBusy(null);
    }
  };

  const runArbitrageScan = async () => {
    try {
      setRiskBusy("scan_arbitrage");
      const res = await emergencyService.scanArbitrage();
      setArbitrageRows(res.opportunities || []);
      setNoticeSuccess(`Arbitrage scan complete. Found ${res.found} opportunities.`);
    } catch (err: unknown) {
      setNoticeError(err, "Arbitrage scan failed");
    } finally {
      setRiskBusy(null);
    }
  };

  const refreshArbitrage = async () => {
    try {
      setRiskBusy("refresh_arbitrage");
      const rows = await emergencyService.getArbitrageOpportunities();
      setArbitrageRows(rows || []);
      setNoticeSuccess("Arbitrage opportunities refreshed.");
    } catch (err: unknown) {
      setNoticeError(err, "Failed to refresh arbitrage opportunities");
    } finally {
      setRiskBusy(null);
    }
  };

  const runRescoreTraders = async () => {
    try {
      setRiskBusy("rescore_traders");
      const res = await emergencyService.rescoreAllTraders();
      setNoticeSuccess(res.message || `Rescored ${res.scored} traders.`);
    } catch (err: unknown) {
      setNoticeError(err, "Trader rescore failed");
    } finally {
      setRiskBusy(null);
    }
  };

  const lookupTraderQuality = async () => {
    if (!qualityWallet.trim()) return;
    try {
      setRiskBusy("quality");
      const quality = await emergencyService.getTraderQuality(
        qualityWallet.trim(),
      );
      setTraderQuality(quality);
      setNoticeSuccess("Trader quality loaded.");
    } catch (err: unknown) {
      setNoticeError(err, "Failed to fetch trader quality");
    } finally {
      setRiskBusy(null);
    }
  };

  // ── Analysis actions ─────────────────────────────────────────────
  const writeAnalysisOutput = (payload: unknown) => {
    setAnalysisOutput(JSON.stringify(payload, null, JSON_PRETTY_SPACE));
  };

  const runAnalysisHealth = async () => {
    try {
      setAnalysisBusy("health");
      const health = await analysisService.healthCheck();
      setAnalysisHealth(health as unknown as Record<string, unknown>);
      writeAnalysisOutput(health);
      setNoticeSuccess("Analysis backend health loaded.");
    } catch (err: unknown) {
      setNoticeError(err, "Failed to fetch analysis backend health");
    } finally {
      setAnalysisBusy(null);
    }
  };

  const runMarketScan = async () => {
    try {
      setAnalysisBusy("scan");
      const parsed = JSON.parse(marketScanJson);
      const markets = Array.isArray(parsed) ? parsed : [];
      const res = await analysisService.scanMarkets({ markets });
      writeAnalysisOutput(res);
      setNoticeSuccess("Market scan completed.");
    } catch (err: unknown) {
      setNoticeError(err, "Market scan failed (check JSON format)");
    } finally {
      setAnalysisBusy(null);
    }
  };

  const runRiskAssessment = async () => {
    try {
      setAnalysisBusy("risk");
      const res = await analysisService.assessRisk(riskForm);
      writeAnalysisOutput(res);
      setNoticeSuccess("Risk assessment completed.");
    } catch (err: unknown) {
      setNoticeError(err, "Risk assessment failed");
    } finally {
      setAnalysisBusy(null);
    }
  };

  const runTradePlan = async () => {
    try {
      setAnalysisBusy("trade_plan");
      const res = await analysisService.generateTradePlan(tradePlanForm);
      writeAnalysisOutput(res);
      setNoticeSuccess("Trade plan generated.");
    } catch (err: unknown) {
      setNoticeError(err, "Trade plan generation failed");
    } finally {
      setAnalysisBusy(null);
    }
  };

  const runTraderAnalysis = async () => {
    if (!traderForm.wallet_address.trim()) return;
    try {
      setAnalysisBusy("trader");
      const res = await analysisService.analyzeTrader({
        ...traderForm,
        wallet_address: traderForm.wallet_address.trim(),
      });
      writeAnalysisOutput(res);
      setNoticeSuccess("Trader analysis completed.");
    } catch (err: unknown) {
      setNoticeError(err, "Trader analysis failed");
    } finally {
      setAnalysisBusy(null);
    }
  };

  const runCopyTradeEval = async () => {
    if (!copyEvalForm.trader_wallet?.trim()) return;
    try {
      setAnalysisBusy("copy_eval");
      const res = await analysisService.evaluateCopyTrade({
        ...copyEvalForm,
        trader_wallet: copyEvalForm.trader_wallet.trim(),
      });
      writeAnalysisOutput(res);
      setNoticeSuccess("Copy-trade evaluation completed.");
    } catch (err: unknown) {
      setNoticeError(err, "Copy-trade evaluation failed");
    } finally {
      setAnalysisBusy(null);
    }
  };

  // ── Markets browse endpoint action ───────────────────────────────
  const loadCategoryMarkets = async () => {
    if (!selectedTag) return;
    try {
      setCategoryBusy(true);
      const res = await marketsService.getMarketsByCategory(selectedTag, 25);
      setCategoryMarkets(res.markets || []);
      setNoticeSuccess(`Loaded ${res.count} markets for "${selectedTag}".`);
    } catch (err: unknown) {
      setNoticeError(err, "Category market browse failed");
    } finally {
      setCategoryBusy(false);
    }
  };

  const arbitragePreview = useMemo(() => arbitrageRows.slice(0, 8), [arbitrageRows]);

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-3xl font-bold">Ops Center</h1>
        <p className="text-soft text-sm mt-1">
          Binance intelligence, risk controls, and advanced analysis endpoints.
        </p>
      </div>

      <section className="surface-panel p-5 space-y-3">
        <div className="flex items-center justify-between gap-3 flex-wrap">
          <h2 className="text-lg font-semibold">How To Use Ops Center</h2>
          <a href="/guide" className="btn-muted text-xs px-3 py-1.5">
            Open Full Guide
          </a>
        </div>
        <div className="grid gap-3 md:grid-cols-2">
          <div className="rounded border border-[var(--line)] bg-[var(--bg-soft)] p-3">
            <p className="text-xs font-semibold mb-1">1. Risk Controls</p>
            <p className="text-xs text-soft">
              Use for emergency stop, resume, trader rescoring, arbitrage scanning,
              and trader quality checks. Start here when you need immediate risk
              intervention.
            </p>
          </div>
          <div className="rounded border border-[var(--line)] bg-[var(--bg-soft)] p-3">
            <p className="text-xs font-semibold mb-1">2. Binance Signals</p>
            <p className="text-xs text-soft">
              Use to refresh Binance intelligence endpoints and inspect token-level
              market activity before taking discretionary trades.
            </p>
          </div>
          <div className="rounded border border-[var(--line)] bg-[var(--bg-soft)] p-3">
            <p className="text-xs font-semibold mb-1">3. Analysis Lab</p>
            <p className="text-xs text-soft">
              Fill request forms and run backend analysis endpoints. Output JSON
              appears at the bottom of the section for quick validation.
            </p>
          </div>
          <div className="rounded border border-[var(--line)] bg-[var(--bg-soft)] p-3">
            <p className="text-xs font-semibold mb-1">4. Category Browse</p>
            <p className="text-xs text-soft">
              Pull markets from <span className="mono">/api/markets/browse</span> by
              category to confirm market inventory and backend browse behavior.
            </p>
          </div>
        </div>
      </section>

      {error && <div className="alert-error p-3 rounded text-sm">{error}</div>}
      {success && <div className="alert-success p-3 rounded text-sm">{success}</div>}

      <section className="surface-panel p-5 space-y-4">
        <h2 className="text-lg font-semibold">Risk Controls</h2>
        <div className="flex flex-wrap gap-2">
          <button
            type="button"
            className="btn-danger text-xs"
            onClick={runEmergencyStop}
            disabled={riskBusy !== null}
          >
            {riskBusy === "emergency_stop" ? "Running..." : "Emergency Stop + Close Positions"}
          </button>
          <button
            type="button"
            className="btn-success text-xs"
            onClick={runResumeTrading}
            disabled={riskBusy !== null}
          >
            {riskBusy === "resume" ? "Running..." : "Resume Trading"}
          </button>
          <button
            type="button"
            className="btn-muted text-xs"
            onClick={runRescoreTraders}
            disabled={riskBusy !== null}
          >
            {riskBusy === "rescore_traders" ? "Running..." : "Rescore Traders"}
          </button>
          <button
            type="button"
            className="btn-muted text-xs"
            onClick={runArbitrageScan}
            disabled={riskBusy !== null}
          >
            {riskBusy === "scan_arbitrage" ? "Running..." : "Scan Arbitrage Now"}
          </button>
          <button
            type="button"
            className="btn-muted text-xs"
            onClick={refreshArbitrage}
            disabled={riskBusy !== null}
          >
            {riskBusy === "refresh_arbitrage" ? "Refreshing..." : "Refresh Arbitrage Feed"}
          </button>
        </div>

        <div className="grid gap-3 md:grid-cols-[minmax(0,1fr)_auto]">
          <input
            type="text"
            value={qualityWallet}
            onChange={(e) => setQualityWallet(e.target.value)}
            placeholder="Trader wallet for quality score (0x...)"
            className="input-field w-full"
          />
          <button
            type="button"
            className="btn-primary text-xs"
            onClick={lookupTraderQuality}
            disabled={riskBusy !== null || !qualityWallet.trim()}
          >
            {riskBusy === "quality" ? "Loading..." : "Lookup Trader Quality"}
          </button>
        </div>

        {traderQuality && (
          <pre className="rounded border border-[var(--line)] bg-[var(--bg-soft)] p-3 text-xs overflow-auto">
            {JSON.stringify(traderQuality, null, JSON_PRETTY_SPACE)}
          </pre>
        )}

        <div>
          <p className="text-xs text-soft mb-2">
            Arbitrage opportunities: {arbitrageRows.length}
          </p>
          <div className="space-y-2 max-h-52 overflow-y-auto scroll-soft">
            {arbitragePreview.map((row, index) => (
              <div
                key={`${row.market_id}-${index}`}
                className="rounded border border-[var(--line)] bg-[var(--bg-soft)] p-2 text-xs"
              >
                <p className="font-semibold text-white">{row.market_title}</p>
                <p className="text-soft">
                  {row.type.toUpperCase()} · Profit {row.profit_pct ?? row.spread_pct ?? 0}%
                </p>
              </div>
            ))}
            {arbitragePreview.length === 0 && (
              <p className="text-xs text-soft">No arbitrage rows loaded yet.</p>
            )}
          </div>
        </div>
      </section>

      <section className="surface-panel p-5 space-y-4">
        <h2 className="text-lg font-semibold">Binance Signals</h2>
        <div className="flex flex-wrap gap-2">
          <button
            type="button"
            className="btn-primary text-xs"
            onClick={loadBinanceDashboard}
            disabled={binanceBusy}
          >
            {binanceBusy ? "Loading..." : "Load Dashboard"}
          </button>
          <button
            type="button"
            className="btn-muted text-xs"
            onClick={loadSignalsAndRankings}
            disabled={binanceBusy}
          >
            {binanceBusy ? "Loading..." : "Load All Signal Endpoints"}
          </button>
        </div>

        <div className="grid gap-3 md:grid-cols-3">
          <input
            type="text"
            value={signalChain}
            onChange={(e) => setSignalChain(e.target.value)}
            placeholder="chain (ethereum)"
            className="input-field w-full"
          />
          <input
            type="number"
            value={signalLimit}
            onChange={(e) => setSignalLimit(Math.max(1, Number(e.target.value) || 1))}
            className="input-field w-full"
            min={1}
            max={50}
          />
          <span className="text-xs text-soft self-center">
            Dashboard enabled: {binanceDashboard?.enabled ? "true" : "false"}
          </span>
        </div>

        <div className="grid gap-3 md:grid-cols-2">
          <div>
            <p className="text-xs text-soft mb-1">Token search</p>
            <div className="flex gap-2">
              <input
                type="text"
                value={tokenQuery}
                onChange={(e) => setTokenQuery(e.target.value)}
                className="input-field w-full"
                placeholder="Search token symbol/name"
              />
              <button
                type="button"
                className="btn-muted text-xs"
                onClick={runTokenSearch}
                disabled={binanceBusy || !tokenQuery.trim()}
              >
                Search
              </button>
            </div>
          </div>
          <div>
            <p className="text-xs text-soft mb-1">Token dynamic data</p>
            <div className="flex gap-2">
              <input
                type="text"
                value={tokenAddress}
                onChange={(e) => setTokenAddress(e.target.value)}
                className="input-field w-full"
                placeholder="Token contract address"
              />
              <input
                type="text"
                value={tokenChain}
                onChange={(e) => setTokenChain(e.target.value)}
                className="input-field w-32"
                placeholder="chain"
              />
              <button
                type="button"
                className="btn-muted text-xs"
                onClick={runTokenData}
                disabled={binanceBusy || !tokenAddress.trim()}
              >
                Load
              </button>
            </div>
          </div>
        </div>

        <div className="grid gap-3 md:grid-cols-3 text-xs">
          <div className="rounded border border-[var(--line)] p-2 bg-[var(--bg-soft)]">
            <p className="font-semibold mb-1">Smart Money</p>
            <p className="text-soft">{smartMoneySignals.length} rows</p>
          </div>
          <div className="rounded border border-[var(--line)] p-2 bg-[var(--bg-soft)]">
            <p className="font-semibold mb-1">Active Buys</p>
            <p className="text-soft">{activeBuySignals.length} rows</p>
          </div>
          <div className="rounded border border-[var(--line)] p-2 bg-[var(--bg-soft)]">
            <p className="font-semibold mb-1">Social Hype</p>
            <p className="text-soft">{socialHype.length} rows</p>
          </div>
          <div className="rounded border border-[var(--line)] p-2 bg-[var(--bg-soft)]">
            <p className="font-semibold mb-1">Trending</p>
            <p className="text-soft">{trendingTokens.length} rows</p>
          </div>
          <div className="rounded border border-[var(--line)] p-2 bg-[var(--bg-soft)]">
            <p className="font-semibold mb-1">Smart Money Inflow</p>
            <p className="text-soft">{smartMoneyInflow.length} rows</p>
          </div>
          <div className="rounded border border-[var(--line)] p-2 bg-[var(--bg-soft)]">
            <p className="font-semibold mb-1">PnL Leaderboard</p>
            <p className="text-soft">{pnlLeaderboard.length} rows</p>
          </div>
        </div>

        {(tokenSearchRows.length > 0 || tokenData) && (
          <pre className="rounded border border-[var(--line)] bg-[var(--bg-soft)] p-3 text-xs overflow-auto max-h-60">
            {JSON.stringify(
              { token_search: tokenSearchRows.slice(0, 10), token_data: tokenData },
              null,
              JSON_PRETTY_SPACE,
            )}
          </pre>
        )}
      </section>

      <section className="surface-panel p-5 space-y-4">
        <h2 className="text-lg font-semibold">Analysis Lab</h2>
        <p className="text-xs text-soft">
          This panel is a direct frontend harness for analysis endpoints. Run health
          first, then scan/risk/trade-plan, then trader/copy evaluation.
        </p>
        <div className="flex flex-wrap gap-2">
          <button
            type="button"
            className="btn-primary text-xs"
            onClick={runAnalysisHealth}
            disabled={analysisBusy !== null}
          >
            {analysisBusy === "health" ? "Loading..." : "Check Analysis Health"}
          </button>
          <button
            type="button"
            className="btn-muted text-xs"
            onClick={runMarketScan}
            disabled={analysisBusy !== null}
          >
            {analysisBusy === "scan" ? "Running..." : "Run Market Scan"}
          </button>
          <button
            type="button"
            className="btn-muted text-xs"
            onClick={runRiskAssessment}
            disabled={analysisBusy !== null}
          >
            {analysisBusy === "risk" ? "Running..." : "Run Risk Assessment"}
          </button>
          <button
            type="button"
            className="btn-muted text-xs"
            onClick={runTradePlan}
            disabled={analysisBusy !== null}
          >
            {analysisBusy === "trade_plan" ? "Running..." : "Generate Trade Plan"}
          </button>
          <button
            type="button"
            className="btn-muted text-xs"
            onClick={runTraderAnalysis}
            disabled={analysisBusy !== null || !traderForm.wallet_address.trim()}
          >
            {analysisBusy === "trader" ? "Running..." : "Analyze Trader (non-stream)"}
          </button>
          <button
            type="button"
            className="btn-muted text-xs"
            onClick={runCopyTradeEval}
            disabled={analysisBusy !== null || !copyEvalForm.trader_wallet?.trim()}
          >
            {analysisBusy === "copy_eval" ? "Running..." : "Evaluate Copy Trade"}
          </button>
        </div>

        <div className="grid gap-3 lg:grid-cols-[minmax(0,1.15fr)_minmax(0,1fr)]">
          <div className="min-w-0">
            <p className="text-xs text-soft mb-1">Market scan JSON (array)</p>
            <textarea
              value={marketScanJson}
              onChange={(e) => setMarketScanJson(e.target.value)}
              rows={12}
              className="w-full rounded border border-[var(--line)] bg-[var(--bg-soft)] p-2 text-xs mono min-h-[210px]"
            />
          </div>
          <div className="space-y-3 min-w-0">
            <div className="rounded border border-[var(--line)] bg-[var(--bg-soft)] p-3">
              <p className="text-xs font-semibold mb-2">Risk Request</p>
              <input
                className="input-field w-full mb-2"
                value={riskForm.market_title}
                onChange={(e) =>
                  setRiskForm((prev) => ({ ...prev, market_title: e.target.value }))
                }
                placeholder="Market title"
              />
              <div className="grid grid-cols-1 sm:grid-cols-3 gap-2">
                <input
                  type="number"
                  className="input-field w-full"
                  value={riskForm.position_size}
                  onChange={(e) =>
                    setRiskForm((prev) => ({
                      ...prev,
                      position_size: Number(e.target.value) || 0,
                    }))
                  }
                  placeholder="Size"
                />
                <input
                  type="number"
                  step="0.01"
                  className="input-field w-full"
                  value={riskForm.entry_price}
                  onChange={(e) =>
                    setRiskForm((prev) => ({
                      ...prev,
                      entry_price: Number(e.target.value) || 0,
                    }))
                  }
                  placeholder="Price"
                />
                <input
                  type="number"
                  className="input-field w-full"
                  value={riskForm.days_to_expiry}
                  onChange={(e) =>
                    setRiskForm((prev) => ({
                      ...prev,
                      days_to_expiry: Number(e.target.value) || 1,
                    }))
                  }
                  placeholder="Days"
                />
              </div>
            </div>

            <div className="rounded border border-[var(--line)] bg-[var(--bg-soft)] p-3">
              <p className="text-xs font-semibold mb-2">Trade Plan Request</p>
              <div className="grid grid-cols-1 sm:grid-cols-2 gap-2 mb-2">
                <select
                  className="input-field w-full"
                  value={tradePlanForm.action}
                  onChange={(e) =>
                    setTradePlanForm((prev) => ({
                      ...prev,
                      action: e.target.value as TradePlanRequest["action"],
                    }))
                  }
                >
                  <option value="buy_yes">buy_yes</option>
                  <option value="buy_no">buy_no</option>
                  <option value="sell_yes">sell_yes</option>
                  <option value="sell_no">sell_no</option>
                </select>
                <input
                  type="number"
                  className="input-field w-full"
                  value={tradePlanForm.target_size}
                  onChange={(e) =>
                    setTradePlanForm((prev) => ({
                      ...prev,
                      target_size: Number(e.target.value) || 0,
                    }))
                  }
                  placeholder="Target size"
                />
              </div>
              <input
                className="input-field w-full mb-2"
                value={tradePlanForm.market_title}
                onChange={(e) =>
                  setTradePlanForm((prev) => ({
                    ...prev,
                    market_title: e.target.value,
                  }))
                }
                placeholder="Market title"
              />
              <input
                type="number"
                step="0.01"
                className="input-field w-full"
                value={tradePlanForm.current_price}
                onChange={(e) =>
                  setTradePlanForm((prev) => ({
                    ...prev,
                    current_price: Number(e.target.value) || 0,
                  }))
                }
                placeholder="Current price"
              />
            </div>
          </div>
        </div>

        <div className="grid gap-3 lg:grid-cols-2">
          <div className="rounded border border-[var(--line)] bg-[var(--bg-soft)] p-3 min-w-0">
            <p className="text-xs font-semibold mb-2">Trader Analysis Request</p>
            <input
              className="input-field w-full mb-2"
              value={traderForm.wallet_address}
              onChange={(e) =>
                setTraderForm((prev) => ({ ...prev, wallet_address: e.target.value }))
              }
              placeholder="Trader wallet (0x...)"
            />
            <div className="grid grid-cols-1 sm:grid-cols-2 gap-2">
              <input
                type="number"
                className="input-field w-full"
                value={traderForm.total_pnl}
                onChange={(e) =>
                  setTraderForm((prev) => ({
                    ...prev,
                    total_pnl: Number(e.target.value) || 0,
                  }))
                }
                placeholder="Total PnL"
              />
              <input
                type="number"
                className="input-field w-full"
                value={traderForm.win_rate}
                onChange={(e) =>
                  setTraderForm((prev) => ({
                    ...prev,
                    win_rate: Number(e.target.value) || 0,
                  }))
                }
                placeholder="Win rate"
              />
            </div>
          </div>

          <div className="rounded border border-[var(--line)] bg-[var(--bg-soft)] p-3 min-w-0">
            <p className="text-xs font-semibold mb-2">Copy Trade Eval Request</p>
            <input
              className="input-field w-full mb-2"
              value={copyEvalForm.trader_wallet || ""}
              onChange={(e) =>
                setCopyEvalForm((prev) => ({ ...prev, trader_wallet: e.target.value }))
              }
              placeholder="Trader wallet (0x...)"
            />
            <input
              className="input-field w-full mb-2"
              value={copyEvalForm.market_title || ""}
              onChange={(e) =>
                setCopyEvalForm((prev) => ({ ...prev, market_title: e.target.value }))
              }
              placeholder="Market title"
            />
            <div className="grid grid-cols-1 sm:grid-cols-3 gap-2">
              <select
                className="input-field w-full"
                value={copyEvalForm.trade_side || "BUY"}
                onChange={(e) =>
                  setCopyEvalForm((prev) => ({
                    ...prev,
                    trade_side: e.target.value as "BUY" | "SELL",
                  }))
                }
              >
                <option value="BUY">BUY</option>
                <option value="SELL">SELL</option>
              </select>
              <input
                type="number"
                className="input-field w-full"
                value={copyEvalForm.trade_size || 0}
                onChange={(e) =>
                  setCopyEvalForm((prev) => ({
                    ...prev,
                    trade_size: Number(e.target.value) || 0,
                  }))
                }
                placeholder="Trade size"
              />
              <input
                type="number"
                step="0.01"
                className="input-field w-full"
                value={copyEvalForm.current_price || 0}
                onChange={(e) =>
                  setCopyEvalForm((prev) => ({
                    ...prev,
                    current_price: Number(e.target.value) || 0,
                  }))
                }
                placeholder="Price"
              />
            </div>
          </div>
        </div>

        {analysisHealth && (
          <p className="text-xs text-soft">
            Health status: <span className="mono">{String(analysisHealth.status || "unknown")}</span>
          </p>
        )}
        {analysisOutput && (
          <pre className="rounded border border-[var(--line)] bg-[var(--bg-soft)] p-3 text-xs overflow-auto max-h-72 whitespace-pre-wrap break-words">
            {analysisOutput}
          </pre>
        )}
      </section>

      <section className="surface-panel p-5 space-y-3">
        <h2 className="text-lg font-semibold">Category Browse Endpoint</h2>
        <p className="text-xs text-soft">
          This uses <span className="mono">/api/markets/browse</span> directly.
        </p>
        <div className="flex flex-wrap items-center gap-2">
          <select
            className="input-field"
            value={selectedTag}
            onChange={(e) => setSelectedTag(e.target.value)}
          >
            <option value="">Select category</option>
            {categories.map((cat) => (
              <option key={cat.id} value={cat.id}>
                {cat.label}
              </option>
            ))}
          </select>
          <button
            type="button"
            className="btn-muted text-xs"
            onClick={loadCategoryMarkets}
            disabled={!selectedTag || categoryBusy}
          >
            {categoryBusy ? "Loading..." : "Load Category Markets"}
          </button>
          <span className="text-xs text-soft">Loaded: {categoryMarkets.length}</span>
        </div>
        {categoryMarkets.length > 0 && (
          <div className="space-y-2 max-h-56 overflow-y-auto scroll-soft">
            {categoryMarkets.slice(0, 12).map((market, idx) => (
              <div
                key={`${market.condition_id || market.question || idx}`}
                className="rounded border border-[var(--line)] bg-[var(--bg-soft)] p-2"
              >
                <p className="text-xs font-semibold text-white">
                  {market.question || market._event_title || "Unknown market"}
                </p>
              </div>
            ))}
          </div>
        )}
      </section>
    </div>
  );
}
