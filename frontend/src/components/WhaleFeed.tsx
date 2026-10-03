/**
 * Live on-chain whale activity panel.
 *
 * Shows whale-sized CTF position movements detected directly on
 * Polygon (typically 3–30s ahead of the public positions API),
 * with per-user thresholds, a wallet watchlist and an opt-in
 * auto-copy toggle. The feed polls every 15 seconds.
 *
 * @module components/WhaleFeed
 */
import { useCallback, useEffect, useMemo, useState } from "react";
import { getApiErrorMessage } from "../utils/apiError";
import { whaleService, WhaleConfig, WhaleEvent } from "../services/whaleService";

const POLL_INTERVAL_MS = 15000;
const DEFAULT_MIN_NOTIONAL = 10000;
const DEFAULT_WHALE_SET_SIZE = 50;

const SIDE_LABELS: Record<string, string> = {
  buy: "Buy",
  sell: "Sell",
  open: "Open",
  close: "Close",
  redeem: "Redeem",
};

const EVENT_TYPE_LABELS: Record<string, string> = {
  transfer: "Transfer",
  position_split: "Split",
  position_merge: "Merge",
  redemption: "Redemption",
};

function formatNotional(value: number): string {
  if (value >= 1_000_000) return `$${(value / 1_000_000).toFixed(2)}M`;
  if (value >= 1_000) return `$${(value / 1_000).toFixed(1)}k`;
  return `$${value.toFixed(0)}`;
}

function formatSize(value: number): string {
  return value >= 10_000
    ? value.toLocaleString(undefined, { maximumFractionDigits: 0 })
    : value.toFixed(0);
}

