import { useCallback, useEffect, useMemo, useState } from "react";
import { createPortal } from "react-dom";
import {
  tradesService,
  ExecuteTradeRequest,
  ExecuteTradeResponse,
} from "../services/tradesService";
import {
  portfolioService,
  PortfolioSummary,
  PolymarketPosition,
} from "../services/portfolioService";
import { getApiErrorMessage } from "../utils/apiError";
import {
  getDefaultTradePreferences,
  loadTradePreferences,
  saveTradePreferences,
} from "../utils/tradePreferences";
import { pushRecentTradeMarket } from "../utils/tradingWorkspace";
import {
  TradePresetMode,
  TradePreferences,
  TradeTicketOutcome,
  TradeTicketSide,
  TradeTicketSource,
  TradeTicketToken,
  TradeValidationResult,
} from "../types/trading";

export interface TradeModalMarket {
  market_id: string;
  title: string;
  image?: string;
  watch_key?: string;
  tokens: TradeTicketToken[];
  bestAsk?: number | string | null;
  bestBid?: number | string | null;
  liquidity?: number;
  quote_timestamp?: string;
}

interface TradeModalProps {
  market: TradeModalMarket | null;
  source: TradeTicketSource;
  defaultSide?: TradeTicketSide;
  defaultOutcome?: TradeTicketOutcome;
  defaultAmount?: string;
  onClose: () => void;
  onSuccess?: (result: ExecuteTradeResponse) => void;
}

const MIN_BUY_USDC = 1;
const MAX_INPUT_DECIMALS = 6;
const LOW_LIQUIDITY_THRESHOLD = 5_000;

const FIXED_BUY_PRESETS = [10, 25, 50, 100];
const FIXED_SELL_PRESETS = [10, 50, 100, 500];
const PERCENT_PRESETS = [1, 2, 5, 10];

const pct = (v: number) => `${(v * 100).toFixed(1)}c`;

const fmt$ = (v: number) =>
  `$${v.toLocaleString("en-US", {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  })}`;

const fmtNumber = (value: number, decimals: number) => {
  if (!Number.isFinite(value) || value <= 0) return "";
  const normalized = Number(value.toFixed(decimals));
  return String(normalized);
};

const toFinite = (v: unknown): number | null => {
  const n = Number(v);
  return Number.isFinite(n) ? n : null;
};

const normalizeTokenOutcome = (value: string): TradeTicketOutcome =>
  value.toLowerCase() === "no" ? "No" : "Yes";

const normalizeOutcome = (value: string): string => value.trim().toLowerCase();

function isEditableTarget(target: EventTarget | null): boolean {
  const el = target as HTMLElement | null;
  if (!el) return false;
  if (el.isContentEditable) return true;
  const tagName = el.tagName.toLowerCase();
  if (tagName === "input" || tagName === "textarea" || tagName === "select") {
    return true;
  }
  return !!el.closest("input, textarea, select, [contenteditable='true']");
}

function findAvailableShares(
  summary: PortfolioSummary | null,
  marketId: string,
  tokenId: string,
  outcome: TradeTicketOutcome,
): number | null {
  if (!summary?.positions?.length) return null;

  const normalizedOutcome = normalizeOutcome(outcome);
  const marketKey = marketId.trim();

  let totalByToken = 0;
  let totalByConditionOutcome = 0;

  for (const row of summary.positions as PolymarketPosition[]) {
    const size = Number(row.size || 0);
    if (!Number.isFinite(size) || size <= 0) continue;

    const assetId = String(row.asset_id || "").trim();
    const conditionId = String(row.condition_id || row.conditionId || "").trim();
    const rowOutcome = normalizeOutcome(String(row.outcome || ""));

    if (tokenId && assetId && assetId === tokenId) {
      totalByToken += size;
      continue;
    }

    if (
      marketKey &&
      conditionId &&
      conditionId === marketKey &&
      rowOutcome === normalizedOutcome
    ) {
      totalByConditionOutcome += size;
    }
  }

  const resolved = totalByToken > 0 ? totalByToken : totalByConditionOutcome;
  return resolved > 0 ? resolved : 0;
}

