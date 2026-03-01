import { useCallback, useEffect, useRef, useState } from "react";
import {
  debugService,
  LogEntry,
  StatsResponse,
  HealthResponse,
  EndpointStat,
} from "../services/debugService";

/* ═══════════════════════════════════════════════════════════════
   Debug Dashboard — real-time monitoring for backend services
   ═══════════════════════════════════════════════════════════════ */

type LevelFilter = "all" | "error" | "warning" | "info";

export default function DebugDashboard() {
  /* ──── state ──── */
  const [logs, setLogs] = useState<LogEntry[]>([]);
  const [totalLogs, setTotalLogs] = useState(0);
  const [stats, setStats] = useState<StatsResponse | null>(null);
  const [health, setHealth] = useState<HealthResponse | null>(null);
  const [levelFilter, setLevelFilter] = useState<LevelFilter>("all");
  const [pathFilter, setPathFilter] = useState("");
  const [autoRefresh, setAutoRefresh] = useState(true);
  const [pageHidden, setPageHidden] = useState(
    typeof document !== "undefined" ? document.hidden : false,
  );
  const [nextRefreshInSeconds, setNextRefreshInSeconds] = useState<number | null>(
    null,
  );
  const [expandedRow, setExpandedRow] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [activeTab, setActiveTab] = useState<"logs" | "endpoints" | "health">(
    "logs",
  );
  const [sortCol, setSortCol] = useState<keyof EndpointStat>("call_count");
  const [sortAsc, setSortAsc] = useState(false);
  const timeoutRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const nextRefreshAtRef = useRef<number | null>(null);
  const backoffMsRef = useRef(5000);
  const refreshInFlightRef = useRef(false);

  /* ──── data fetching ──── */
  const refresh = useCallback(async (): Promise<boolean> => {
    if (refreshInFlightRef.current) return true;
    refreshInFlightRef.current = true;
    try {
      const [logsRes, statsRes, healthRes] = await Promise.all([
        debugService.fetchLogs({
          level: levelFilter === "all" ? undefined : levelFilter,
          path: pathFilter || undefined,
          limit: 300,
        }),
        debugService.fetchStats(),
        debugService.fetchHealth(),
      ]);
      setLogs(logsRes.entries);
      setTotalLogs(logsRes.total);
      setStats(statsRes);
      setHealth(healthRes);
      return true;
    } catch {
      /* ignore — user might not be authed yet */
      return false;
    } finally {
      refreshInFlightRef.current = false;
      setLoading(false);
    }
  }, [levelFilter, pathFilter]);

  const clearScheduledRefresh = useCallback(() => {
    if (timeoutRef.current) {
      clearTimeout(timeoutRef.current);
      timeoutRef.current = null;
    }
    nextRefreshAtRef.current = null;
    setNextRefreshInSeconds(null);
  }, []);

  const scheduleNextRefresh = useCallback(
    (delayMs: number) => {
      clearScheduledRefresh();

      nextRefreshAtRef.current = Date.now() + delayMs;
      setNextRefreshInSeconds(Math.max(1, Math.ceil(delayMs / 1000)));

      timeoutRef.current = setTimeout(async () => {
        if (!autoRefresh || document.hidden) return;
        const ok = await refresh();
        backoffMsRef.current = ok
          ? 5000
          : Math.min(backoffMsRef.current * 2, 60000);
        scheduleNextRefresh(backoffMsRef.current);
      }, delayMs);
    },
    [autoRefresh, clearScheduledRefresh, refresh],
  );

  useEffect(() => {
    refresh();
  }, [refresh]);

  useEffect(() => {
    if (!autoRefresh || pageHidden) {
      clearScheduledRefresh();
      return;
    }
    backoffMsRef.current = 5000;
    scheduleNextRefresh(backoffMsRef.current);
    return clearScheduledRefresh;
  }, [autoRefresh, pageHidden, scheduleNextRefresh, clearScheduledRefresh]);

  useEffect(() => {
    const visibilityTick = setInterval(() => {
      const nextRefreshAt = nextRefreshAtRef.current;
      if (!nextRefreshAt) {
        setNextRefreshInSeconds(null);
        return;
      }
      const remainingMs = nextRefreshAt - Date.now();
      setNextRefreshInSeconds(Math.max(0, Math.ceil(remainingMs / 1000)));
    }, 400);
    return () => clearInterval(visibilityTick);
  }, []);

  useEffect(() => {
    const onVisibilityChange = () => {
      const hidden = document.hidden;
      setPageHidden(hidden);
      if (hidden) {
        clearScheduledRefresh();
        return;
      }
      if (autoRefresh) {
        backoffMsRef.current = 5000;
        scheduleNextRefresh(backoffMsRef.current);
      }
    };

    document.addEventListener("visibilitychange", onVisibilityChange);
    return () => {
      document.removeEventListener("visibilitychange", onVisibilityChange);
    };
  }, [autoRefresh, clearScheduledRefresh, scheduleNextRefresh]);

  const handleManualRefresh = useCallback(async () => {
    const ok = await refresh();
    if (autoRefresh && !pageHidden) {
      backoffMsRef.current = ok ? 5000 : Math.min(backoffMsRef.current * 2, 60000);
      scheduleNextRefresh(backoffMsRef.current);
    }
  }, [autoRefresh, pageHidden, refresh, scheduleNextRefresh]);

  const handleClearLogs = async () => {
    await debugService.clearLogs();
    refresh();
  };

  /* ──── helpers ──── */
  const fmtTime = (iso: string) => {
    const d = new Date(iso);
    return d.toLocaleTimeString([], {
      hour: "2-digit",
      minute: "2-digit",
      second: "2-digit",
    });
  };

  const methodColor = (m: string) => {
    switch (m) {
      case "GET":
        return "bg-blue-500/20 text-blue-400";
      case "POST":
        return "bg-[var(--accent-soft)] text-[var(--accent)]";
      case "PUT":
        return "bg-yellow-500/20 text-yellow-400";
      case "DELETE":
        return "bg-red-500/20 text-red-400";
      default:
        return "bg-[var(--bg-soft)] text-muted";
    }
  };

  const statusColor = (code: number) => {
    if (code >= 500) return "text-red-400";
    if (code >= 400) return "text-yellow-400";
    if (code >= 300) return "text-blue-400";
    return "text-green-400";
  };

  const levelBadge = (level: string) => {
    switch (level) {
      case "error":
        return "chip-danger";
      case "warning":
        return "chip-warning";
      default:
        return "chip-success";
    }
  };

  const healthColor = (status: string) => {
    switch (status) {
      case "healthy":
        return "border-green-500/40 bg-green-500/10";
      case "unhealthy":
        return "border-red-500/40 bg-red-500/10";
      case "warning":
        return "border-yellow-500/40 bg-yellow-500/10";
      default:
        return "border-[var(--line)] bg-[var(--bg-soft)]";
    }
  };

  const healthDot = (status: string) => {
    switch (status) {
      case "healthy":
        return "bg-green-400";
      case "unhealthy":
        return "bg-red-400";
      case "warning":
        return "bg-yellow-400";
      default:
        return "bg-gray-400";
    }
  };

  const durationColor = (ms: number) => {
    if (ms > 5000) return "text-red-400";
    if (ms > 1000) return "text-yellow-400";
    return "text-muted";
  };

  /* ──── sorted endpoint stats ──── */
  const sortedEndpoints = stats
    ? [...stats.endpoints].sort((a, b) => {
        const av = a[sortCol] ?? 0;
        const bv = b[sortCol] ?? 0;
        if (typeof av === "string" && typeof bv === "string")
          return sortAsc ? av.localeCompare(bv) : bv.localeCompare(av);
        return sortAsc
          ? (av as number) - (bv as number)
          : (bv as number) - (av as number);
      })
    : [];

  const handleSort = (col: keyof EndpointStat) => {
    if (sortCol === col) setSortAsc((p) => !p);
    else {
      setSortCol(col);
      setSortAsc(false);
    }
  };

  const sortIcon = (col: keyof EndpointStat) =>
    sortCol === col ? (sortAsc ? " ↑" : " ↓") : "";

  /* ──── loading skeleton ──── */
  if (loading) {
    return (
      <div className="space-y-6">
        <h1 className="text-3xl font-bold">Debug Dashboard</h1>
        <p className="text-soft">Loading monitoring data…</p>
        <div className="space-y-3">
          {Array.from({ length: 4 }).map((_, i) => (
            <div key={i} className="surface-panel p-4 animate-pulse h-20" />
          ))}
        </div>
      </div>
    );
  }

  /* ────────────────────────── RENDER ────────────────────────── */
  return (
    <div className="space-y-6 page-enter">
      {/* ── Header ── */}
      <div className="flex justify-between items-start flex-wrap gap-4">
        <div>
          <h1 className="text-3xl font-bold mb-1">🔧 Debug Dashboard</h1>
          <p className="text-soft text-sm">
            Real-time request monitoring &amp; service health
          </p>
        </div>
        <div className="flex items-center gap-2">
          <button
            onClick={() => setAutoRefresh((p) => !p)}
            className={`px-3 py-1.5 rounded-md text-xs font-semibold transition border ${
              autoRefresh
                ? "bg-green-500/15 text-green-400 border-green-500/30"
                : "bg-[var(--bg-soft)] text-muted border-[var(--line)]"
            }`}
          >
            {autoRefresh
              ? pageHidden
                ? "⏱ Auto (Hidden)"
                : `⏱ Auto${nextRefreshInSeconds != null ? ` (${nextRefreshInSeconds}s)` : ""}`
              : "⏸ Paused"}
          </button>
          <button onClick={handleManualRefresh} className="btn-muted">
            ↻ Refresh
          </button>
        </div>
      </div>

      {/* ── Summary KPIs ── */}
      <div className="grid grid-cols-2 md:grid-cols-5 gap-3">
        <KpiCard label="Uptime" value={health?.uptime_human ?? "—"} icon="⏱" />
        <KpiCard
          label="Total Requests"
          value={stats?.total_requests.toLocaleString() ?? "0"}
          icon="📡"
        />
        <KpiCard
          label="Errors"
          value={stats?.total_errors.toLocaleString() ?? "0"}
          icon="❌"
          accent={stats && stats.total_errors > 0 ? "danger" : undefined}
        />
        <KpiCard
          label="Warnings"
          value={stats?.total_warnings.toLocaleString() ?? "0"}
          icon="⚠️"
          accent={stats && stats.total_warnings > 0 ? "warning" : undefined}
        />
        <KpiCard
          label="Error Rate"
          value={`${stats?.error_rate ?? 0}%`}
          icon="📊"
          accent={
            stats && stats.error_rate > 5
              ? "danger"
              : stats && stats.error_rate > 1
                ? "warning"
                : undefined
          }
        />
      </div>

      {/* ── Service Health Row ── */}
      {health && (
        <div className="grid grid-cols-2 md:grid-cols-5 gap-3">
          {health.services.map((s) => (
            <div
              key={s.name}
              className={`rounded-xl border p-3 ${healthColor(s.status)}`}
            >
              <div className="flex items-center gap-2 mb-1">
                <span
                  className={`w-2.5 h-2.5 rounded-full inline-block ${healthDot(s.status)}`}
                />
                <span className="text-xs font-semibold text-white truncate">
                  {s.name}
                </span>
              </div>
              <p className="text-[10px] text-muted truncate">{s.detail}</p>
              {s.latency_ms !== null && (
                <p className="text-[10px] text-muted mt-0.5 mono">
                  {s.latency_ms}ms
                </p>
              )}
            </div>
          ))}
        </div>
      )}

      {/* ── Tab Switcher ── */}
      <div className="flex bg-[var(--bg-soft)] rounded-lg p-1 w-fit">
        {(
          [
            ["logs", "Request Logs"],
            ["endpoints", "Endpoint Stats"],
            ["health", "Health Details"],
          ] as const
        ).map(([key, label]) => (
          <button
            key={key}
            onClick={() => setActiveTab(key)}
            className={`px-4 py-1.5 rounded-md text-xs font-semibold transition ${
              activeTab === key
                ? "bg-[var(--accent-soft)] text-[var(--accent)]"
                : "text-muted hover:text-white"
            }`}
          >
            {label}
          </button>
        ))}
      </div>

      {/* ━━━━━━━━━━ TAB: Request Logs ━━━━━━━━━━ */}
      {activeTab === "logs" && (
        <div className="surface-panel p-4 space-y-3">
          {/* Filters */}
          <div className="flex flex-wrap items-center gap-2">
            {(["all", "error", "warning", "info"] as const).map((lv) => (
              <button
                key={lv}
                onClick={() => setLevelFilter(lv)}
                className={`px-3 py-1 rounded-md text-xs font-semibold transition border ${
                  levelFilter === lv
                    ? lv === "error"
                      ? "bg-red-500/15 text-red-400 border-red-500/30"
                      : lv === "warning"
                        ? "bg-yellow-500/15 text-yellow-400 border-yellow-500/30"
                        : lv === "info"
                          ? "bg-green-500/15 text-green-400 border-green-500/30"
                          : "bg-[var(--accent-soft)] text-[var(--accent)] border-[var(--accent)]/30"
                    : "bg-[var(--bg-elevated)] text-muted border-[var(--line)]"
                }`}
              >
                {lv === "all"
                  ? `All (${totalLogs})`
                  : lv.charAt(0).toUpperCase() + lv.slice(1)}
              </button>
            ))}

            <input
              type="text"
              placeholder="Filter by path…"
              value={pathFilter}
              onChange={(e) => setPathFilter(e.target.value)}
              className="input-theme text-xs px-3 py-1.5 w-48"
            />

            <button
              onClick={handleClearLogs}
              className="ml-auto btn-danger text-xs px-3 py-1"
            >
              🗑 Clear Logs
            </button>
          </div>

          {/* Log table */}
          <div className="overflow-x-auto max-h-[520px] overflow-y-auto scroll-soft">
            <table className="table-theme w-full text-xs">
              <thead>
                <tr>
                  <th className="w-20">Time</th>
                  <th className="w-16">Method</th>
                  <th>Path</th>
                  <th className="w-16">Status</th>
                  <th className="w-20">Duration</th>
                  <th className="w-16">Level</th>
                  <th className="w-8"></th>
                </tr>
              </thead>
              <tbody>
                {logs.length === 0 && (
                  <tr>
                    <td colSpan={7} className="text-center text-muted py-8">
                      No log entries{" "}
                      {levelFilter !== "all"
                        ? `matching "${levelFilter}"`
                        : "yet"}
                    </td>
                  </tr>
                )}
                {logs.map((entry) => (
                  <LogRow
                    key={entry.request_id + entry.timestamp}
                    entry={entry}
                    expanded={expandedRow === entry.request_id}
                    onToggle={() =>
                      setExpandedRow(
                        expandedRow === entry.request_id
                          ? null
                          : entry.request_id,
                      )
                    }
                    fmtTime={fmtTime}
                    methodColor={methodColor}
                    statusColor={statusColor}
                    levelBadge={levelBadge}
                    durationColor={durationColor}
                  />
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}

      {/* ━━━━━━━━━━ TAB: Endpoint Stats ━━━━━━━━━━ */}
      {activeTab === "endpoints" && (
        <div className="surface-panel p-4 overflow-x-auto">
          <table className="table-theme w-full text-xs">
            <thead>
              <tr>
                <th>Endpoint</th>
                <th
                  className="cursor-pointer select-none"
                  onClick={() => handleSort("call_count")}
                >
                  Calls{sortIcon("call_count")}
                </th>
                <th
                  className="cursor-pointer select-none"
                  onClick={() => handleSort("error_count")}
                >
                  Errors{sortIcon("error_count")}
                </th>
                <th
                  className="cursor-pointer select-none"
                  onClick={() => handleSort("warning_count")}
                >
                  Warnings{sortIcon("warning_count")}
                </th>
                <th
                  className="cursor-pointer select-none"
                  onClick={() => handleSort("avg_duration_ms")}
                >
                  Avg (ms){sortIcon("avg_duration_ms")}
                </th>
                <th
                  className="cursor-pointer select-none"
                  onClick={() => handleSort("min_duration_ms")}
                >
                  Min (ms){sortIcon("min_duration_ms")}
                </th>
                <th
                  className="cursor-pointer select-none"
                  onClick={() => handleSort("max_duration_ms")}
                >
                  Max (ms){sortIcon("max_duration_ms")}
                </th>
                <th>Last Called</th>
                <th>Last Status</th>
              </tr>
            </thead>
            <tbody>
              {sortedEndpoints.length === 0 && (
                <tr>
                  <td colSpan={9} className="text-center text-muted py-8">
                    No endpoint data yet — make some API calls first
                  </td>
                </tr>
              )}
              {sortedEndpoints.map((ep) => (
                <tr key={`${ep.method} ${ep.path}`}>
                  <td>
                    <span
                      className={`inline-block px-1.5 py-0.5 rounded text-[10px] font-bold mr-2 ${methodColor(ep.method)}`}
                    >
                      {ep.method}
                    </span>
                    <span className="mono">{ep.path}</span>
                  </td>
                  <td className="mono font-semibold">
                    {ep.call_count.toLocaleString()}
                  </td>
                  <td
                    className={`mono font-semibold ${ep.error_count > 0 ? "text-red-400" : "text-muted"}`}
                  >
                    {ep.error_count}
                  </td>
                  <td
                    className={`mono font-semibold ${ep.warning_count > 0 ? "text-yellow-400" : "text-muted"}`}
                  >
                    {ep.warning_count}
                  </td>
                  <td className={`mono ${durationColor(ep.avg_duration_ms)}`}>
                    {ep.avg_duration_ms.toFixed(1)}
                  </td>
                  <td className="mono text-muted">
                    {ep.min_duration_ms.toFixed(1)}
                  </td>
                  <td className={`mono ${durationColor(ep.max_duration_ms)}`}>
                    {ep.max_duration_ms.toFixed(1)}
                  </td>
                  <td className="text-muted">{fmtTime(ep.last_called)}</td>
                  <td
                    className={`mono font-bold ${statusColor(ep.last_status)}`}
                  >
                    {ep.last_status}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {/* ━━━━━━━━━━ TAB: Health Details ━━━━━━━━━━ */}
      {activeTab === "health" && health && (
        <div className="space-y-4">
          {/* Server Info */}
          <div className="surface-panel p-4">
            <h3 className="text-sm font-semibold mb-3 text-muted uppercase tracking-wider">
              Server Info
            </h3>
            <div className="grid grid-cols-2 md:grid-cols-4 gap-4 text-sm">
              <div>
                <p className="text-muted text-xs">Uptime</p>
                <p className="font-bold text-white">{health.uptime_human}</p>
              </div>
              <div>
                <p className="text-muted text-xs">Server Time (UTC)</p>
                <p className="font-bold text-white mono text-xs">
                  {new Date(health.server_time).toLocaleString()}
                </p>
              </div>
              <div>
                <p className="text-muted text-xs">Python Version</p>
                <p className="font-bold text-white mono">
                  {health.python_version}
                </p>
              </div>
              <div>
                <p className="text-muted text-xs">Endpoints Tracked</p>
                <p className="font-bold text-white">
                  {stats?.endpoints.length ?? 0}
                </p>
              </div>
            </div>
          </div>

          {/* Detailed service cards */}
          <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
            {health.services.map((svc) => (
              <div
                key={svc.name}
                className={`rounded-xl border p-4 ${healthColor(svc.status)}`}
              >
                <div className="flex items-center justify-between mb-3">
                  <div className="flex items-center gap-2">
                    <span
                      className={`w-3 h-3 rounded-full ${healthDot(svc.status)}`}
                    />
                    <span className="font-semibold text-white">{svc.name}</span>
                  </div>
                  <span
                    className={`chip text-xs ${
                      svc.status === "healthy"
                        ? "chip-success"
                        : svc.status === "unhealthy"
                          ? "chip-danger"
                          : "chip-warning"
                    }`}
                  >
                    {svc.status}
                  </span>
                </div>
                <p className="text-xs text-muted mb-1">{svc.detail}</p>
                {svc.latency_ms !== null && (
                  <p className="text-xs mono text-muted">
                    Latency: {svc.latency_ms}ms
                  </p>
                )}
              </div>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}

/* ═══════ Sub-components ═══════ */

function KpiCard({
  label,
  value,
  icon,
  accent,
}: {
  label: string;
  value: string;
  icon: string;
  accent?: "danger" | "warning";
}) {
  const border =
    accent === "danger"
      ? "border-red-500/30"
      : accent === "warning"
        ? "border-yellow-500/30"
        : "border-[var(--line)]";
  const valColor =
    accent === "danger"
      ? "text-red-400"
      : accent === "warning"
        ? "text-yellow-400"
        : "text-white";
  return (
    <div className={`rounded-xl border p-3.5 bg-[var(--bg-soft)] ${border}`}>
      <div className="flex items-center gap-2 mb-1">
        <span className="text-base">{icon}</span>
        <span className="text-muted text-xs font-medium">{label}</span>
      </div>
      <p className={`text-lg font-bold mono ${valColor}`}>{value}</p>
    </div>
  );
}

function LogRow({
  entry,
  expanded,
  onToggle,
  fmtTime,
  methodColor,
  statusColor,
  levelBadge,
  durationColor,
}: {
  entry: LogEntry;
  expanded: boolean;
  onToggle: () => void;
  fmtTime: (iso: string) => string;
  methodColor: (m: string) => string;
  statusColor: (c: number) => string;
  levelBadge: (l: string) => string;
  durationColor: (ms: number) => string;
}) {
  return (
    <>
      <tr
        className={`cursor-pointer transition ${
          entry.level === "error"
            ? "bg-red-500/5 hover:bg-red-500/10"
            : entry.level === "warning"
              ? "bg-yellow-500/5 hover:bg-yellow-500/10"
              : "hover:bg-[var(--bg-elevated)]"
        }`}
        onClick={onToggle}
      >
        <td className="text-muted mono">{fmtTime(entry.timestamp)}</td>
        <td>
          <span
            className={`inline-block px-1.5 py-0.5 rounded text-[10px] font-bold ${methodColor(entry.method)}`}
          >
            {entry.method}
          </span>
        </td>
        <td className="mono truncate max-w-[300px]" title={entry.path}>
          {entry.path}
          {entry.query && (
            <span className="text-muted ml-1">?{entry.query}</span>
          )}
        </td>
        <td className={`mono font-bold ${statusColor(entry.status_code)}`}>
          {entry.status_code}
        </td>
        <td className={`mono ${durationColor(entry.duration_ms)}`}>
          {entry.duration_ms.toFixed(0)}ms
        </td>
        <td>
          <span className={`chip text-[10px] ${levelBadge(entry.level)}`}>
            {entry.level}
          </span>
        </td>
        <td>
          {entry.error_detail && (
            <span className="text-muted">{expanded ? "▾" : "▸"}</span>
          )}
        </td>
      </tr>
      {expanded && entry.error_detail && (
        <tr>
          <td colSpan={7} className="p-0">
            <div className="bg-[var(--bg-elevated)] border-t border-b border-[var(--line)] p-3">
              <div className="flex items-center gap-2 mb-2">
                <span className="text-xs font-semibold text-muted">
                  Error Detail
                </span>
                <span className="text-[10px] text-muted mono">
                  ID: {entry.request_id}
                </span>
                <span className="text-[10px] text-muted">
                  IP: {entry.client_ip}
                </span>
              </div>
              <pre className="text-[11px] text-red-300 whitespace-pre-wrap leading-relaxed max-h-60 overflow-y-auto scroll-soft mono bg-[var(--bg-base)] rounded p-3">
                {entry.error_detail}
              </pre>
              {entry.user_agent && (
                <p className="text-[10px] text-muted mt-2 truncate">
                  UA: {entry.user_agent}
                </p>
              )}
            </div>
          </td>
        </tr>
      )}
    </>
  );
}
