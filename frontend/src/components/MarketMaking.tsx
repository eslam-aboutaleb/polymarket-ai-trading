import { useEffect, useState, useCallback } from "react";
import {
  marketMakerService,
  MarketMakerConfig,
  UpsertMarketMakerConfigRequest,
  MarketMakerMetrics,
} from "../services/marketMakerService";

type FormState = UpsertMarketMakerConfigRequest;

const defaultForm: FormState = {
  condition_id: "",
  token_id_yes: "",
  token_id_no: "",
  market_title: "",
  enabled: false,
  strategy: "bands",
  num_bands: 3,
  min_spread: 0.02,
  max_spread: 0.1,
  band_order_size: 10,
  amm_liquidity: 1000,
  max_collateral: 500,
  sync_interval_seconds: 30,
  min_order_size: 1,
  min_price: 0.01,
  max_price: 0.99,
};

export default function MarketMaking() {
  const [configs, setConfigs] = useState<MarketMakerConfig[]>([]);
  const [metrics, setMetrics] = useState<MarketMakerMetrics | null>(null);
  const [loading, setLoading] = useState(true);
  const [showForm, setShowForm] = useState(false);
  const [form, setForm] = useState<FormState>({ ...defaultForm });
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const fetchData = useCallback(async () => {
    try {
      const [cfgs, met] = await Promise.all([
        marketMakerService.listMarketMakerConfigs(),
        marketMakerService.getMarketMakerMetrics(),
      ]);
      setConfigs(cfgs);
      setMetrics(met);
    } catch (e) {
      setError(String(e));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    fetchData();
    const interval = setInterval(fetchData, 15000);
    return () => clearInterval(interval);
  }, [fetchData]);

  const handleSave = async () => {
    if (!form.condition_id || !form.token_id_yes || !form.token_id_no) {
      setError("Condition ID and both Token IDs are required");
      return;
    }
    setSaving(true);
    setError(null);
    try {
      await marketMakerService.upsertMarketMakerConfig(form);
      setShowForm(false);
      setForm({ ...defaultForm });
      await fetchData();
    } catch (e) {
      setError(String(e));
    } finally {
      setSaving(false);
    }
  };

  const handleStart = async (id: number) => {
    try {
      await marketMakerService.startMarketMaker(id);
      await fetchData();
    } catch (e) {
      setError(String(e));
    }
  };

  const handleStop = async (id: number) => {
    try {
      await marketMakerService.stopMarketMaker(id);
      await fetchData();
    } catch (e) {
      setError(String(e));
    }
  };

  const handleSync = async (id: number) => {
    try {
      const result = await marketMakerService.syncMarketMaker(id);
      if (!result.success) {
        setError(result.detail || "Sync failed");
      }
      await fetchData();
    } catch (e) {
      setError(String(e));
    }
  };

  const handleDelete = async (id: number) => {
    if (!confirm("Disable this market maker config?")) return;
    try {
      await marketMakerService.deleteMarketMakerConfig(id);
      await fetchData();
    } catch (e) {
      setError(String(e));
    }
  };

  if (loading) {
    return (
      <div className="p-6">
        <p className="text-soft text-sm">Loading market maker configs...</p>
      </div>
    );
  }

  return (
    <div className="p-6 space-y-6 max-w-6xl mx-auto">
      {/* Header */}
      <div className="flex items-center justify-between">
        <div>
          <h2 className="text-xl font-bold text-white">Market Making</h2>
          <p className="text-soft text-sm mt-1">
            Configure automated market making with Bands or AMM strategies
          </p>
        </div>
        <div className="flex items-center gap-3">
          {metrics && (
            <span className="text-xs text-soft">
              {metrics.total_running} running
            </span>
          )}
          <button
            onClick={() => setShowForm(!showForm)}
            className="btn-primary text-sm"
          >
            {showForm ? "Cancel" : "+ New Config"}
          </button>
        </div>
      </div>

      {error && (
        <div className="bg-red-500/10 border border-red-500/30 rounded-lg p-3 text-red-400 text-sm">
          {error}
          <button
            onClick={() => setError(null)}
            className="ml-2 text-red-300 hover:text-white"
          >
            ✕
          </button>
        </div>
      )}

      {/* Create/Edit Form */}
      {showForm && (
        <div className="surface-panel p-5 space-y-4">
          <h3 className="text-base font-semibold text-white">
            New Market Maker Config
          </h3>

          <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
            <div>
              <label className="block text-xs text-soft mb-1">
                Condition ID *
              </label>
              <input
                type="text"
                value={form.condition_id}
                onChange={(e) =>
                  setForm({ ...form, condition_id: e.target.value })
                }
                className="input-field w-full"
                placeholder="0x..."
              />
            </div>
            <div>
              <label className="block text-xs text-soft mb-1">
                Market Title
              </label>
              <input
                type="text"
                value={form.market_title || ""}
                onChange={(e) =>
                  setForm({ ...form, market_title: e.target.value })
                }
                className="input-field w-full"
                placeholder="Market name"
              />
            </div>
            <div>
              <label className="block text-xs text-soft mb-1">
                Token ID (Yes) *
              </label>
              <input
                type="text"
                value={form.token_id_yes}
                onChange={(e) =>
                  setForm({ ...form, token_id_yes: e.target.value })
                }
                className="input-field w-full"
              />
            </div>
            <div>
              <label className="block text-xs text-soft mb-1">
                Token ID (No) *
              </label>
              <input
                type="text"
                value={form.token_id_no}
                onChange={(e) =>
                  setForm({ ...form, token_id_no: e.target.value })
                }
                className="input-field w-full"
              />
            </div>
          </div>

          <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
            <div>
              <label className="block text-xs text-soft mb-1">Strategy</label>
              <select
                value={form.strategy}
                onChange={(e) =>
                  setForm({
                    ...form,
                    strategy: e.target.value as "bands" | "amm",
                  })
                }
                className="input-field w-full"
              >
                <option value="bands">Bands</option>
                <option value="amm">AMM</option>
              </select>
            </div>
            <div>
              <label className="block text-xs text-soft mb-1">Num Bands</label>
              <input
                type="number"
                value={form.num_bands}
                onChange={(e) =>
                  setForm({ ...form, num_bands: Number(e.target.value) })
                }
                className="input-field w-full"
                min={1}
                max={20}
              />
            </div>
            <div>
              <label className="block text-xs text-soft mb-1">Min Spread</label>
              <input
                type="number"
                step="0.001"
                value={form.min_spread}
                onChange={(e) =>
                  setForm({ ...form, min_spread: Number(e.target.value) })
                }
                className="input-field w-full"
              />
            </div>
            <div>
              <label className="block text-xs text-soft mb-1">Max Spread</label>
              <input
                type="number"
                step="0.001"
                value={form.max_spread}
                onChange={(e) =>
                  setForm({ ...form, max_spread: Number(e.target.value) })
                }
                className="input-field w-full"
              />
            </div>
          </div>

          <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
            <div>
              <label className="block text-xs text-soft mb-1">
                Band Order Size ($)
              </label>
              <input
                type="number"
                value={form.band_order_size}
                onChange={(e) =>
                  setForm({ ...form, band_order_size: Number(e.target.value) })
                }
                className="input-field w-full"
              />
            </div>
            <div>
              <label className="block text-xs text-soft mb-1">
                Max Collateral ($)
              </label>
              <input
                type="number"
                value={form.max_collateral}
                onChange={(e) =>
                  setForm({ ...form, max_collateral: Number(e.target.value) })
                }
                className="input-field w-full"
              />
            </div>
            <div>
              <label className="block text-xs text-soft mb-1">
                Sync Interval (s)
              </label>
              <input
                type="number"
                value={form.sync_interval_seconds}
                onChange={(e) =>
                  setForm({
                    ...form,
                    sync_interval_seconds: Number(e.target.value),
                  })
                }
                className="input-field w-full"
                min={10}
              />
            </div>
            <div>
              <label className="block text-xs text-soft mb-1">
                AMM Liquidity ($)
              </label>
              <input
                type="number"
                value={form.amm_liquidity}
                onChange={(e) =>
                  setForm({ ...form, amm_liquidity: Number(e.target.value) })
                }
                className="input-field w-full"
              />
            </div>
          </div>

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
            <button
              onClick={handleSave}
              disabled={saving}
              className="btn-primary text-sm"
            >
              {saving ? "Saving..." : "Save Config"}
            </button>
          </div>
        </div>
      )}

      {/* Configs List */}
      {configs.length === 0 ? (
        <div className="surface-panel p-8 text-center">
          <p className="text-soft text-sm">No market maker configs yet.</p>
          <p className="text-soft text-xs mt-1">
            Create one to start automated market making on a Polymarket market.
          </p>
        </div>
      ) : (
        <div className="space-y-4">
          {configs.map((cfg) => (
            <div key={cfg.id} className="surface-panel p-4 space-y-3">
              <div className="flex items-start justify-between">
                <div>
                  <h4 className="text-sm font-semibold text-white">
                    {cfg.market_title || cfg.condition_id.slice(0, 20) + "..."}
                  </h4>
                  <p className="text-xs text-soft mt-0.5">
                    Strategy:{" "}
                    <span className="text-white">
                      {cfg.strategy.toUpperCase()}
                    </span>
                    {" · "}
                    Bands: {cfg.num_bands}
                    {" · "}
                    Spread: {(cfg.min_spread * 100).toFixed(1)}%–
                    {(cfg.max_spread * 100).toFixed(1)}%
                  </p>
                </div>
                <div className="flex items-center gap-2">
                  <span
                    className={`text-xs px-2 py-0.5 rounded-full font-semibold ${
                      cfg.is_running
                        ? "bg-green-500/20 text-green-400"
                        : cfg.status === "error"
                          ? "bg-red-500/20 text-red-400"
                          : "bg-gray-500/20 text-gray-400"
                    }`}
                  >
                    {cfg.is_running ? "Running" : cfg.status}
                  </span>
                </div>
              </div>

              {/* Stats row */}
              <div className="grid grid-cols-2 md:grid-cols-5 gap-3 text-xs">
                <div>
                  <span className="text-soft">Open Orders</span>
                  <p className="text-white font-semibold">
                    {cfg.current_open_orders}
                  </p>
                </div>
                <div>
                  <span className="text-soft">Total Placed</span>
                  <p className="text-white font-semibold">
                    {cfg.total_orders_placed}
                  </p>
                </div>
                <div>
                  <span className="text-soft">Total Cancelled</span>
                  <p className="text-white font-semibold">
                    {cfg.total_orders_cancelled}
                  </p>
                </div>
                <div>
                  <span className="text-soft">Volume (USDC)</span>
                  <p className="text-white font-semibold">
                    ${cfg.total_volume_usdc.toFixed(2)}
                  </p>
                </div>
                <div>
                  <span className="text-soft">Last Sync</span>
                  <p className="text-white font-semibold text-[11px]">
                    {cfg.last_sync_at
                      ? new Date(cfg.last_sync_at).toLocaleTimeString()
                      : "Never"}
                  </p>
                </div>
              </div>

              {cfg.last_error && (
                <p className="text-xs text-red-400 bg-red-500/10 rounded p-2">
                  {cfg.last_error}
                </p>
              )}

              {/* Actions */}
              <div className="flex gap-2">
                {cfg.is_running ? (
                  <button
                    onClick={() => handleStop(cfg.id)}
                    className="btn-danger text-xs"
                  >
                    Stop
                  </button>
                ) : (
                  <button
                    onClick={() => handleStart(cfg.id)}
                    className="btn-primary text-xs"
                  >
                    Start
                  </button>
                )}
                <button
                  onClick={() => handleSync(cfg.id)}
                  className="btn-muted text-xs"
                >
                  Sync Now
                </button>
                <button
                  onClick={() => handleDelete(cfg.id)}
                  className="btn-muted text-xs text-red-400 hover:text-red-300"
                >
                  Disable
                </button>
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
