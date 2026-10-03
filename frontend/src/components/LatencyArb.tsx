/**
 * Latency-arbitrage panel: live edge board, engine
 * configuration and trade log.
 *
 * The board lists BTC/ETH/SOL 5m/15m/1h up/down
 * mispricings — model probability (Bachelier on the
 * Binance feed) vs the market's current price — with
 * the window countdown and the Binance-feed→order
 * latency distribution. The engine is paper-first:
 * with simulation mode on, fills are simulated and
 * no real orders are ever submitted.
 *
 * Polls every 5 seconds (the engine cycle interval).
 *
 * @module components/LatencyArb
 */
import { useCallback, useEffect, useMemo, useState } from "react";
import { getApiErrorMessage } from "../utils/apiError";
import {
  LatencyArbConfig,
  LatencyArbOpportunity,
  LatencyArbTrade,
  latencyArbService,
} from "../services/latencyArbService";
import { useTradingKeyStatus } from "../hooks/useTradingKeyStatus";
import TradingKeyPrompt from "./TradingKeyPrompt";

const POLL_INTERVAL_MS = 5000;
const DEFAULT_EDGE_THRESHOLD = 0.03;
const DEFAULT_MAX_NOTIONAL = 50;
const DEFAULT_DAILY_LOSS_LIMIT = 20;

const SYMBOL_OPTIONS = ["BTC", "ETH", "SOL"];
const WINDOW_OPTIONS = [5, 15, 60];

const STATUS_LABELS: Record<string, string> = {
  simulated: "Simulated",
  pending: "Pending",
  executed: "Executed",
  filled: "Filled",
  rejected: "Rejected",
  aborted: "Aborted",
  failed: "Failed",
};

function pct(value: number, digits = 1): string {
  return `${(value * 100).toFixed(digits)}%`;
}

function usd(value: number | null | undefined): string {
  if (value == null) return "—";
  return `$${value.toFixed(2)}`;
}

function signedUsd(value: number | null | undefined): string {
  if (value == null) return "—";
  const sign = value < 0 ? "−" : "+";
  return `${sign}$${Math.abs(value).toFixed(2)}`;
}

function countdown(secondsRemaining: number): string {
  const seconds = Math.max(0, Math.floor(secondsRemaining));
  const minutes = Math.floor(seconds / 60);
  const leftover = seconds % 60;
  return `${minutes}:${String(leftover).padStart(2, "0")}`;
}

function age(detectedAt: string): string {
  const seconds = Math.max(0, (Date.now() - new Date(detectedAt).getTime()) / 1000);
  if (seconds < 60) return `${Math.floor(seconds)}s ago`;
  return `${Math.floor(seconds / 60)}m ago`;
}

function ms(value: number | null | undefined): string {
  if (value == null) return "—";
  return `${value.toFixed(0)}ms`;
}