function getQuoteTimestamp(market: TradeModalMarket | null): number {
  if (!market) return Date.now();
  if (!market.quote_timestamp) return Date.now();
  const parsed = new Date(market.quote_timestamp).getTime();
  if (!Number.isFinite(parsed) || parsed <= 0) return Date.now();
  return parsed;
}

function buildValidationResult(params: {
  amount: string;
  amountNum: number;
  side: TradeTicketSide;
  activeTokenExists: boolean;
  usdcBalance: number | null;
  availableShares: number | null;
  highNotionalThreshold: number;
  activePrice: number;
  quoteAgeSec: number;
  spread: number | null;
  liquidity: number | null;
}): TradeValidationResult {
  const {
    amount,
    amountNum,
    side,
    activeTokenExists,
    usdcBalance,
    availableShares,
    highNotionalThreshold,
    activePrice,
    quoteAgeSec,
    spread,
    liquidity,
  } = params;

  const errors: string[] = [];
  const warnings: string[] = [];

  const cleaned = amount.trim();
  if (!cleaned) {
    errors.push("Enter an amount.");
  } else {
    if (!/^\d*\.?\d*$/.test(cleaned)) {
      errors.push("Amount must be numeric.");
    }

    const decimals = cleaned.includes(".") ? cleaned.split(".")[1].length : 0;
    if (decimals > MAX_INPUT_DECIMALS) {
      errors.push(`Amount supports up to ${MAX_INPUT_DECIMALS} decimals.`);
    }

    if (!Number.isFinite(amountNum)) {
      errors.push("Amount is invalid.");
    } else if (amountNum <= 0) {
      errors.push("Amount must be greater than zero.");
    }
  }

  if (!activeTokenExists) {
    errors.push("Selected outcome token is not available for this market.");
  }

  if (side === "BUY") {
    if (amountNum > 0 && amountNum < MIN_BUY_USDC) {
      errors.push(`Minimum buy amount is ${fmt$(MIN_BUY_USDC)}.`);
    }
    if (
      usdcBalance != null &&
      Number.isFinite(usdcBalance) &&
      amountNum > usdcBalance + 1e-9
    ) {
      errors.push(`Insufficient USDC balance. Available: ${fmt$(usdcBalance)}.`);
    }
  }

  if (side === "SELL") {
    if (
      availableShares != null &&
      Number.isFinite(availableShares) &&
      amountNum > availableShares + 1e-9
    ) {
      errors.push(
        `Insufficient shares. Available: ${availableShares.toFixed(4)} ${"shares"}.`,
      );
    }
  }

  const notional = side === "BUY" ? amountNum : amountNum * activePrice;
  const requiresConfirm =
    Number.isFinite(notional) && notional >= Math.max(1, highNotionalThreshold);

  if (quoteAgeSec > 15) {
    warnings.push("Quote is stale (>15s). Refresh market context before submitting.");
  }

  if (spread != null && spread >= 0.04) {
    warnings.push(`Wide spread detected (${(spread * 100).toFixed(1)}c).`);
  }

  if (liquidity != null && liquidity > 0 && liquidity < LOW_LIQUIDITY_THRESHOLD) {
    warnings.push(
      `Low liquidity market (${fmt$(liquidity)}). Slippage risk can be elevated.`,
    );
  }

  if (requiresConfirm) {
    warnings.push(
      `Large order notional (${fmt$(notional)}). Confirmation required before submit.`,
    );
  }

  return { errors, warnings, requiresConfirm };
}

