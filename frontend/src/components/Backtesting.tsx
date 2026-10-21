/**
 * Backtesting workbench: submit a strategy run, then browse and inspect historical results.
 *
 * Loads recent runs and the available strategy catalogue via `backtestingService`, with a create
 * form whose parameter fields depend on the chosen strategy type (`indicator` or `copy_trade`).
 * Selecting a run shows PnL, win rate, Sharpe, drawdown, profit factor and the per-trade log;
 * runs can be deleted from the list.
 *
 * @module components/Backtesting
 */
import { useEffect, useState, useCallback } from "react";
import {
  backtestingService,
  BacktestRun,
  CreateBacktestRequest,
  StrategyInfo,
} from "../services/backtestingService";
import { getApiErrorMessage } from "../utils/apiError";
import { Banner, ConfirmModal, Skeleton } from "./ui";

type FormState = CreateBacktestRequest;

const defaultForm: FormState = {
  strategy_type: "indicator",
  strategy_name: "",
  start_date: "2024-06-01",
  end_date: "2025-01-01",
  parameters: {
    strategy: "rsi_mean_reversion",
    position_size: 10,
    rsi_oversold: 30,
    rsi_overbought: 70,
    condition_id: "",
    outcome: "Yes",
  },
};

export default function Backtesting() {
  const [runs, setRuns] = useState<BacktestRun[]>([]);
  const [strategies, setStrategies] = useState<StrategyInfo[]>([]);
  const [loading, setLoading] = useState(true);
  const [showForm, setShowForm] = useState(false);
  const [form, setForm] = useState<FormState>({ ...defaultForm });
  const [running, setRunning] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [selectedRun, setSelectedRun] = useState<BacktestRun | null>(null);
  const [pendingDelete, setPendingDelete] = useState<number | null>(null);

  const fetchData = useCallback(async () => {
    try {
      const [runsResp, strats] = await Promise.all([
        backtestingService.listBacktestRuns(50),
        backtestingService.listStrategies(),
      ]);
      setRuns(runsResp.runs);
      setStrategies(strats.strategies);
    } catch (e) {
      setError(getApiErrorMessage(e, "Failed to load backtesting data"));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    fetchData();
  }, [fetchData]);

  const handleRun = async () => {
    if (!form.start_date || !form.end_date) {
      setError("Start and end dates are required");
      return;
    }
    setRunning(true);
    setError(null);
    try {
      const result = await backtestingService.createBacktestRun(form);
      setShowForm(false);
      setSelectedRun(result);
      await fetchData();
    } catch (e) {
      setError(getApiErrorMessage(e, "Failed to start backtest run"));
    } finally {
      setRunning(false);
    }
  };

  const handleDelete = async (id: number) => {
    try {
      await backtestingService.deleteBacktestRun(id);
      if (selectedRun?.id === id) setSelectedRun(null);
      await fetchData();
    } catch (e) {
      setError(getApiErrorMessage(e, "Failed to delete backtest run"));
    }
  };

  const pnlColor = (pnl: number) =>
    pnl > 0 ? "text-green-400" : pnl < 0 ? "text-red-400" : "text-gray-400";

  const statusBadge = (status: string) => {
    const colors: Record<string, string> = {
      completed: "bg-green-500/20 text-green-400",
      running: "bg-blue-500/20 text-blue-400",
      pending: "bg-yellow-500/20 text-yellow-400",
      failed: "bg-red-500/20 text-red-400",
    };
    return colors[status] || "bg-gray-500/20 text-gray-400";
  };

  if (loading) {
    return (
      <div className="p-6 space-y-3" role="status" aria-live="polite">
        <Skeleton variant="text" lines={3} />
        <p className="text-soft text-sm">Loading backtesting data...</p>
      </div>
    );
  }

  return (
    <div className="p-6 space-y-6 max-w-6xl mx-auto">
      {/* Header */}
      <div className="flex items-center justify-between" data-tutorial="backtesting">
        <div>
          <h1 className="text-xl font-bold text-white">Backtesting</h1>
          <p className="text-soft text-sm mt-1">
            Test strategies against historical data before trading live
          </p>
        </div>
        <button onClick={() => setShowForm(!showForm)} className="btn-primary text-sm">
          {showForm ? "Cancel" : "+ New Backtest"}
        </button>
      </div>

      {error && (
        <Banner variant="error" onDismiss={() => setError(null)} onRetry={fetchData}>
          {error}
        </Banner>
      )}

      {/* Create Form */}
      {showForm && (
        <div className="surface-panel p-5 space-y-4">
          <h3 className="text-base font-semibold text-white">New Backtest</h3>

          <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
            <div>
              <label className="block text-xs text-soft mb-1">Strategy Type</label>
              <select
                value={form.strategy_type}
                onChange={(e) =>
                  setForm({
                    ...form,
                    strategy_type: e.target.value as FormState["strategy_type"],
                  })
                }
                className="input-field w-full"
              >
                <option value="copy_trade">Copy Trade Replay</option>
                <option value="indicator">Technical Indicator</option>
              </select>
            </div>
            <div>
              <label className="block text-xs text-soft mb-1">Strategy Name</label>
              <input
                type="text"
                value={form.strategy_name || ""}
                onChange={(e) => setForm({ ...form, strategy_name: e.target.value })}
                className="input-field w-full"
                placeholder="My Strategy"
              />
            </div>
            <div>
              <label className="block text-xs text-soft mb-1">Start Date</label>
              <input
                type="date"
                value={form.start_date}
                onChange={(e) => setForm({ ...form, start_date: e.target.value })}
                className="input-field w-full"
              />
            </div>
            <div>
              <label className="block text-xs text-soft mb-1">End Date</label>
              <input
                type="date"
                value={form.end_date}
                onChange={(e) => setForm({ ...form, end_date: e.target.value })}
                className="input-field w-full"
              />
            </div>
          </div>

          {form.strategy_type === "indicator" && (
            <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
              <div>
                <label className="block text-xs text-soft mb-1">Indicator Strategy</label>
                <select
                  value={(form.parameters.strategy as string) || "rsi_mean_reversion"}
                  onChange={(e) =>
                    setForm({
                      ...form,
                      parameters: {
                        ...form.parameters,
                        strategy: e.target.value,
                      },
                    })
                  }
                  className="input-field w-full"
                >
                  <option value="rsi_mean_reversion">RSI Mean Reversion</option>
                  <option value="macd_crossover">MACD Crossover</option>
                  <option value="bollinger_bounce">Bollinger Bounce</option>
                </select>
              </div>
              <div>
                <label className="block text-xs text-soft mb-1">Position Size ($)</label>
                <input
                  type="number"
                  value={(form.parameters.position_size as number) || 10}
                  onChange={(e) =>
                    setForm({
                      ...form,
                      parameters: {
                        ...form.parameters,
                        position_size: Number(e.target.value),
                      },
                    })
                  }
                  className="input-field w-full"
                />
              </div>
              <div>
                <label className="block text-xs text-soft mb-1">Condition ID</label>
                <input
                  type="text"
                  value={(form.parameters.condition_id as string) || ""}
                  onChange={(e) =>
                    setForm({
                      ...form,
                      parameters: {
                        ...form.parameters,
                        condition_id: e.target.value,
                      },
                    })
                  }
                  className="input-field w-full"
                  placeholder="Market condition ID"
                />
              </div>
              <div>
                <label className="block text-xs text-soft mb-1">Outcome token</label>
                <select
                  value={(form.parameters.outcome as string) || "Yes"}
                  onChange={(e) =>
                    setForm({
                      ...form,
                      parameters: { ...form.parameters, outcome: e.target.value },
                    })
                  }
                  className="input-field w-full"
                >
                  <option value="Yes">YES</option>
                  <option value="No">NO</option>
                </select>
              </div>
              <div>
                <label className="block text-xs text-soft mb-1">RSI Oversold</label>
                <input
                  type="number"
                  value={(form.parameters.rsi_oversold as number) || 30}
                  onChange={(e) =>
                    setForm({
                      ...form,
                      parameters: {
                        ...form.parameters,
                        rsi_oversold: Number(e.target.value),
                      },
                    })
                  }
                  className="input-field w-full"
                />
              </div>
            </div>
          )}

          {form.strategy_type === "copy_trade" && (
            <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
              <div>
                <label className="block text-xs text-soft mb-1">Followed Wallet</label>
                <input
                  type="text"
                  value={(form.parameters.followed_wallet as string) || ""}
                  onChange={(e) =>
                    setForm({
                      ...form,
                      parameters: {
                        ...form.parameters,
                        followed_wallet: e.target.value,
                      },
                    })
                  }
                  className="input-field w-full"
                  placeholder="0x..."
                />
              </div>
              <div>
                <label className="block text-xs text-soft mb-1">Max Position Size ($)</label>
                <input
                  type="number"
                  value={(form.parameters.max_position_size as number) || 100}
                  onChange={(e) =>
                    setForm({
                      ...form,
                      parameters: {
                        ...form.parameters,
                        max_position_size: Number(e.target.value),
                      },
                    })
                  }
                  className="input-field w-full"
                />
              </div>
              <div>
                <label className="block text-xs text-soft mb-1">Daily Loss Limit ($)</label>
                <input
                  type="number"
                  value={(form.parameters.daily_loss_limit as number) || 50}
                  onChange={(e) =>
                    setForm({
                      ...form,
                      parameters: {
                        ...form.parameters,
                        daily_loss_limit: Number(e.target.value),
                      },
                    })
                  }
                  className="input-field w-full"
                />
              </div>
            </div>
          )}

          <div className="flex justify-end gap-2">
            <button
              onClick={() => {
                setShowForm(false);
                setForm({ ...defaultForm });
              }}
              className="btn-muted text-sm"
            >
              Cancel
            </button>
            <button onClick={handleRun} disabled={running} className="btn-primary text-sm">
              {running ? "Running..." : "Run Backtest"}
            </button>
          </div>
        </div>
      )}

      {/* Selected Run Detail */}
      {selectedRun && (
        <div className="surface-panel p-5 space-y-4">
          <div className="flex items-center justify-between">
            <h3 className="text-base font-semibold text-white">
              {selectedRun.strategy_name || selectedRun.strategy_type} — Results
            </h3>
            <button
              onClick={() => setSelectedRun(null)}
              className="text-soft hover:text-white text-sm"
            >
              ✕ Close
            </button>
          </div>

          {selectedRun.status === "failed" && (
            <div role="alert" className="rounded bg-red-500/10 p-3 text-sm text-red-300">
              This run failed validation. Its zero-value placeholders are not trading results.
              {selectedRun.error_message && <p className="mt-1">{selectedRun.error_message}</p>}
            </div>
          )}
          {selectedRun.strategy_type === "indicator" && (
            <div className="rounded bg-surface-secondary p-3 text-xs text-soft" role="status">
              <p>
                {selectedRun.indicator_values?.history_quality === "indicative_price_only"
                  ? "Verified-source CLOB sampled token prices — indicative only. No historical bid/ask depth, executable fills, costs or validated trading edge."
                  : "Unverified or incomplete historical feed. Legacy results are not independently validated."}
              </p>
              {selectedRun.indicator_values?.history_source != null && (
                <p className="mt-1 break-all">
                  Outcome token:{" "}
                  {String(
                    (selectedRun.indicator_values.history_source as Record<string, unknown>)
                      .token_id || "unknown",
                  )}
                  {" · "}Dataset SHA-256:{" "}
                  {String(
                    (selectedRun.indicator_values.history_source as Record<string, unknown>)
                      .data_hash || "unknown",
                  ).slice(0, 16)}
                  {"…"}
                </p>
              )}
            </div>
          )}
          {selectedRun.status !== "failed" && (
            <div className="grid grid-cols-2 md:grid-cols-4 lg:grid-cols-6 gap-4 text-xs">
              <div>
                <span className="text-soft">{selectedRun.strategy_type === "indicator" ? "Gross Indicative P&L" : "Total PnL"}</span>
                <p className={`font-bold text-lg ${pnlColor(selectedRun.total_pnl)}`}>
                  ${selectedRun.total_pnl.toFixed(2)}
                </p>
              </div>
              <div>
                <span className="text-soft">Win Rate</span>
                <p className="text-white font-semibold text-lg">
                  {selectedRun.win_rate?.toFixed(1) ?? "—"}%
                </p>
              </div>
              <div>
                <span className="text-soft">Total Trades</span>
                <p className="text-white font-semibold">{selectedRun.total_trades}</p>
              </div>
              <div>
                <span className="text-soft">Sharpe Ratio</span>
                <p className="text-white font-semibold">
                  {selectedRun.sharpe_ratio?.toFixed(2) ?? "—"}
                </p>
              </div>
              <div>
                <span className="text-soft">Max Drawdown</span>
                <p className="text-red-400 font-semibold">
                  ${selectedRun.max_drawdown?.toFixed(2) ?? "—"}
                </p>
              </div>
              <div>
                <span className="text-soft">Profit Factor</span>
                <p className="text-white font-semibold">
                  {selectedRun.profit_factor?.toFixed(2) ?? "—"}
                </p>
              </div>
            </div>
          )}

          <div className="grid grid-cols-3 gap-4 text-xs">
            <div>
              <span className="text-soft">Winning</span>
              <p className="text-green-400 font-semibold">{selectedRun.winning_trades}</p>
            </div>
            <div>
              <span className="text-soft">Losing</span>
              <p className="text-red-400 font-semibold">{selectedRun.losing_trades}</p>
            </div>
            <div>
              <span className="text-soft">Avg PnL / Trade</span>
              <p className={`font-semibold ${pnlColor(selectedRun.avg_trade_pnl ?? 0)}`}>
                ${selectedRun.avg_trade_pnl?.toFixed(2) ?? "—"}
              </p>
            </div>
          </div>

          {selectedRun.status !== "failed" && selectedRun.error_message && (
            <p className="text-xs text-yellow-400 bg-yellow-500/10 rounded p-2">
              {selectedRun.error_message}
            </p>
          )}

          {/* Trade Log */}
          {selectedRun.trade_log && selectedRun.trade_log.length > 0 && (
            <div>
              <h4 className="text-sm font-semibold text-white mb-2">Trade Log</h4>
              <div className="max-h-60 overflow-y-auto">
                <table className="w-full text-xs">
                  <thead>
                    <tr className="text-soft border-b border-[var(--line)]">
                      <th className="text-left py-1 px-2">Time</th>
                      <th className="text-left py-1 px-2">Side</th>
                      <th className="text-right py-1 px-2">Price</th>
                      <th className="text-right py-1 px-2">Size</th>
                      <th className="text-right py-1 px-2">PnL</th>
                    </tr>
                  </thead>
                  <tbody>
                    {(selectedRun.trade_log as Array<Record<string, unknown>>).map((trade, idx) => (
                      <tr key={idx} className="border-b border-[var(--line)]/30">
                        <td className="py-1 px-2 text-soft">
                          {String(trade.timestamp || "").slice(0, 16)}
                        </td>
                        <td className="py-1 px-2">
                          <span
                            className={trade.side === "BUY" ? "text-green-400" : "text-red-400"}
                          >
                            {String(trade.side)}
                          </span>
                        </td>
                        <td className="py-1 px-2 text-right text-white">
                          {Number(trade.price).toFixed(4)}
                        </td>
                        <td className="py-1 px-2 text-right text-white">
                          ${Number(trade.size).toFixed(2)}
                        </td>
                        <td
                          className={`py-1 px-2 text-right font-semibold ${pnlColor(
                            Number(trade.pnl),
                          )}`}
                        >
                          ${Number(trade.pnl).toFixed(2)}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </div>
          )}
        </div>
      )}

      {/* Runs List */}
      {runs.length === 0 ? (
        <div className="surface-panel p-8 text-center">
          <p className="text-soft text-sm">No backtest runs yet.</p>
          <p className="text-soft text-xs mt-1">
            Run your first backtest to see historical performance data.
          </p>
        </div>
      ) : (
        <div className="space-y-3">
          <h3 className="text-sm font-semibold text-white">Previous Runs</h3>
          {runs.map((run) => (
            <div
              key={run.id}
              className="surface-panel p-4 cursor-pointer hover:border-[var(--accent)]/30 transition"
              onClick={() => setSelectedRun(run)}
            >
              <div className="flex items-center justify-between">
                <div>
                  <span className="text-sm font-semibold text-white">
                    {run.strategy_name || run.strategy_type}
                  </span>
                  <span className="text-xs text-soft ml-2">
                    {run.start_date} → {run.end_date}
                  </span>
                </div>
                <div className="flex items-center gap-3">
                  <span className={`text-sm font-bold ${pnlColor(run.total_pnl)}`}>
                    ${run.total_pnl.toFixed(2)}
                  </span>
                  <span className={`text-xs px-2 py-0.5 rounded-full ${statusBadge(run.status)}`}>
                    {run.status}
                  </span>
                  <button
                    onClick={(e) => {
                      e.stopPropagation();
                      setPendingDelete(run.id);
                    }}
                    className="text-xs text-soft hover:text-red-400"
                  >
                    Delete
                  </button>
                </div>
              </div>
              <div className="flex gap-4 mt-1 text-xs text-soft">
                <span>{run.total_trades} trades</span>
                <span>Win: {run.win_rate?.toFixed(1) ?? "—"}%</span>
                <span>Sharpe: {run.sharpe_ratio?.toFixed(2) ?? "—"}</span>
                <span>Max DD: ${run.max_drawdown?.toFixed(2) ?? "—"}</span>
              </div>
            </div>
          ))}
        </div>
      )}

      <ConfirmModal
        open={pendingDelete !== null}
        title="Delete backtest run?"
        body="This permanently deletes the backtest run and its results. This action cannot be undone."
        destructive
        confirmLabel="Delete"
        onConfirm={() => {
          const id = pendingDelete;
          setPendingDelete(null);
          if (id !== null) void handleDelete(id);
        }}
        onCancel={() => setPendingDelete(null)}
      />
    </div>
  );
}