export default function LatencyArb() {
  const { hasTradingKey } = useTradingKeyStatus();
  const [error, setError] = useState<string | null>(null);
  const [success, setSuccess] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [tradingKeyPrompt, setTradingKeyPrompt] = useState(false);

  // ── Board state ─────────────────────────────────────
  const [opportunities, setOpportunities] = useState<LatencyArbOpportunity[]>([]);
  const [engine, setEngine] = useState<{
    running: boolean;
    live_mode: boolean;
    last_cycle_at: string | null;
    cycle_seconds: number;
  } | null>(null);
  const [latency, setLatency] = useState<{
    samples: number;
    feed_lag_p50_ms: number;
    feed_lag_p95_ms: number;
    total_p50_ms: number;
    total_p95_ms: number;
  } | null>(null);

  // ── Config state ────────────────────────────────────
  const [config, setConfig] = useState<LatencyArbConfig>({
    enabled: false,
    edge_threshold: DEFAULT_EDGE_THRESHOLD,
    max_notional: DEFAULT_MAX_NOTIONAL,
    symbols: [...SYMBOL_OPTIONS],
    windows: [...WINDOW_OPTIONS],
    late_entry: false,
    daily_loss_limit: DEFAULT_DAILY_LOSS_LIMIT,
    alert_on_opportunity: true,
  });

  // ── Trade log state ─────────────────────────────────
  const [trades, setTrades] = useState<LatencyArbTrade[]>([]);
  const [totalTrades, setTotalTrades] = useState(0);
  const [tradeOffset, setTradeOffset] = useState(0);
  const TRADE_LIMIT = 20;

  const fetchBoard = useCallback(async () => {
    try {
      const result = await latencyArbService.getOpportunities();
      setOpportunities(result.opportunities);
      setEngine(result.engine);
      setLatency(result.latency);
      setError(null);
    } catch (err) {
      setError(getApiErrorMessage(err));
    } finally {
      setLoading(false);
    }
  }, []);

  const fetchConfig = useCallback(async () => {
    try {
      const result = await latencyArbService.getConfig();
      setConfig(result);
    } catch (err) {
      setError(getApiErrorMessage(err));
    }
  }, []);

  const fetchTrades = useCallback(async (offset: number) => {
    try {
      const result = await latencyArbService.getTrades({
        limit: TRADE_LIMIT,
        offset,
      });
      setTrades(result.trades);
      setTotalTrades(result.total);
    } catch (err) {
      setError(getApiErrorMessage(err));
    }
  }, []);

  useEffect(() => {
    fetchConfig().finally(() => {
      fetchBoard();
      fetchTrades(0);
    });
  }, [fetchConfig, fetchBoard, fetchTrades]);

  useEffect(() => {
    const timer = setInterval(fetchBoard, POLL_INTERVAL_MS);
    return () => clearInterval(timer);
  }, [fetchBoard]);

  const saveConfig = async () => {
    if (config.enabled && hasTradingKey === false) {
      setTradingKeyPrompt(true);
      return;
    }
    setSaving(true);
    setSuccess(null);
    setError(null);
    try {
      const result = await latencyArbService.updateConfig({
        enabled: config.enabled,
        edge_threshold: config.edge_threshold,
        max_notional: config.max_notional,
        symbols: config.symbols,
        windows: config.windows,
        late_entry: config.late_entry,
        daily_loss_limit: config.daily_loss_limit,
        alert_on_opportunity: config.alert_on_opportunity,
      });
      setConfig(result);
      setSuccess("Latency-arb settings saved");
    } catch (err) {
      setError(getApiErrorMessage(err));
    } finally {
      setSaving(false);
    }
  };

  const toggleSymbol = (symbol: string) => {
    setConfig((prev) => ({
      ...prev,
      symbols: prev.symbols.includes(symbol)
        ? prev.symbols.filter((item) => item !== symbol)
        : [...prev.symbols, symbol],
    }));
  };

  const toggleWindow = (window: number) => {
    setConfig((prev) => ({
      ...prev,
      windows: prev.windows.includes(window)
        ? prev.windows.filter((item) => item !== window)
        : [...prev.windows, window],
    }));
  };

  const totalPages = useMemo(
    () => Math.max(1, Math.ceil(totalTrades / TRADE_LIMIT)),
    [totalTrades],
  );
  const currentPage = Math.min(Math.floor(tradeOffset / TRADE_LIMIT) + 1, totalPages);

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-3xl font-bold">Latency Arbitrage</h1>
        <p className="text-soft text-sm mt-1">
          BTC/ETH/SOL 5m/15m/1h up-down mispricings detected from the Binance feed before Polymarket
          odds adjust. Paper-first: simulated fills never touch the exchange.
        </p>
      </div>

      {error && (
        <div className="rounded border border-red-500/40 bg-red-500/10 p-3 text-sm text-red-400">
          {error}
        </div>
      )}
      {success && (
        <div className="rounded border border-green-500/40 bg-green-500/10 p-3 text-sm text-green-400">
          {success}
        </div>
      )}

      {tradingKeyPrompt && <TradingKeyPrompt strategy="Latency arbitrage" />}

      <section className="surface-panel p-5 space-y-4">
        <div className="flex items-center justify-between gap-3 flex-wrap">
          <h2 className="text-lg font-semibold">
            Live Edge Board{" "}
            <span className="text-sm font-normal text-soft">
              ({opportunities.length} opportunities)
            </span>
          </h2>
          <div className="flex items-center gap-2 text-xs text-soft">
            <span
              className={
                "px-2 py-0.5 rounded " +
                (engine?.running ? "bg-green-500/10 text-green-400" : "bg-red-500/10 text-red-400")
              }
            >
              {engine?.running ? "Engine running" : "Engine stopped"}
            </span>
            <span
              className={
                "px-2 py-0.5 rounded " +
                (engine?.live_mode
                  ? "bg-amber-500/10 text-amber-400"
                  : "bg-[var(--bg-soft)] text-soft")
              }
            >
              {engine?.live_mode ? "LIVE" : "PAPER"}
            </span>
            <button onClick={() => fetchBoard()} className="btn-muted text-xs px-3 py-1.5">
              Refresh
            </button>
          </div>
        </div>

        {latency && latency.samples > 0 && (
          <div className="rounded border border-[var(--line)] bg-[var(--bg-soft)] p-3 text-xs text-soft">
            Binance-feed→order latency (n={latency.samples}): feed lag p50{" "}
            {ms(latency.feed_lag_p50_ms)} / p95 {ms(latency.feed_lag_p95_ms)} · total p50{" "}
            {ms(latency.total_p50_ms)} / p95 {ms(latency.total_p95_ms)}. The edge only exists when
            Polymarket's lag dominates.
          </div>
        )}

        {loading ? (
          <div className="space-y-3">
            {[1, 2, 3].map((i) => (
              <div key={i} className="surface-panel p-4 animate-pulse h-14" />
            ))}
          </div>
        ) : opportunities.length === 0 ? (
          <p className="text-soft text-sm py-8 text-center">
            No latency-arb opportunities above the edge threshold right now. The board refreshes
            every 5 seconds with the engine cycle.
          </p>
        ) : (
          <div className="overflow-x-auto">
            <table className="table-theme text-sm w-full">
              <thead>
                <tr className="text-left text-xs text-muted/60">
                  <th className="pb-2 pr-4">Symbol</th>
                  <th className="pb-2 pr-4">Window</th>
                  <th className="pb-2 pr-4">Side</th>
                  <th className="pb-2 pr-4 text-right">Model P</th>
                  <th className="pb-2 pr-4 text-right">Market P</th>
                  <th className="pb-2 pr-4 text-right">Edge</th>
                  <th className="pb-2 pr-4 text-right">Price move</th>
                  <th className="pb-2 pr-4 text-right">Feed lag</th>
                  <th className="pb-2 text-right">Countdown</th>
                </tr>
              </thead>
              <tbody>
                {opportunities.map((opportunity, index) => (
                  <tr
                    key={`${opportunity.condition_id}-${index}`}
                    className="border-t border-[var(--line)] transition"
                  >
                    <td className="py-2.5 pr-4 font-semibold">{opportunity.symbol}</td>
                    <td className="py-2.5 pr-4">{opportunity.window_minutes}m</td>
                    <td className="py-2.5 pr-4">
                      <span
                        className={
                          "text-xs font-semibold px-2 py-0.5 rounded " +
                          (opportunity.side === "up"
                            ? "bg-green-500/10 text-green-400"
                            : "bg-red-500/10 text-red-400")
                        }
                      >
                        {opportunity.side === "up" ? "UP" : "DOWN"}
                      </span>
                    </td>
                    <td className="py-2.5 pr-4 text-right font-mono text-xs">
                      {pct(opportunity.p_model)}
                    </td>
                    <td className="py-2.5 pr-4 text-right font-mono text-xs">
                      {pct(opportunity.p_market)}
                    </td>
                    <td className="py-2.5 pr-4 text-right font-mono text-xs font-semibold text-amber-400">
                      {pct(opportunity.edge)}
                    </td>
                    <td className="py-2.5 pr-4 text-right font-mono text-xs">
                      {(opportunity.distance * 100).toFixed(2)}%
                    </td>
                    <td className="py-2.5 pr-4 text-right font-mono text-xs">
                      {ms(opportunity.feed_lag_ms)}
                    </td>
                    <td className="py-2.5 text-right font-mono text-xs">
                      {countdown(opportunity.seconds_remaining)}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
        {engine?.last_cycle_at && (
          <p className="text-xs text-soft">Last engine cycle: {age(engine.last_cycle_at)}</p>
        )}
      </section>

      <section className="surface-panel p-5 space-y-4">
        <div className="flex items-center justify-between gap-3 flex-wrap">
          <h2 className="text-lg font-semibold">Engine Settings</h2>
          <button onClick={saveConfig} disabled={saving} className="btn-muted text-xs px-3 py-1.5">
            {saving ? "Saving…" : "Save Settings"}
          </button>
        </div>
        <div className="grid gap-3 md:grid-cols-3">
          <div className="rounded border border-[var(--line)] bg-[var(--bg-soft)] p-3">
            <p className="text-xs font-semibold mb-1">Engine enabled</p>
            <label className="flex items-center gap-2 text-sm py-1">
              <input
                type="checkbox"
                checked={config.enabled}
                onChange={(e) => {
                  if (e.target.checked && hasTradingKey === false) {
                    setTradingKeyPrompt(true);
                    return;
                  }
                  setTradingKeyPrompt(false);
                  setConfig({ ...config, enabled: e.target.checked });
                }}
              />
              Trade eligible opportunities
            </label>
            <p className="text-xs text-soft mt-1">
              Off by default. Paper mode fills are simulated; live orders require the
              LATENCY_ARB_LIVE server flag and a connected trading key.
            </p>
          </div>
          <div className="rounded border border-[var(--line)] bg-[var(--bg-soft)] p-3">
            <p className="text-xs font-semibold mb-1">Edge threshold</p>
            <input
              type="number"
              min={0.01}
              max={0.5}
              step={0.01}
              value={config.edge_threshold}
              onChange={(e) =>
                setConfig({
                  ...config,
                  edge_threshold: Number(e.target.value),
                })
              }
              className="w-full bg-transparent text-sm border-b border-[var(--line)] focus:border-[var(--accent)] outline-none py-1"
            />
            <p className="text-xs text-soft mt-1">
              Minimum |model − market| probability gap (0.03 = 3%).
            </p>
          </div>
          <div className="rounded border border-[var(--line)] bg-[var(--bg-soft)] p-3">
            <p className="text-xs font-semibold mb-1">Max notional (USDC)</p>
            <input
              type="number"
              min={1}
              value={config.max_notional}
              onChange={(e) =>
                setConfig({
                  ...config,
                  max_notional: Number(e.target.value),
                })
              }
              className="w-full bg-transparent text-sm border-b border-[var(--line)] focus:border-[var(--accent)] outline-none py-1"
            />
            <p className="text-xs text-soft mt-1">
              Per-trade cap, also bounded by the server-wide maximum.
            </p>
          </div>
          <div className="rounded border border-[var(--line)] bg-[var(--bg-soft)] p-3">
            <p className="text-xs font-semibold mb-1">Daily loss limit (USDC)</p>
            <input
              type="number"
              min={0}
              value={config.daily_loss_limit}
              onChange={(e) =>
                setConfig({
                  ...config,
                  daily_loss_limit: Number(e.target.value),
                })
              }
              className="w-full bg-transparent text-sm border-b border-[var(--line)] focus:border-[var(--accent)] outline-none py-1"
            />
            <p className="text-xs text-soft mt-1">
              Per-strategy daily loss limit; trading pauses when hit.
            </p>
          </div>
          <div className="rounded border border-[var(--line)] bg-[var(--bg-soft)] p-3">
            <p className="text-xs font-semibold mb-1">Symbols</p>
            <div className="flex flex-wrap gap-2 py-1">
              {SYMBOL_OPTIONS.map((symbol) => (
                <button
                  key={symbol}
                  type="button"
                  onClick={() => toggleSymbol(symbol)}
                  className={
                    "text-xs px-2 py-1 rounded border " +
                    (config.symbols.includes(symbol)
                      ? "border-[var(--accent)] bg-[var(--accent-soft)] text-[var(--accent)]"
                      : "border-[var(--line)] text-soft")
                  }
                >
                  {symbol}
                </button>
              ))}
            </div>
            <p className="text-xs text-soft mt-1">Crypto symbols the engine evaluates.</p>
          </div>
          <div className="rounded border border-[var(--line)] bg-[var(--bg-soft)] p-3">
            <p className="text-xs font-semibold mb-1">Windows</p>
            <div className="flex flex-wrap gap-2 py-1">
              {WINDOW_OPTIONS.map((window) => (
                <button
                  key={window}
                  type="button"
                  onClick={() => toggleWindow(window)}
                  className={
                    "text-xs px-2 py-1 rounded border " +
                    (config.windows.includes(window)
                      ? "border-[var(--accent)] bg-[var(--accent-soft)] text-[var(--accent)]"
                      : "border-[var(--line)] text-soft")
                  }
                >
                  {window}m
                </button>
              ))}
            </div>
            <p className="text-xs text-soft mt-1">Up/down window sizes the engine evaluates.</p>
          </div>
          <div className="rounded border border-[var(--line)] bg-[var(--bg-soft)] p-3">
            <p className="text-xs font-semibold mb-1">Late entry</p>
            <label className="flex items-center gap-2 text-sm py-1">
              <input
                type="checkbox"
                checked={config.late_entry}
                onChange={(e) => setConfig({ ...config, late_entry: e.target.checked })}
              />
              Allow trading the final 10s
            </label>
            <p className="text-xs text-soft mt-1">
              Off by default — the engine halts in the last 10 seconds of each window.
            </p>
          </div>
          <div className="rounded border border-[var(--line)] bg-[var(--bg-soft)] p-3">
            <p className="text-xs font-semibold mb-1">Opportunity alerts</p>
            <label className="flex items-center gap-2 text-sm py-1">
              <input
                type="checkbox"
                checked={config.alert_on_opportunity}
                onChange={(e) =>
                  setConfig({
                    ...config,
                    alert_on_opportunity: e.target.checked,
                  })
                }
              />
              Alert on executed opportunities
            </label>
            <p className="text-xs text-soft mt-1">
              Sends a latency_arb_opportunity notification per fill.
            </p>
          </div>
        </div>
      </section>

      <section className="surface-panel p-5 space-y-3">
        <div className="flex items-center justify-between gap-3 flex-wrap">
          <h2 className="text-lg font-semibold">
            Trade Log{" "}
            <span className="text-sm font-normal text-soft">
              ({totalTrades.toLocaleString()} trades)
            </span>
          </h2>
          <div className="flex items-center gap-2">
            <button
              onClick={() => setTradeOffset(Math.max(0, tradeOffset - TRADE_LIMIT))}
              disabled={tradeOffset === 0}
              className="btn-muted text-xs px-3 py-1.5"
            >
              Previous
            </button>
            <span className="text-xs text-soft">
              Page {currentPage} / {totalPages}
            </span>
            <button
              onClick={() =>
                setTradeOffset(Math.min((totalPages - 1) * TRADE_LIMIT, tradeOffset + TRADE_LIMIT))
              }
              disabled={currentPage >= totalPages}
              className="btn-muted text-xs px-3 py-1.5"
            >
              Next
            </button>
            <button
              onClick={() => {
                setTradeOffset(0);
                fetchTrades(0);
              }}
              className="btn-muted text-xs px-3 py-1.5"
            >
              Refresh
            </button>
          </div>
        </div>

        {trades.length === 0 ? (
          <p className="text-soft text-sm py-8 text-center">
            No latency-arb trades yet. Simulated fills appear here when the engine detects an
            eligible opportunity.
          </p>
        ) : (
          <div className="overflow-x-auto">
            <table className="table-theme text-sm w-full">
              <thead>
                <tr className="text-left text-xs text-muted/60">
                  <th className="pb-2 pr-4">Market</th>
                  <th className="pb-2 pr-4">Side</th>
                  <th className="pb-2 pr-4 text-right">Size</th>
                  <th className="pb-2 pr-4 text-right">Expected</th>
                  <th className="pb-2 pr-4 text-right">Filled</th>
                  <th className="pb-2 pr-4 text-right">Slippage</th>
                  <th className="pb-2 pr-4 text-right">PnL</th>
                  <th className="pb-2 pr-4">Status</th>
                  <th className="pb-2 text-right">Age</th>
                </tr>
              </thead>
              <tbody>
                {trades.map((trade) => (
                  <tr key={trade.id} className="border-t border-[var(--line)] transition">
                    <td className="py-2.5 pr-4 max-w-xs">
                      <p className="truncate font-mono text-xs">{trade.market_id}</p>
                    </td>
                    <td className="py-2.5 pr-4">
                      <span
                        className={
                          "text-xs font-semibold px-2 py-0.5 rounded " +
                          (trade.action === "buy"
                            ? "bg-green-500/10 text-green-400"
                            : "bg-red-500/10 text-red-400")
                        }
                      >
                        {trade.action.toUpperCase()}
                      </span>
                    </td>
                    <td className="py-2.5 pr-4 text-right font-mono text-xs">
                      {usd(trade.amount)}
                    </td>
                    <td className="py-2.5 pr-4 text-right font-mono text-xs">
                      {trade.expected_price == null ? "—" : pct(trade.expected_price)}
                    </td>
                    <td className="py-2.5 pr-4 text-right font-mono text-xs">
                      {trade.filled_price == null ? "—" : pct(trade.filled_price)}
                    </td>
                    <td className="py-2.5 pr-4 text-right font-mono text-xs">
                      {trade.slippage_bps == null ? "—" : `${trade.slippage_bps.toFixed(1)} bps`}
                    </td>
                    <td
                      className={
                        "py-2.5 pr-4 text-right font-mono text-xs " +
                        (trade.pnl != null && trade.pnl < 0 ? "text-red-400" : "text-green-400")
                      }
                    >
                      {signedUsd(trade.pnl)}
                    </td>
                    <td className="py-2.5 pr-4">
                      <span className="text-xs font-semibold px-2 py-0.5 rounded bg-[var(--bg-soft)] text-soft">
                        {STATUS_LABELS[trade.status] ?? trade.status}
                      </span>
                    </td>
                    <td className="py-2.5 text-right text-xs text-soft">
                      {trade.created_at ? age(trade.created_at) : "—"}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>
    </div>
  );
}