export default function TradeModal({
  market,
  source,
  defaultSide,
  defaultOutcome,
  defaultAmount,
  onClose,
  onSuccess,
}: TradeModalProps) {
  const [outcome, setOutcome] = useState<TradeTicketOutcome>("Yes");
  const [side, setSide] = useState<TradeTicketSide>("BUY");
  const [amount, setAmount] = useState("");
  const [presetMode, setPresetMode] = useState<TradePresetMode>("fixed");
  const [highNotionalThreshold, setHighNotionalThreshold] = useState(250);

  const [portfolioSummary, setPortfolioSummary] = useState<PortfolioSummary | null>(
    null,
  );
  const [balanceLoading, setBalanceLoading] = useState(false);
  const [balanceError, setBalanceError] = useState<string | null>(null);

  const [loading, setLoading] = useState(false);
  const [result, setResult] = useState<ExecuteTradeResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [confirmArmed, setConfirmArmed] = useState(false);
  const [quoteNowTs, setQuoteNowTs] = useState(Date.now());

  const yesToken = useMemo(
    () => market?.tokens.find((t) => normalizeTokenOutcome(t.outcome) === "Yes"),
    [market],
  );
  const noToken = useMemo(
    () => market?.tokens.find((t) => normalizeTokenOutcome(t.outcome) === "No"),
    [market],
  );

  const activeToken = outcome === "Yes" ? yesToken : noToken;
  const activePrice = Number(activeToken?.price ?? 0.5);

  const amountNum = Number(amount);
  const shares =
    side === "BUY"
      ? activePrice > 0 && Number.isFinite(amountNum)
        ? amountNum / activePrice
        : 0
      : Number.isFinite(amountNum)
        ? amountNum
        : 0;
  const notional = side === "BUY" ? amountNum : shares * activePrice;
  const potentialPayout = side === "BUY" ? shares : notional;
  const potentialProfit = side === "BUY" ? potentialPayout - notional : 0;

  const usdcBalance =
    portfolioSummary && Number.isFinite(Number(portfolioSummary.usdc_balance))
      ? Number(portfolioSummary.usdc_balance)
      : null;

  const availableShares = useMemo(() => {
    if (!market || !activeToken) return null;
    return findAvailableShares(
      portfolioSummary,
      market.market_id,
      activeToken.token_id,
      outcome,
    );
  }, [activeToken, market, outcome, portfolioSummary]);

  const quoteAgeSec = useMemo(() => {
    const quoteTs = getQuoteTimestamp(market);
    return Math.max(0, Math.floor((quoteNowTs - quoteTs) / 1000));
  }, [market, quoteNowTs]);

  const spread = useMemo(() => {
    if (!market) return null;
    const bestAsk = toFinite(market.bestAsk);
    const bestBid = toFinite(market.bestBid);
    if (bestAsk == null || bestBid == null) return null;
    if (bestAsk <= 0 || bestAsk >= 1 || bestBid < 0 || bestBid >= 1) return null;
    if (bestAsk < bestBid) return null;
    return bestAsk - bestBid;
  }, [market]);

  const liquidity = useMemo(() => {
    if (!market) return null;
    const n = toFinite(market.liquidity);
    return n != null && n > 0 ? n : null;
  }, [market]);

  const validation = useMemo(
    () =>
      buildValidationResult({
        amount,
        amountNum,
        side,
        activeTokenExists: !!activeToken,
        usdcBalance,
        availableShares,
        highNotionalThreshold,
        activePrice,
        quoteAgeSec,
        spread,
        liquidity,
      }),
    [
      amount,
      amountNum,
      side,
      activeToken,
      usdcBalance,
      availableShares,
      highNotionalThreshold,
      activePrice,
      quoteAgeSec,
      spread,
      liquidity,
    ],
  );

  const fixedPresets = side === "BUY" ? FIXED_BUY_PRESETS : FIXED_SELL_PRESETS;

  const canSubmit = validation.errors.length === 0 && !!activeToken && !loading;

  useEffect(() => {
    if (!market) return;

    const prefs = loadTradePreferences();
    const fallback = getDefaultTradePreferences();

    setOutcome(defaultOutcome ?? prefs.lastOutcome ?? fallback.lastOutcome);
    setSide(defaultSide ?? prefs.lastSide ?? fallback.lastSide);
    setAmount(defaultAmount ?? prefs.lastAmount ?? fallback.lastAmount);
    setPresetMode(prefs.preferredPresetMode ?? fallback.preferredPresetMode);
    setHighNotionalThreshold(
      Number.isFinite(Number(prefs.highNotionalConfirmThreshold))
        ? Number(prefs.highNotionalConfirmThreshold)
        : fallback.highNotionalConfirmThreshold,
    );

    setResult(null);
    setError(null);
    setConfirmArmed(false);
    setBalanceError(null);
  }, [market, defaultAmount, defaultOutcome, defaultSide]);

  useEffect(() => {
    if (!market) return;
    const nextPrefs: Partial<TradePreferences> = {
      lastSide: side,
      lastOutcome: outcome,
      lastAmount: amount,
      preferredPresetMode: presetMode,
      highNotionalConfirmThreshold: highNotionalThreshold,
    };
    saveTradePreferences(nextPrefs);
  }, [amount, highNotionalThreshold, market, outcome, presetMode, side]);

  useEffect(() => {
    if (!market) return;

    let cancelled = false;
    setBalanceLoading(true);
    setBalanceError(null);

    portfolioService
      .getSummary()
      .then((summary) => {
        if (!cancelled) setPortfolioSummary(summary);
      })
      .catch((err: unknown) => {
        if (!cancelled) {
          setBalanceError(getApiErrorMessage(err, "Failed to load available balance"));
        }
      })
      .finally(() => {
        if (!cancelled) setBalanceLoading(false);
      });

    return () => {
      cancelled = true;
    };
  }, [market]);

  useEffect(() => {
    if (!market) return;
    setQuoteNowTs(Date.now());
    const id = window.setInterval(() => setQuoteNowTs(Date.now()), 1000);
    return () => window.clearInterval(id);
  }, [market]);

  useEffect(() => {
    setResult(null);
    setError(null);
    setConfirmArmed(false);
  }, [outcome, side, amount, highNotionalThreshold]);

  const applyPreset = useCallback(
    (index: number) => {
      if (presetMode === "fixed") {
        const preset = fixedPresets[index];
        if (preset != null) {
          setAmount(String(preset));
        }
        return;
      }

      const percent = PERCENT_PRESETS[index];
      if (percent == null) return;

      const base = side === "BUY" ? usdcBalance : availableShares;
      if (base == null || !Number.isFinite(base) || base <= 0) return;

      const next = (base * percent) / 100;
      const decimals = side === "BUY" ? 2 : 4;
      setAmount(fmtNumber(next, decimals));
    },
    [availableShares, fixedPresets, presetMode, side, usdcBalance],
  );

  const setMaxAmount = useCallback(() => {
    const maxValue = side === "BUY" ? usdcBalance : availableShares;
    if (maxValue == null || !Number.isFinite(maxValue) || maxValue <= 0) return;
    const decimals = side === "BUY" ? 2 : 4;
    setAmount(fmtNumber(maxValue, decimals));
  }, [availableShares, side, usdcBalance]);

  const handleSubmit = useCallback(async () => {
    if (!canSubmit || !activeToken || !market) return;

    if (validation.requiresConfirm && !confirmArmed) {
      setConfirmArmed(true);
      return;
    }

    setLoading(true);
    setError(null);
    setResult(null);

    try {
      const req: ExecuteTradeRequest = {
        token_id: activeToken.token_id,
        market_id: market.market_id,
        market_title: market.title,
        side,
        price: activePrice,
        size: amountNum,
        outcome: activeToken.outcome,
      };

      const resp = await tradesService.executeTrade(req);
      setResult(resp);

      if (resp.success) {
        pushRecentTradeMarket({
          market_id: market.market_id,
          title: market.title,
          image: market.image,
          watch_key: market.watch_key,
          tokens: market.tokens,
          bestAsk: market.bestAsk,
          bestBid: market.bestBid,
          liquidity: market.liquidity,
          quote_timestamp: market.quote_timestamp,
          source,
        });
        onSuccess?.(resp);
      } else if (resp.error) {
        setError(resp.error);
      }
    } catch (err: unknown) {
      setError(getApiErrorMessage(err, "Trade execution failed"));
    } finally {
      setLoading(false);
    }
  }, [
    activePrice,
    activeToken,
    amountNum,
    canSubmit,
    confirmArmed,
    market,
    onSuccess,
    side,
    source,
    validation.requiresConfirm,
  ]);

  const handleReverseSide = () => {
    const prevSide = side;
    const sameNotional = prevSide === "BUY" ? amountNum : amountNum * activePrice;
    const nextSide: TradeTicketSide = prevSide === "BUY" ? "SELL" : "BUY";

    setSide(nextSide);

    if (nextSide === "BUY") {
      setAmount(fmtNumber(sameNotional, 2));
    } else {
      const sellShares = activePrice > 0 ? sameNotional / activePrice : 0;
      setAmount(fmtNumber(sellShares, 4));
    }

    setResult(null);
    setError(null);
    setConfirmArmed(false);
  };

  useEffect(() => {
    if (!market) return;

    const onKeyDown = (event: KeyboardEvent) => {
      if (event.defaultPrevented) return;
      if (event.metaKey || event.ctrlKey || event.altKey) return;

      if (event.key === "Escape") {
        event.preventDefault();
        onClose();
        return;
      }

      const editable = isEditableTarget(event.target);
      if (editable) return;

      const key = event.key.toLowerCase();
      if (key === "y") {
        event.preventDefault();
        setOutcome("Yes");
        return;
      }
      if (key === "n") {
        event.preventDefault();
        setOutcome("No");
        return;
      }
      if (key === "b") {
        event.preventDefault();
        setSide("BUY");
        return;
      }
      if (key === "s") {
        event.preventDefault();
        setSide("SELL");
        return;
      }
      if (key === "enter") {
        event.preventDefault();
        void handleSubmit();
        return;
      }

      if (["1", "2", "3", "4"].includes(key)) {
        event.preventDefault();
        applyPreset(Number(key) - 1);
      }
    };

    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [applyPreset, handleSubmit, market, onClose]);

  if (!market) return null;

  const yesPrice = Number(yesToken?.price ?? 0.5);
  const noPrice = Number(noToken?.price ?? 0.5);
  const isStale = quoteAgeSec > 15;

  return createPortal(
    <div
      className="fixed inset-0 bg-black/70 flex items-center justify-center z-50 p-4"
      onClick={onClose}
    >
      <div
        className="surface-panel w-full max-w-lg max-h-[90vh] overflow-hidden rounded-xl flex flex-col"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="p-5 border-b border-[var(--line)]">
          <div className="flex justify-between items-start gap-3">
            <div className="min-w-0">
              <h2 className="text-base font-bold text-white leading-snug truncate">
                {market.title}
              </h2>
              <p className="text-xs text-muted mt-1 uppercase tracking-wide">
                Fast Trade Ticket · {source}
              </p>
            </div>
            <button
              onClick={onClose}
              className="text-muted hover:text-white transition text-xl leading-none shrink-0"
            >
              x
            </button>
          </div>
        </div>

        <div className="flex-1 min-h-0 overflow-y-auto">
        <div className="flex border-b border-[var(--line)]">
          <button
            onClick={() => setOutcome("Yes")}
            className={`flex-1 py-3 text-sm font-semibold transition relative ${
              outcome === "Yes" ? "text-emerald-400" : "text-muted hover:text-white"
            }`}
          >
            <span className="flex items-center justify-center gap-2">
              Yes
              <span
                className={`text-xs font-mono px-1.5 py-0.5 rounded ${
                  outcome === "Yes"
                    ? "bg-emerald-500/20 text-emerald-400"
                    : "bg-[var(--bg-soft)] text-muted"
                }`}
              >
                {pct(yesPrice)}
              </span>
            </span>
            {outcome === "Yes" && (
              <div className="absolute bottom-0 left-0 right-0 h-0.5 bg-emerald-400" />
            )}
          </button>
          <button
            onClick={() => setOutcome("No")}
            className={`flex-1 py-3 text-sm font-semibold transition relative ${
              outcome === "No" ? "text-red-400" : "text-muted hover:text-white"
            }`}
          >
            <span className="flex items-center justify-center gap-2">
              No
              <span
                className={`text-xs font-mono px-1.5 py-0.5 rounded ${
                  outcome === "No"
                    ? "bg-red-500/20 text-red-400"
                    : "bg-[var(--bg-soft)] text-muted"
                }`}
              >
                {pct(noPrice)}
              </span>
            </span>
            {outcome === "No" && (
              <div className="absolute bottom-0 left-0 right-0 h-0.5 bg-red-400" />
            )}
          </button>
        </div>

        <div className="px-5 pt-4">
          <div className="flex gap-2 p-1 rounded-lg bg-[var(--bg-soft)]">
            <button
              onClick={() => setSide("BUY")}
              className={`flex-1 py-2 rounded-md text-sm font-semibold transition ${
                side === "BUY"
                  ? "bg-emerald-500/20 text-emerald-400 shadow-sm"
                  : "text-muted hover:text-white"
              }`}
            >
              Buy (B)
            </button>
            <button
              onClick={() => setSide("SELL")}
              className={`flex-1 py-2 rounded-md text-sm font-semibold transition ${
                side === "SELL"
                  ? "bg-red-500/20 text-red-400 shadow-sm"
                  : "text-muted hover:text-white"
              }`}
            >
              Sell (S)
            </button>
          </div>
        </div>

        <div className="p-5 space-y-4">
          <div>
            <div className="flex items-center justify-between mb-1.5">
              <label className="block text-sm text-soft">
                {side === "BUY" ? "Amount (USDC)" : "Shares to Sell"}
              </label>
              <button
                onClick={setMaxAmount}
                className="text-xs text-[var(--accent)] hover:underline"
                type="button"
              >
                Max
              </button>
            </div>
            <div className="relative">
              <span className="absolute left-3 top-1/2 -translate-y-1/2 text-muted text-sm">
                {side === "BUY" ? "$" : "#"}
              </span>
              <input
                type="text"
                value={amount}
                onChange={(e) => setAmount(e.target.value)}
                placeholder={side === "BUY" ? "10.00" : "100"}
                className="w-full pl-7 pr-4 py-2.5 rounded-lg bg-[var(--bg-soft)] border border-[var(--line)] text-white placeholder-[var(--text-muted)] focus:outline-none focus:border-[var(--accent)] transition mono"
              />
            </div>

            <div className="flex items-center gap-2 mt-2">
              <button
                type="button"
                onClick={() => setPresetMode("fixed")}
                className={`text-xs px-2.5 py-1 rounded border ${
                  presetMode === "fixed"
                    ? "border-[var(--accent)] text-[var(--accent)] bg-[var(--accent-soft)]"
                    : "border-[var(--line)] text-soft"
                }`}
              >
                Fixed
              </button>
              <button
                type="button"
                onClick={() => setPresetMode("percentage")}
                className={`text-xs px-2.5 py-1 rounded border ${
                  presetMode === "percentage"
                    ? "border-[var(--accent)] text-[var(--accent)] bg-[var(--accent-soft)]"
                    : "border-[var(--line)] text-soft"
                }`}
              >
                % Balance
              </button>
            </div>

            <div className="grid grid-cols-4 gap-2 mt-2">
              {(presetMode === "fixed" ? fixedPresets : PERCENT_PRESETS).map(
                (value, index) => (
                  <button
                    key={`${presetMode}-${value}`}
                    onClick={() => applyPreset(index)}
                    className="py-1 text-xs rounded bg-[var(--bg-soft)] text-muted hover:text-white hover:bg-[var(--bg-card)] border border-[var(--line)] transition"
                    type="button"
                  >
                    {presetMode === "fixed"
                      ? side === "BUY"
                        ? `$${value}`
                        : `${value}`
                      : `${value}%`}
                  </button>
                ),
              )}
            </div>

            <div className="flex justify-between mt-2 text-xs text-muted">
              <span>
                {side === "BUY"
                  ? `USDC available: ${
                      usdcBalance != null ? fmt$(usdcBalance) : balanceLoading ? "Loading..." : "N/A"
                    }`
                  : `Shares available: ${
                      availableShares != null
                        ? `${availableShares.toFixed(4)} ${outcome}`
                        : balanceLoading
                          ? "Loading..."
                          : "N/A"
                    }`}
              </span>
              {side === "BUY" && (
                <span>Minimum buy: {fmt$(MIN_BUY_USDC)}</span>
              )}
            </div>
          </div>

          <div className="surface-soft rounded-lg p-3 text-xs space-y-1.5">
            <div className="flex justify-between text-soft">
              <span>Quote age</span>
              <span className={isStale ? "text-yellow-300" : "text-emerald-300"}>
                {quoteAgeSec}s {isStale ? "stale" : "fresh"}
              </span>
            </div>
            <div className="flex justify-between text-soft">
              <span>Spread proxy</span>
              <span>{spread != null ? `${(spread * 100).toFixed(2)}c` : "N/A"}</span>
            </div>
            <div className="flex justify-between text-soft">
              <span>Liquidity</span>
              <span>
                {liquidity != null ? fmt$(liquidity) : "N/A"}
                {liquidity != null && liquidity < LOW_LIQUIDITY_THRESHOLD ? " (low)" : ""}
              </span>
            </div>
          </div>

          {amountNum > 0 && Number.isFinite(amountNum) && (
            <div className="surface-soft p-3.5 rounded-lg space-y-2 text-sm">
              {side === "BUY" ? (
                <>
                  <div className="flex justify-between">
                    <span className="text-muted">You pay</span>
                    <span className="text-white mono">{fmt$(notional)}</span>
                  </div>
                  <div className="flex justify-between">
                    <span className="text-muted">Est. shares</span>
                    <span className="text-white mono">
                      {shares.toFixed(4)} {outcome}
                    </span>
                  </div>
                  <div className="flex justify-between">
                    <span className="text-muted">Payout if correct</span>
                    <span className="text-white mono">{fmt$(potentialPayout)}</span>
                  </div>
                  <div className="flex justify-between">
                    <span className="text-muted">Max loss</span>
                    <span className="text-red-300 mono">{fmt$(notional)}</span>
                  </div>
                  <div className="border-t border-[var(--line)] pt-2 flex justify-between font-semibold">
                    <span className="text-muted">Breakeven framing</span>
                    <span className="text-soft mono">Need &gt; {(activePrice * 100).toFixed(1)}%</span>
                  </div>
                  <div className="flex justify-between">
                    <span className="text-muted">Potential profit</span>
                    <span className="status-good mono">+{fmt$(potentialProfit)}</span>
                  </div>
                </>
              ) : (
                <>
                  <div className="flex justify-between">
                    <span className="text-muted">Shares to sell</span>
                    <span className="text-white mono">
                      {amountNum.toFixed(4)} {outcome}
                    </span>
                  </div>
                  <div className="flex justify-between">
                    <span className="text-muted">Estimated proceeds</span>
                    <span className="text-white mono">{fmt$(notional)}</span>
                  </div>
                  <div className="flex justify-between">
                    <span className="text-muted">Upside given up if {outcome}</span>
                    <span className="text-yellow-300 mono">
                      {fmt$(Math.max(0, shares * (1 - activePrice)))}
                    </span>
                  </div>
                  <div className="border-t border-[var(--line)] pt-2 flex justify-between font-semibold">
                    <span className="text-muted">Breakeven framing</span>
                    <span className="text-soft mono">Selling near {(activePrice * 100).toFixed(1)}%</span>
                  </div>
                </>
              )}
            </div>
          )}

          {balanceError && (
            <div className="p-2.5 rounded-lg bg-yellow-500/10 border border-yellow-500/30 text-yellow-200 text-xs">
              {balanceError}
            </div>
          )}

          {validation.errors.length > 0 && (
            <div className="p-3 rounded-lg bg-red-500/10 border border-red-500/30 text-red-300 text-xs space-y-1">
              {validation.errors.map((row) => (
                <p key={row}>{row}</p>
              ))}
            </div>
          )}

          {validation.warnings.length > 0 && (
            <div className="p-3 rounded-lg bg-yellow-500/10 border border-yellow-500/30 text-yellow-200 text-xs space-y-1">
              {validation.warnings.map((row) => (
                <p key={row}>{row}</p>
              ))}
            </div>
          )}

          {error && (
            <div className="p-3 rounded-lg bg-red-500/10 border border-red-500/30 text-red-400 text-sm">
              {error}
            </div>
          )}

          {result?.success && (
            <div className="p-3 rounded-lg bg-emerald-500/10 border border-emerald-500/30 text-emerald-400 text-sm">
              <p className="font-semibold">
                {side === "BUY" ? "Bought" : "Sold"} {outcome} shares.
              </p>
              <p className="text-xs mt-1 mono">
                {result.side} {result.side === "BUY" ? `${fmt$(result.size ?? amountNum)}` : `${result.size ?? amountNum} shares`} @ {pct(result.price)}
              </p>
              {result.order_hash && (
                <p className="text-xs mt-1 text-muted truncate">Hash: {result.order_hash}</p>
              )}
            </div>
          )}

          <div className="flex items-center gap-2 text-xs">
            <span className="text-muted">High-notional confirm at</span>
            <input
              type="number"
              min={1}
              step={1}
              value={highNotionalThreshold}
              onChange={(e) => {
                const next = Number(e.target.value);
                setHighNotionalThreshold(Number.isFinite(next) && next >= 1 ? next : 1);
              }}
              className="w-24 px-2 py-1 rounded bg-[var(--bg-soft)] border border-[var(--line)] text-white"
            />
            <span className="text-muted">USDC</span>
          </div>

          {!result?.success ? (
            <button
              onClick={() => void handleSubmit()}
              disabled={!canSubmit}
              className={`w-full py-3 rounded-lg font-bold text-sm transition ${
                side === "BUY" ? "btn-success" : "btn-danger"
              }`}
            >
              {loading ? (
                <span className="flex items-center justify-center gap-2">
                  <svg className="animate-spin h-4 w-4" viewBox="0 0 24 24" fill="none">
                    <circle
                      cx="12"
                      cy="12"
                      r="10"
                      stroke="currentColor"
                      strokeWidth="3"
                      className="opacity-30"
                    />
                    <path
                      d="M4 12a8 8 0 018-8"
                      stroke="currentColor"
                      strokeWidth="3"
                      strokeLinecap="round"
                    />
                  </svg>
                  Placing order...
                </span>
              ) : validation.requiresConfirm && !confirmArmed ? (
                `Confirm large ${side.toLowerCase()} (${fmt$(notional)})`
              ) : (
                `${side === "BUY" ? "Buy" : "Sell"} ${outcome}${
                  amountNum > 0
                    ? side === "BUY"
                      ? ` - ${fmt$(amountNum)}`
                      : ` - ${amountNum.toFixed(4)} shares`
                    : ""
                }`
              )}
            </button>
          ) : (
            <div className="grid grid-cols-2 gap-2">
              <button
                onClick={handleReverseSide}
                className="py-3 rounded-lg font-bold text-sm btn-muted"
                type="button"
              >
                Reverse Side
              </button>
              <button
                onClick={onClose}
                className="py-3 rounded-lg font-bold text-sm btn-muted"
                type="button"
              >
                Done
              </button>
            </div>
          )}

          <p className="text-[11px] text-muted">
            Shortcuts: Y/N outcome, B/S side, 1-4 presets, Enter submit, Esc close.
          </p>
        </div>
        </div>
      </div>
    </div>,
    document.body,
  );
}