function formatAge(detectedAt: string): string {
  const seconds = Math.max(0, (Date.now() - new Date(detectedAt).getTime()) / 1000);
  if (seconds < 60) return `${Math.floor(seconds)}s ago`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m ago`;
  return `${Math.floor(seconds / 3600)}h ago`;
}

function truncateWallet(wallet: string): string {
  if (wallet.length <= 12) return wallet;
  return `${wallet.slice(0, 6)}…${wallet.slice(-4)}`;
}

export default function WhaleFeed() {
  const [error, setError] = useState<string | null>(null);
  const [success, setSuccess] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);

  // ── Feed state ────────────────────────────────────────────
  const [events, setEvents] = useState<WhaleEvent[]>([]);
  const [total, setTotal] = useState(0);
  const [sideFilter, setSideFilter] = useState("");
  const [typeFilter, setTypeFilter] = useState("");

  // ── Config state ──────────────────────────────────────────
  const [config, setConfig] = useState<WhaleConfig>({
    min_notional: DEFAULT_MIN_NOTIONAL,
    auto_copy: false,
    watchlist: [],
    whale_set_size: DEFAULT_WHALE_SET_SIZE,
  });
  const [watchlistText, setWatchlistText] = useState("");

  const fetchEvents = useCallback(async () => {
    try {
      const result = await whaleService.listEvents({
        limit: 100,
        side: sideFilter || undefined,
        event_type: typeFilter || undefined,
      });
      setEvents(result.events);
      setTotal(result.total);
      setError(null);
    } catch (err) {
      setError(getApiErrorMessage(err));
    } finally {
      setLoading(false);
    }
  }, [sideFilter, typeFilter]);

  const fetchConfig = useCallback(async () => {
    try {
      const result = await whaleService.getConfig();
      setConfig(result);
      setWatchlistText(result.watchlist.join("\n"));
    } catch (err) {
      setError(getApiErrorMessage(err));
    }
  }, []);

  useEffect(() => {
    fetchConfig().finally(() => fetchEvents());
  }, [fetchConfig, fetchEvents]);

  useEffect(() => {
    const timer = setInterval(fetchEvents, POLL_INTERVAL_MS);
    return () => clearInterval(timer);
  }, [fetchEvents]);

  const saveConfig = async () => {
    setSaving(true);
    setSuccess(null);
    setError(null);
    try {
      const watchlist = watchlistText
        .split(/[\n,]+/)
        .map((w) => w.trim().toLowerCase())
        .filter((w) => w.startsWith("0x"));
      const result = await whaleService.updateConfig({
        min_notional: config.min_notional,
        auto_copy: config.auto_copy,
        watchlist,
        whale_set_size: config.whale_set_size,
      });
      setConfig(result);
      setWatchlistText(result.watchlist.join("\n"));
      setSuccess("Whale monitoring settings saved");
    } catch (err) {
      setError(getApiErrorMessage(err));
    } finally {
      setSaving(false);
    }
  };

  const sideOptions = useMemo(() => ["buy", "sell", "open", "close", "redeem"], []);
  const typeOptions = useMemo(
    () => ["transfer", "position_split", "position_merge", "redemption"],
    [],
  );

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-3xl font-bold">Whale Monitoring</h1>
        <p className="text-soft text-sm mt-1">
          Whale-sized position movements detected on-chain, typically 3–30s ahead of the public
          positions API.
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

      <section className="surface-panel p-5 space-y-4">
        <div className="flex items-center justify-between gap-3 flex-wrap">
          <h2 className="text-lg font-semibold">Alert Settings</h2>
          <button onClick={saveConfig} disabled={saving} className="btn-muted text-xs px-3 py-1.5">
            {saving ? "Saving…" : "Save Settings"}
          </button>
        </div>
        <div className="grid gap-3 md:grid-cols-3">
          <div className="rounded border border-[var(--line)] bg-[var(--bg-soft)] p-3">
            <p className="text-xs font-semibold mb-1">Min notional (USD)</p>
            <input
              type="number"
              min={1}
              value={config.min_notional}
              onChange={(e) => setConfig({ ...config, min_notional: Number(e.target.value) })}
              className="w-full bg-transparent text-sm border-b border-[var(--line)] focus:border-[var(--accent)] outline-none py-1"
            />
            <p className="text-xs text-soft mt-1">Only alert on whale moves above this size.</p>
          </div>
          <div className="rounded border border-[var(--line)] bg-[var(--bg-soft)] p-3">
            <p className="text-xs font-semibold mb-1">Leaderboard whale set</p>
            <input
              type="number"
              min={1}
              max={500}
              value={config.whale_set_size}
              onChange={(e) => setConfig({ ...config, whale_set_size: Number(e.target.value) })}
              className="w-full bg-transparent text-sm border-b border-[var(--line)] focus:border-[var(--accent)] outline-none py-1"
            />
            <p className="text-xs text-soft mt-1">Top wallets tracked from the leaderboard.</p>
          </div>
          <div className="rounded border border-[var(--line)] bg-[var(--bg-soft)] p-3">
            <p className="text-xs font-semibold mb-1">Auto-copy whale trades</p>
            <label className="flex items-center gap-2 text-sm py-1">
              <input
                type="checkbox"
                checked={config.auto_copy}
                onChange={(e) => setConfig({ ...config, auto_copy: e.target.checked })}
              />
              Copy followed-whale trades automatically
            </label>
            <p className="text-xs text-soft mt-1">
              Off by default. Inherits your copy-trade risk settings.
            </p>
          </div>
        </div>
        <div className="rounded border border-[var(--line)] bg-[var(--bg-soft)] p-3">
          <p className="text-xs font-semibold mb-1">Watchlist (one wallet per line)</p>
          <textarea
            value={watchlistText}
            onChange={(e) => setWatchlistText(e.target.value)}
            rows={4}
            placeholder={"0x…\n0x…"}
            className="w-full bg-transparent text-sm border border-[var(--line)] rounded p-2 focus:border-[var(--accent)] outline-none font-mono"
          />
          <p className="text-xs text-soft mt-1">
            Get alerts for these wallets even if they are not on the leaderboard.
          </p>
        </div>
      </section>

      <section className="surface-panel p-5 space-y-3">
        <div className="flex items-center justify-between gap-3 flex-wrap">
          <h2 className="text-lg font-semibold">
            Live Whale Activity{" "}
            <span className="text-sm font-normal text-soft">({total.toLocaleString()} events)</span>
          </h2>
          <div className="flex items-center gap-2">
            <select
              value={sideFilter}
              onChange={(e) => setSideFilter(e.target.value)}
              className="bg-[var(--bg-soft)] border border-[var(--line)] rounded text-xs px-2 py-1.5"
            >
              <option value="">All sides</option>
              {sideOptions.map((side) => (
                <option key={side} value={side}>
                  {SIDE_LABELS[side] ?? side}
                </option>
              ))}
            </select>
            <select
              value={typeFilter}
              onChange={(e) => setTypeFilter(e.target.value)}
              className="bg-[var(--bg-soft)] border border-[var(--line)] rounded text-xs px-2 py-1.5"
            >
              <option value="">All types</option>
              {typeOptions.map((type) => (
                <option key={type} value={type}>
                  {EVENT_TYPE_LABELS[type] ?? type}
                </option>
              ))}
            </select>
            <button onClick={() => fetchEvents()} className="btn-muted text-xs px-3 py-1.5">
              Refresh
            </button>
          </div>
        </div>

        {loading ? (
          <div className="space-y-3">
            {[1, 2, 3, 4, 5].map((i) => (
              <div key={i} className="surface-panel p-4 animate-pulse h-16" />
            ))}
          </div>
        ) : events.length === 0 ? (
          <p className="text-soft text-sm py-8 text-center">
            No whale events yet. Activity appears here seconds after whales move on-chain.
          </p>
        ) : (
          <div className="overflow-x-auto">
            <table className="table-theme text-sm w-full">
              <thead>
                <tr className="text-left text-xs text-muted/60">
                  <th className="pb-2 pr-4">Wallet</th>
                  <th className="pb-2 pr-4">Market</th>
                  <th className="pb-2 pr-4">Side</th>
                  <th className="pb-2 pr-4 text-right">Size</th>
                  <th className="pb-2 pr-4 text-right">Notional</th>
                  <th className="pb-2 text-right">Age</th>
                </tr>
              </thead>
              <tbody>
                {events.map((event) => (
                  <tr key={event.id} className="border-t border-[var(--line)] transition">
                    <td className="py-2.5 pr-4 font-mono text-xs">
                      <span title={event.wallet}>{truncateWallet(event.wallet)}</span>
                    </td>
                    <td className="py-2.5 pr-4 max-w-xs">
                      <p className="truncate">{event.market_id || "—"}</p>
                      <p className="text-xs text-soft truncate">
                        {EVENT_TYPE_LABELS[event.event_type] ?? event.event_type}
                      </p>
                    </td>
                    <td className="py-2.5 pr-4">
                      <span
                        className={
                          "text-xs font-semibold px-2 py-0.5 rounded " +
                          (event.side === "buy" || event.side === "open"
                            ? "bg-green-500/10 text-green-400"
                            : event.side === "sell" || event.side === "close"
                              ? "bg-red-500/10 text-red-400"
                              : "bg-[var(--bg-soft)] text-soft")
                        }
                      >
                        {SIDE_LABELS[event.side] ?? event.side}
                      </span>
                    </td>
                    <td className="py-2.5 pr-4 text-right font-mono text-xs">
                      {formatSize(event.size)}
                    </td>
                    <td className="py-2.5 pr-4 text-right font-semibold">
                      {formatNotional(event.notional)}
                    </td>
                    <td className="py-2.5 text-right text-xs text-soft">
                      {formatAge(event.detected_at)}
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
