import { useCallback, useEffect, useState } from "react";
import { apiClient } from "../services/apiClient";
import { useAuthStore } from "../store/authStore";
import { getApiErrorMessage } from "../utils/apiError";
import { ConfirmModal } from "./ui";

interface ExecutionStatus {
  global: { state: string };
  wallet: { state: string };
  live_trading_enabled: boolean;
  live_copy_trading_enabled: boolean;
  live_execution_available: boolean;
  copy_trading_enabled: boolean;
  simulation_mode: boolean;
  initial_capital: number | null;
}

interface LiveOrder {
  order_hash: string;
  status: string;
  trade_size: number;
}

export default function LiveExecutionControls() {
  const isAdmin = useAuthStore((state) => state.isAdmin);
  const [status, setStatus] = useState<ExecutionStatus | null>(null);
  const [orders, setOrders] = useState<LiveOrder[]>([]);
  const [busy, setBusy] = useState(false);
  const [initialCapital, setInitialCapital] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [confirm, setConfirm] = useState<"user" | "global" | null>(null);
  const refresh = useCallback(async () => {
    try {
      const [summary, recent] = await Promise.all([
        apiClient.get<ExecutionStatus>("/api/v1/risk/summary"),
        apiClient.get<LiveOrder[]>("/api/v1/risk/orders"),
      ]);
      setStatus(summary);
      setInitialCapital(
        (current) => current || (summary.initial_capital ? String(summary.initial_capital) : ""),
      );
      setOrders(recent);
    } catch (err) {
      setStatus(null);
      setError(getApiErrorMessage(err, "Execution status unavailable"));
    }
  }, []);
  useEffect(() => {
    void refresh();
    const timer = window.setInterval(() => void refresh(), 15000);
    return () => window.clearInterval(timer);
  }, [refresh]);

  async function run(action: () => Promise<unknown>) {
    setBusy(true);
    setError(null);
    try {
      await action();
      await refresh();
    } catch (err) {
      setError(getApiErrorMessage(err, "Could not update execution settings"));
    } finally {
      setBusy(false);
      setConfirm(null);
    }
  }

  return (
    <div className="surface-panel p-6 rounded-lg space-y-3">
      <h2 className="text-lg font-semibold">Copy Trading Execution</h2>
      <p className="text-sm text-soft" role="status">
        {status
          ? `${status.copy_trading_enabled ? "Enabled" : "Stopped"} · ${status.simulation_mode ? "Simulation" : "Live account"} · Exchange execution ${status.live_execution_available && status.live_copy_trading_enabled ? "available" : "blocked"}`
          : "Loading execution status…"}
      </p>
      <p className="text-sm text-muted">
        Live copying uses real funds. Follow wallets below, configure your risk limits in Settings,
        then activate your account. Live budgets reserve purchase principal until cancellation is
        verified; simulated returns never increase them.
      </p>
      {error && (
        <p role="alert" className="status-bad text-sm">
          {error}
        </p>
      )}
      <label className="block text-sm text-soft">
        Initial capital for live loss budgets
        <input
          type="number"
          min="0.01"
          step="0.01"
          value={initialCapital}
          onChange={(event) => setInitialCapital(event.target.value)}
          className="ml-3 bg-[var(--bg-soft)] border border-[var(--line)] rounded px-3 py-2"
          placeholder="Amount in collateral"
        />
      </label>
      <div className="flex flex-wrap gap-2">
        <button
          className="btn-secondary"
          disabled={busy || !status}
          onClick={() =>
            void run(() =>
              apiClient.put("/api/settings/copy-trading", {
                simulation_mode: true,
                copy_trading_enabled: true,
              }),
            )
          }
        >
          Enable Simulation
        </button>
        <button
          className="btn-primary"
          disabled={busy || !status?.live_trading_enabled || !status.live_copy_trading_enabled}
          onClick={() => setConfirm("user")}
        >
          Activate Live Copying
        </button>
        <button
          className="btn-secondary"
          disabled={busy || !status}
          onClick={() =>
            void run(() =>
              apiClient.put("/api/settings/copy-trading", { copy_trading_enabled: false }),
            )
          }
        >
          Stop Copying
        </button>
        {isAdmin && status?.global.state !== "RUNNING" && (
          <button
            className="btn-secondary"
            disabled={busy || !status?.live_trading_enabled}
            onClick={() => setConfirm("global")}
          >
            Activate Global Live Execution
          </button>
        )}
      </div>
      {status && !status.live_trading_enabled && (
        <p className="text-xs text-muted">
          The server operator must enable live trading and live copy trading before activation.
        </p>
      )}
      {orders.length > 0 && (
        <div className="space-y-2">
          <h3 className="font-medium text-sm">Recent Live Orders</h3>
          {orders.slice(0, 10).map((order) => (
            <div key={order.order_hash} className="flex flex-wrap items-center gap-3 text-sm">
              <span className="mono">{order.order_hash.slice(0, 14)}…</span>
              <span>
                {order.status} · ${order.trade_size.toFixed(2)}
              </span>
              <button
                className="btn-secondary"
                disabled={busy}
                onClick={() =>
                  void run(() =>
                    apiClient.post(
                      `/api/v1/risk/orders/${encodeURIComponent(order.order_hash)}/reconcile`,
                    ),
                  )
                }
              >
                Check Exchange
              </button>
              {["submitted", "partially_filled", "unknown", "submitting"].includes(
                order.status,
              ) && (
                <button
                  className="btn-secondary"
                  disabled={busy}
                  onClick={() =>
                    void run(() =>
                      apiClient.post(
                        `/api/v1/risk/orders/${encodeURIComponent(order.order_hash)}/cancel`,
                      ),
                    )
                  }
                >
                  Cancel Order
                </button>
              )}
            </div>
          ))}
        </div>
      )}
      <ConfirmModal
        open={confirm !== null}
        title={confirm === "global" ? "Activate Global Live Execution" : "Activate Live Copying"}
        body={
          confirm === "global"
            ? "Allow activated accounts to submit real exchange orders within the server strategy switches?"
            : "Enable real copy orders for the wallets you follow? Orders will spend real funds within your configured risk budgets."
        }
        confirmLabel="Activate Live"
        confirmDisabled={busy}
        onCancel={() => setConfirm(null)}
        onConfirm={() =>
          void run(async () => {
            await apiClient.post("/api/v1/risk/activate", {
              scope: confirm,
              ...(confirm === "user" && Number(initialCapital) > 0
                ? { initial_capital: Number(initialCapital) }
                : {}),
            });
            if (confirm === "user")
              await apiClient.put("/api/settings/copy-trading", { copy_trading_enabled: true });
          })
        }
      />
    </div>
  );
}
