import { useEffect, useState, useCallback, useMemo, useRef } from "react";
import { TableVirtuoso } from "react-virtuoso";
import {
  tradesService,
  LeaderboardEntry,
  LeaderboardResponse,
  NotificationFollowedTrader,
  TraderProfile,
} from "../services/tradesService";
import { analysisService } from "../services/analysisService";
import { getApiErrorMessage, sanitizeAIError } from "../utils/apiError";
import { buildPolymarketProfileUrl } from "../utils/urlSafety";
import TraderAnalysisTable from "./TraderAnalysisTable";
import {
  MockCopyTrader,
  hasMockCopyTrader,
  loadMockCopyTraders,
  removeMockCopyTrader,
  saveMockCopyTraders,
  subscribeMockCopyTraders,
  upsertMockCopyTrader,
} from "../utils/mockCopyTraders";
import { useRafBufferedText } from "../hooks/useRafBufferedText";
import { useRequireAuth } from "../hooks/useRequireAuth";

type Period = "24h" | "7d" | "30d" | "all_time";
const PERIODS: { value: Period; label: string }[] = [
  { value: "24h", label: "24 H" },
  { value: "7d", label: "7 D" },
  { value: "30d", label: "30 D" },
  { value: "all_time", label: "All Time" },
];

export default function Leaderboard() {
  const { requireAuth } = useRequireAuth();
  const [entries, setEntries] = useState<LeaderboardEntry[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [updatedAt, setUpdatedAt] = useState<string>("");
  const [period, setPeriod] = useState<Period>("all_time");
  const [traderLimit, setTraderLimit] = useState<number>(100);
  const [selectedTrader, setSelectedTrader] = useState<TraderProfile | null>(
    null,
  );
  const [profileLoading, setProfileLoading] = useState(false);
  const [followingSet, setFollowingSet] = useState<Set<string>>(new Set());
  const [notificationFollowingSet, setNotificationFollowingSet] = useState<
    Set<string>
  >(new Set());
  const [notificationEmailByWallet, setNotificationEmailByWallet] = useState<
    Record<string, boolean>
  >({});
  const [mockTraders, setMockTraders] = useState<MockCopyTrader[]>(() =>
    loadMockCopyTraders(),
  );
  const [popupActionMessage, setPopupActionMessage] = useState<string | null>(
    null,
  );
  const [popupActionError, setPopupActionError] = useState<string | null>(null);
  const {
    text: analysisText,
    append: appendAnalysisText,
    reset: resetAnalysisText,
    flushNow: flushAnalysisTextNow,
  } = useRafBufferedText();
  const [analysisLoading, setAnalysisLoading] = useState(false);
  const [analysisError, setAnalysisError] = useState<string | null>(null);
  const analysisAbortRef = useRef<AbortController | null>(null);
  /** Pixel position of the click that opened the profile popup */
  const [popupClickY, setPopupClickY] = useState(0);

  // ── Column sorting ──
  type SortKey =
    | "rank"
    | "profit_loss"
    | "volume"
    | "markets_traded"
    | "win_rate"
    | "positions_value";
  const [sortKey, setSortKey] = useState<SortKey>("rank");
  const [sortAsc, setSortAsc] = useState(true);

  const handleSort = (key: SortKey) => {
    if (sortKey === key) {
      setSortAsc((prev) => !prev);
    } else {
      setSortKey(key);
      setSortAsc(key === "rank"); // rank ascending by default, everything else descending
    }
  };

  const sortedEntries = useMemo(() => {
    return [...entries].sort((a, b) => {
      let av: number;
      let bv: number;
      switch (sortKey) {
        case "profit_loss":
          av = a.profit_loss;
          bv = b.profit_loss;
          break;
        case "volume":
          av = a.volume;
          bv = b.volume;
          break;
        case "markets_traded":
          av = a.markets_traded;
          bv = b.markets_traded;
          break;
        case "win_rate":
          av = a.win_rate ?? -1;
          bv = b.win_rate ?? -1;
          break;
        case "positions_value":
          av = a.positions_value;
          bv = b.positions_value;
          break;
        default: // rank
          av = a.rank;
          bv = b.rank;
      }
      return sortAsc ? av - bv : bv - av;
    });
  }, [entries, sortAsc, sortKey]);

  const SortIcon = ({ col }: { col: SortKey }) => {
    if (sortKey !== col) return <span className="ml-1 text-muted/40">↕</span>;
    return (
      <span className="ml-1 text-[var(--accent)]">{sortAsc ? "↑" : "↓"}</span>
    );
  };

  const fetchLeaderboard = useCallback(
    async (retries = 2) => {
      setLoading(true);
      setError(null);
      for (let attempt = 0; attempt <= retries; attempt++) {
        try {
          const [leaderboardResult, notificationFollowingResult] =
            await Promise.allSettled([
              tradesService.getLeaderboard(traderLimit, period),
              tradesService.getNotificationFollowing(),
            ]);
          if (leaderboardResult.status !== "fulfilled") {
            throw leaderboardResult.reason;
          }
          const result: LeaderboardResponse = leaderboardResult.value;
          setEntries(result.entries);
          setUpdatedAt(result.updated_at);
          setFollowingSet(
            new Set(
              result.entries
                .filter((e) => e.is_followed)
                .map((e) => e.address.toLowerCase()),
            ),
          );
          setNotificationFollowingSet(
            new Set(
              result.entries
                .filter((e) => e.is_notification_followed)
                .map((e) => e.address.toLowerCase()),
            ),
          );
          if (notificationFollowingResult.status === "fulfilled") {
            const emailByWallet: Record<string, boolean> = {};
            notificationFollowingResult.value.forEach(
              (row: NotificationFollowedTrader) => {
                emailByWallet[row.trader_wallet.toLowerCase()] =
                  !!row.email_enabled;
              },
            );
            setNotificationEmailByWallet(emailByWallet);
          }
          setLoading(false);
          return;
        } catch (err: unknown) {
          if (attempt < retries) {
            // Wait 1s before retry
            await new Promise((r) => setTimeout(r, 1000));
            continue;
          }
          setError(getApiErrorMessage(err, "Failed to load leaderboard"));
        }
      }
      setLoading(false);
    },
    [period, traderLimit],
  );

  useEffect(() => {
    fetchLeaderboard();
    // Reset sort to default rank when period changes
    setSortKey("rank");
    setSortAsc(true);
  }, [fetchLeaderboard]);

  useEffect(() => subscribeMockCopyTraders(setMockTraders), []);

  const handleFollow = async (wallet: string) => {
    try {
      await tradesService.followTrader(wallet);
      setFollowingSet((prev) => new Set(prev).add(wallet.toLowerCase()));
      setPopupActionError(null);
      setPopupActionMessage(`Copy trade enabled for ${shortAddress(wallet)}.`);
    } catch (err: unknown) {
      setError(getApiErrorMessage(err, "Failed to follow trader"));
      setPopupActionMessage(null);
      setPopupActionError(
        getApiErrorMessage(err, "Failed to enable copy trade"),
      );
    }
  };

  const handleUnfollow = async (wallet: string) => {
    try {
      await tradesService.unfollowTrader(wallet);
      setFollowingSet((prev) => {
        const next = new Set(prev);
        next.delete(wallet.toLowerCase());
        return next;
      });
      setPopupActionError(null);
      setPopupActionMessage(`Copy trade disabled for ${shortAddress(wallet)}.`);
    } catch (err: unknown) {
      setError(getApiErrorMessage(err, "Failed to unfollow trader"));
      setPopupActionMessage(null);
      setPopupActionError(
        getApiErrorMessage(err, "Failed to disable copy trade"),
      );
    }
  };

  const handleNotificationFollow = async (wallet: string) => {
    try {
      const row = await tradesService.followNotifications(wallet, {
        feed_enabled: true,
      });
      const normalized = row.trader_wallet.toLowerCase();
      setNotificationFollowingSet((prev) => new Set(prev).add(normalized));
      setNotificationEmailByWallet((prev) => ({
        ...prev,
        [normalized]: !!row.email_enabled,
      }));
      setPopupActionError(null);
      setPopupActionMessage(
        `Now following ${shortAddress(normalized)} for opened/closed notifications.`,
      );
    } catch (err: unknown) {
      setPopupActionMessage(null);
      setPopupActionError(
        getApiErrorMessage(err, "Failed to follow trader notifications"),
      );
    }
  };

  const handleNotificationUnfollow = async (wallet: string) => {
    try {
      await tradesService.unfollowNotifications(wallet);
      const normalized = wallet.toLowerCase();
      setNotificationFollowingSet((prev) => {
        const next = new Set(prev);
        next.delete(normalized);
        return next;
      });
      setNotificationEmailByWallet((prev) => {
        const next = { ...prev };
        delete next[normalized];
        return next;
      });
      setPopupActionError(null);
      setPopupActionMessage(
        `Stopped notification follow for ${shortAddress(normalized)}.`,
      );
    } catch (err: unknown) {
      setPopupActionMessage(null);
      setPopupActionError(
        getApiErrorMessage(err, "Failed to unfollow trader notifications"),
      );
    }
  };

  const handleNotificationEmailToggle = async (
    wallet: string,
    emailEnabled: boolean,
  ) => {
    try {
      const row = await tradesService.followNotifications(wallet, {
        feed_enabled: true,
        email_enabled: emailEnabled,
      });
      const normalized = row.trader_wallet.toLowerCase();
      setNotificationFollowingSet((prev) => new Set(prev).add(normalized));
      setNotificationEmailByWallet((prev) => ({
        ...prev,
        [normalized]: !!row.email_enabled,
      }));
      setPopupActionError(null);
      setPopupActionMessage(
        emailEnabled
          ? "Email notifications enabled for this trader."
          : "Email notifications disabled for this trader.",
      );
    } catch (err: unknown) {
      setPopupActionMessage(null);
      setPopupActionError(
        getApiErrorMessage(err, "Failed to update email notification setting"),
      );
    }
  };

  const handleToggleMockCopy = (wallet: string) => {
    const normalized = wallet.toLowerCase();
    setMockTraders((prev) => {
      const exists = hasMockCopyTrader(prev, normalized);
      const next = exists
        ? removeMockCopyTrader(prev, normalized)
        : upsertMockCopyTrader(prev, normalized);
      saveMockCopyTraders(next);
      setPopupActionError(null);
      setPopupActionMessage(
        exists
          ? `Removed ${shortAddress(normalized)} from mock copy trading.`
          : `Added ${shortAddress(normalized)} to mock copy trading ($10,000).`,
      );
      return next;
    });
  };

  const openProfile = async (wallet: string, e?: React.MouseEvent) => {
    if (e) {
      setPopupClickY(e.clientY);
    }
    setPopupActionMessage(null);
    setPopupActionError(null);
    setProfileLoading(true);
    resetAnalysisText();
    setAnalysisError(null);
    setAnalysisLoading(false);
    try {
      const profile = await tradesService.getTraderProfile(wallet);
      setSelectedTrader(profile);
    } catch {
      setSelectedTrader(null);
    } finally {
      setProfileLoading(false);
    }
  };

  const closeProfile = () => {
    if (analysisAbortRef.current) {
      analysisAbortRef.current.abort();
      analysisAbortRef.current = null;
    }
    setSelectedTrader(null);
    resetAnalysisText();
    setAnalysisError(null);
    setAnalysisLoading(false);
    setPopupActionMessage(null);
    setPopupActionError(null);
  };

  const handleAnalyzeTrader = () => {
    if (!selectedTrader) return;
    setAnalysisLoading(true);
    resetAnalysisText();
    setAnalysisError(null);

    // Find matching leaderboard entry for stats
    const entry = entries.find(
      (e) =>
        e.address.toLowerCase() === selectedTrader.wallet_address.toLowerCase(),
    );

    // Use real trade_stats from backend if available
    const ts = selectedTrader.trade_stats;

    const controller = analysisService.streamTraderAnalysis(
      {
        wallet_address: selectedTrader.wallet_address,
        display_name: selectedTrader.display_name || undefined,
        total_pnl: selectedTrader.profit_loss || entry?.profit_loss || 0,
        // Backend will fetch real trades and compute real win rate
        // (these are just hints; the backend overrides them with real data)
        win_rate: ts?.win_rate ?? selectedTrader.win_rate ?? 0,
        trade_count:
          ts?.total_trades ?? selectedTrader.recent_trades?.length ?? 0,
        markets_traded:
          ts?.unique_markets ??
          selectedTrader.markets_traded ??
          entry?.markets_traded ??
          0,
        // No need to send recent_trades_json — backend fetches all trades itself
        recent_trades_json: "[]",
      },
      {
        onChunk: (text) => appendAnalysisText(text),
        onDone: () => {
          flushAnalysisTextNow();
          setAnalysisLoading(false);
        },
        onError: (err) => {
          flushAnalysisTextNow();
          setAnalysisError(sanitizeAIError(err));
          setAnalysisLoading(false);
        },
      },
    );
    analysisAbortRef.current = controller;
  };

  const pnlForPeriod = (e: LeaderboardEntry) => {
    // profit_loss is already the period-specific value from the backend
    return e.profit_loss;
  };

  const formatUSD = (v: number) => {
    const abs = Math.abs(v);
    if (abs >= 1_000_000) return `$${(v / 1_000_000).toFixed(1)}M`;
    if (abs >= 1_000) return `$${(v / 1_000).toFixed(1)}K`;
    return `$${v.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
  };

  const shortAddress = (addr: string) =>
    addr.length >= 10 ? `${addr.slice(0, 6)}...${addr.slice(-4)}` : addr;
  const tableHeight = useMemo(() => {
    const estimatedRowHeight = 56;
    const estimatedHeaderHeight = 48;
    const minHeight = 220;
    const maxHeight = 560;
    const estimated =
      sortedEntries.length * estimatedRowHeight + estimatedHeaderHeight;
    return Math.max(minHeight, Math.min(maxHeight, estimated));
  }, [sortedEntries.length]);

  if (loading) {
    return (
      <div className="space-y-6">
        <h1 className="text-3xl font-bold">Leaderboard</h1>
        <p className="text-soft">Loading top traders...</p>
        <div className="space-y-3">
          {[1, 2, 3, 4, 5].map((i) => (
            <div key={i} className="surface-panel p-4 animate-pulse h-16" />
          ))}
        </div>
      </div>
    );
  }

  return (
    <div className="space-y-6">
      {/* Header */}
      <div className="flex justify-between items-start flex-wrap gap-4">
        <div>
          <h1 className="text-3xl font-bold mb-2">Leaderboard</h1>
          <p className="text-soft">
            Top {entries.length} traders on Polymarket
            {loading && entries.length === 0 && (
              <span className="text-muted ml-1">
                (loading up to {traderLimit}…)
              </span>
            )}
            {updatedAt && (
              <span className="text-muted ml-2">
                · Updated {new Date(updatedAt).toLocaleTimeString()}
              </span>
            )}
          </p>
        </div>
        <div className="flex items-center gap-2">
          {/* Trader count selector */}
          <select
            value={traderLimit}
            onChange={(e) => setTraderLimit(Number(e.target.value))}
            className="bg-[var(--bg-soft)] text-[var(--text-primary)] border border-[var(--border)] rounded-lg px-2 py-1.5 text-xs font-semibold cursor-pointer"
          >
            {[25, 50, 100, 250, 500, 1000].map((n) => (
              <option key={n} value={n}>
                Top {n}
              </option>
            ))}
          </select>
          {/* Period tabs */}
          <div className="flex bg-[var(--bg-soft)] rounded-lg p-1">
            {PERIODS.map((p) => (
              <button
                key={p.value}
                onClick={() => setPeriod(p.value)}
                className={`px-3 py-1.5 rounded-md text-xs font-semibold transition ${
                  period === p.value
                    ? "bg-[var(--accent-soft)] text-[var(--accent)]"
                    : "text-soft hover:text-white"
                }`}
              >
                {p.label}
              </button>
            ))}
          </div>
          <button onClick={() => fetchLeaderboard()} className="btn-muted">
            Refresh
          </button>
        </div>
      </div>

      {error && <div className="p-4 alert-error rounded text-sm">{error}</div>}

      {/* Podium – Top 3 */}
      {entries.length >= 3 && (
        <div className="grid grid-cols-3 gap-4">
          {[entries[1], entries[0], entries[2]].map((e, i) => {
            const podiumOrder = [2, 1, 3];
            const colors = [
              "border-[#7f8fb1] bg-[#7f8fb11f]",
              "border-[var(--accent)] bg-[var(--accent-soft)]",
              "border-[#d68b3d] bg-[#d68b3d1f]",
            ];
            const heights = ["pt-6", "pt-0", "pt-10"];
            const pnl = pnlForPeriod(e);
            const isFollowed = followingSet.has(e.address.toLowerCase());
            return (
              <div
                key={e.rank}
                className={`${heights[i]} flex flex-col items-center`}
              >
                <div
                  className={`w-full border-2 ${colors[i]} rounded-lg p-4 text-center`}
                >
                  <div className="text-2xl font-bold mb-1">
                    {podiumOrder[i] === 1
                      ? "🥇"
                      : podiumOrder[i] === 2
                        ? "🥈"
                        : "🥉"}
                  </div>
                  <button
                    onClick={(ev) => openProfile(e.address, ev)}
                    className="font-semibold text-white text-sm truncate hover:underline cursor-pointer block mx-auto"
                  >
                    {e.display_name || shortAddress(e.address)}
                  </button>
                  <p
                    className={`text-lg font-bold mt-1 ${pnl >= 0 ? "status-good" : "status-bad"}`}
                  >
                    {pnl >= 0 ? "+" : ""}
                    {formatUSD(pnl)}
                  </p>
                  <p className="text-soft text-xs mt-1">
                    Vol: {formatUSD(e.volume)}
                  </p>
                  <button
                    onClick={() =>
                      requireAuth(() =>
                        isFollowed
                          ? handleUnfollow(e.address)
                          : handleFollow(e.address),
                      )
                    }
                    className={`mt-2 px-3 py-1 rounded text-xs font-semibold transition ${
                      isFollowed
                        ? "bg-red-500/20 text-red-400 hover:bg-red-500/30"
                        : "bg-[var(--accent-soft)] text-[var(--accent)] hover:bg-[var(--accent)]/30"
                    }`}
                  >
                    {isFollowed ? "Unfollow" : "Follow"}
                  </button>
                </div>
              </div>
            );
          })}
        </div>
      )}

      {/* Full Table */}
      {entries.length > 0 ? (
        <div className="surface-panel overflow-hidden">
          <TableVirtuoso
            data={sortedEntries}
            style={{ height: tableHeight }}
            className="scroll-soft"
            increaseViewportBy={{ top: 240, bottom: 360 }}
            computeItemKey={(_, entry) => entry.address.toLowerCase()}
            components={{
              Table: (props) => (
                <table {...props} className="table-theme text-sm w-full" />
              ),
              TableRow: (props) => <tr {...props} className="transition" />,
            }}
            fixedHeaderContent={() => (
              <tr>
                <th
                  className="text-center p-3 w-12 cursor-pointer select-none hover:text-[var(--accent)] transition"
                  onClick={() => handleSort("rank")}
                >
                  #<SortIcon col="rank" />
                </th>
                <th className="text-left p-3">Trader</th>
                <th
                  className="text-right p-3 cursor-pointer select-none hover:text-[var(--accent)] transition"
                  onClick={() => handleSort("profit_loss")}
                >
                  P&amp;L ({PERIODS.find((p) => p.value === period)?.label})
                  <SortIcon col="profit_loss" />
                </th>
                <th
                  className="text-right p-3 cursor-pointer select-none hover:text-[var(--accent)] transition"
                  onClick={() => handleSort("volume")}
                >
                  Volume
                  <SortIcon col="volume" />
                </th>
                <th
                  className="text-right p-3 cursor-pointer select-none hover:text-[var(--accent)] transition"
                  onClick={() => handleSort("markets_traded")}
                >
                  Markets
                  <SortIcon col="markets_traded" />
                </th>
                <th
                  className="text-right p-3 cursor-pointer select-none hover:text-[var(--accent)] transition"
                  onClick={() => handleSort("win_rate")}
                >
                  Win Rate
                  <SortIcon col="win_rate" />
                </th>
                <th
                  className="text-right p-3 cursor-pointer select-none hover:text-[var(--accent)] transition"
                  onClick={() => handleSort("positions_value")}
                >
                  Positions
                  <SortIcon col="positions_value" />
                </th>
                <th className="text-center p-3 w-20">Follow</th>
              </tr>
            )}
            itemContent={(_, entry) => {
              const pnl = pnlForPeriod(entry);
              const isFollowed = followingSet.has(entry.address.toLowerCase());
              return (
                <>
                  <td className="p-3 text-center text-soft font-medium">
                    {entry.rank <= 3 ? (
                      <span className="text-base">
                        {entry.rank === 1
                          ? "🥇"
                          : entry.rank === 2
                            ? "🥈"
                            : "🥉"}
                      </span>
                    ) : (
                      entry.rank
                    )}
                  </td>
                  <td className="p-3">
                    <button
                      onClick={(ev) => openProfile(entry.address, ev)}
                      className="text-left hover:underline"
                    >
                      <p className="text-white font-medium text-sm">
                        {entry.display_name || shortAddress(entry.address)}
                      </p>
                      {entry.display_name && (
                        <p className="text-muted text-xs mono">
                          {shortAddress(entry.address)}
                        </p>
                      )}
                    </button>
                  </td>
                  <td className="p-3 text-right mono">
                    <span className={pnl >= 0 ? "status-good" : "status-bad"}>
                      {pnl >= 0 ? "+" : ""}
                      {formatUSD(pnl)}
                    </span>
                  </td>
                  <td className="p-3 text-right text-soft mono">
                    {formatUSD(entry.volume)}
                  </td>
                  <td className="p-3 text-right text-soft">
                    {entry.markets_traded || "—"}
                  </td>
                  <td className="p-3 text-right">
                    {entry.win_rate != null && entry.win_rate > 0 ? (
                      <span
                        className={
                          entry.win_rate >= 60
                            ? "status-good"
                            : entry.win_rate >= 40
                              ? "status-warn"
                              : "status-bad"
                        }
                      >
                        {entry.win_rate.toFixed(0)}%
                      </span>
                    ) : (
                      <span className="text-muted">—</span>
                    )}
                  </td>
                  <td className="p-3 text-right text-soft mono">
                    {entry.positions_value > 0
                      ? formatUSD(entry.positions_value)
                      : "—"}
                  </td>
                  <td className="p-3 text-center">
                    <button
                      onClick={() =>
                        requireAuth(() =>
                          isFollowed
                            ? handleUnfollow(entry.address)
                            : handleFollow(entry.address),
                        )
                      }
                      className={`px-2 py-1 rounded text-xs font-semibold transition ${
                        isFollowed
                          ? "bg-red-500/20 text-red-400 hover:bg-red-500/30"
                          : "bg-[var(--accent-soft)] text-[var(--accent)] hover:bg-[var(--accent)]/30"
                      }`}
                    >
                      {isFollowed ? "Unfollow" : "Follow"}
                    </button>
                  </td>
                </>
              );
            }}
          />
        </div>
      ) : (
        <div className="text-center py-12 surface-panel">
          <p className="text-soft text-lg mb-2">
            No leaderboard data available
          </p>
          <p className="text-muted text-sm">
            Check back later — leaderboard data is fetched from Polymarket.
          </p>
        </div>
      )}

      {/* Trader Profile Modal */}
      {(selectedTrader || profileLoading) && (
        <div
          className="fixed inset-0 bg-black/60 z-50 overflow-y-auto"
          onClick={closeProfile}
        >
          <div
            className="surface-panel max-w-3xl w-full p-0 rounded-2xl mx-auto shadow-2xl shadow-black/40 border border-[var(--line-strong)]"
            style={{
              marginTop: `${Math.max(16, Math.min(popupClickY - 40, window.innerHeight - 200))}px`,
              marginBottom: "2rem",
            }}
            onClick={(e) => e.stopPropagation()}
          >
            {profileLoading ? (
              <div className="flex items-center justify-center py-16">
                <div className="animate-spin rounded-full h-10 w-10 border-t-2 border-b-2 border-[var(--accent)]" />
              </div>
            ) : selectedTrader ? (
              <>
                {/* Header */}
                <div
                  className="relative px-6 pt-6 pb-5 rounded-t-2xl"
                  style={{
                    background:
                      "linear-gradient(135deg, rgba(247,166,0,0.12) 0%, rgba(255,186,43,0.08) 50%, rgba(39,215,128,0.06) 100%)",
                  }}
                >
                  <button
                    onClick={closeProfile}
                    className="absolute top-4 right-4 w-8 h-8 flex items-center justify-center rounded-full bg-white/5 hover:bg-white/10 text-soft hover:text-white transition"
                  >
                    <svg
                      className="w-4 h-4"
                      fill="none"
                      viewBox="0 0 24 24"
                      stroke="currentColor"
                      strokeWidth={2}
                    >
                      <path
                        strokeLinecap="round"
                        strokeLinejoin="round"
                        d="M6 18L18 6M6 6l12 12"
                      />
                    </svg>
                  </button>
                  <div className="flex items-center gap-4">
                    <div className="w-14 h-14 rounded-full bg-gradient-to-br from-[var(--accent)] to-emerald-500 flex items-center justify-center text-black text-xl font-bold shadow-lg">
                      {(selectedTrader.display_name ||
                        selectedTrader.wallet_address)[0]?.toUpperCase()}
                    </div>
                    <div>
                      <h2 className="text-xl font-bold text-white">
                        {selectedTrader.display_name ||
                          shortAddress(selectedTrader.wallet_address)}
                      </h2>
                      <p className="text-muted text-xs mono mt-0.5">
                        {selectedTrader.wallet_address}
                      </p>
                    </div>
                  </div>
                </div>
                {/* Stats Grid */}
                <div className="px-6 pt-5 pb-4">
                  <div className="grid grid-cols-2 md:grid-cols-4 gap-3 mb-5">
                    <div className="rounded-xl p-3.5 border border-[var(--line)] bg-[var(--bg-soft)]">
                      <div className="flex items-center gap-2 mb-1.5">
                        <span className="w-6 h-6 rounded-md bg-[var(--accent-soft)] flex items-center justify-center text-[var(--accent)] text-xs">
                          $
                        </span>
                        <span className="text-muted text-xs font-medium">
                          Total P&L
                        </span>
                      </div>
                      <p
                        className={`text-lg font-bold ${selectedTrader.profit_loss >= 0 ? "status-good" : "status-bad"}`}
                      >
                        {formatUSD(selectedTrader.profit_loss)}
                      </p>
                    </div>
                    <div className="rounded-xl p-3.5 border border-[var(--line)] bg-[var(--bg-soft)]">
                      <div className="flex items-center gap-2 mb-1.5">
                        <span className="w-6 h-6 rounded-md bg-[var(--accent-soft)] flex items-center justify-center text-[var(--accent)] text-xs">
                          <svg
                            className="w-3.5 h-3.5"
                            fill="none"
                            viewBox="0 0 24 24"
                            stroke="currentColor"
                            strokeWidth={2}
                          >
                            <path
                              strokeLinecap="round"
                              strokeLinejoin="round"
                              d="M3 7v10a2 2 0 002 2h14a2 2 0 002-2V9a2 2 0 00-2-2h-6l-2-2H5a2 2 0 00-2 2z"
                            />
                          </svg>
                        </span>
                        <span className="text-muted text-xs font-medium">
                          Volume
                        </span>
                      </div>
                      <p className="text-lg font-bold text-white">
                        {formatUSD(selectedTrader.volume)}
                      </p>
                    </div>
                    <div className="rounded-xl p-3.5 border border-[var(--line)] bg-[var(--bg-soft)]">
                      <div className="flex items-center gap-2 mb-1.5">
                        <span className="w-6 h-6 rounded-md bg-[var(--accent-soft)] flex items-center justify-center text-[var(--accent)] text-xs">
                          <svg
                            className="w-3.5 h-3.5"
                            fill="none"
                            viewBox="0 0 24 24"
                            stroke="currentColor"
                            strokeWidth={2}
                          >
                            <path
                              strokeLinecap="round"
                              strokeLinejoin="round"
                              d="M4 6h16M4 10h16M4 14h16M4 18h16"
                            />
                          </svg>
                        </span>
                        <span className="text-muted text-xs font-medium">
                          Markets
                        </span>
                      </div>
                      <p className="text-lg font-bold text-white">
                        {selectedTrader.trade_stats?.unique_markets ||
                          selectedTrader.markets_traded ||
                          "—"}
                      </p>
                    </div>
                    <div className="rounded-xl p-3.5 border border-[var(--line)] bg-[var(--bg-soft)]">
                      <div className="flex items-center gap-2 mb-1.5">
                        <span className="w-6 h-6 rounded-md bg-amber-500/15 flex items-center justify-center text-amber-400 text-xs">
                          <svg
                            className="w-3.5 h-3.5"
                            fill="none"
                            viewBox="0 0 24 24"
                            stroke="currentColor"
                            strokeWidth={2}
                          >
                            <path
                              strokeLinecap="round"
                              strokeLinejoin="round"
                              d="M9 12l2 2 4-4m6 2a9 9 0 11-18 0 9 9 0 0118 0z"
                            />
                          </svg>
                        </span>
                        <span className="text-muted text-xs font-medium">
                          Win Rate
                        </span>
                      </div>
                      <p className="text-lg font-bold text-white">
                        {(() => {
                          const wr =
                            selectedTrader.trade_stats?.win_rate ??
                            selectedTrader.win_rate;
                          return wr != null && wr > 0
                            ? `${wr.toFixed(1)}%`
                            : "—";
                        })()}
                      </p>
                    </div>
                  </div>

                  {/* Trade Activity Summary (from real data) */}
                  {selectedTrader.trade_stats && (
                    <div className="mb-4 p-3 rounded-xl border border-[var(--line)] bg-[var(--bg-soft)]">
                      <div className="flex items-center gap-2 mb-2">
                        <span className="text-xs font-semibold text-muted uppercase tracking-wider">
                          Trade Activity
                        </span>
                        <span className="text-[10px] px-1.5 py-0.5 rounded bg-[var(--accent-soft)] text-[var(--accent)] font-medium">
                          Live Data
                        </span>
                      </div>
                      <div className="grid grid-cols-3 gap-3 text-xs">
                        <div>
                          <p className="text-muted">Total Trades</p>
                          <p className="font-bold text-white">
                            {selectedTrader.trade_stats.total_trades}
                          </p>
                        </div>
                        <div>
                          <p className="text-muted">W / L</p>
                          <p className="font-bold">
                            <span className="status-good">
                              {selectedTrader.trade_stats.winning_trades}
                            </span>
                            {" / "}
                            <span className="status-bad">
                              {selectedTrader.trade_stats.losing_trades}
                            </span>
                          </p>
                        </div>
                        <div>
                          <p className="text-muted">Avg Trade</p>
                          <p className="font-bold text-white">
                            {formatUSD(
                              selectedTrader.trade_stats.avg_trade_size,
                            )}
                          </p>
                        </div>
                        {selectedTrader.trade_stats.trade_frequency && (
                          <div>
                            <p className="text-muted">Frequency</p>
                            <p className="font-bold text-white">
                              {selectedTrader.trade_stats.trade_frequency}
                            </p>
                          </div>
                        )}
                        <div>
                          <p className="text-muted">Active Days</p>
                          <p className="font-bold text-white">
                            {selectedTrader.trade_stats.active_days}
                          </p>
                        </div>
                        <div>
                          <p className="text-muted">Largest Trade</p>
                          <p className="font-bold text-white">
                            {formatUSD(
                              selectedTrader.trade_stats.largest_trade,
                            )}
                          </p>
                        </div>
                      </div>
                    </div>
                  )}

                  {/* AI Analysis Section */}
                  <div className="mb-5">
                    <button
                      onClick={handleAnalyzeTrader}
                      disabled={analysisLoading}
                      className="w-full py-3 rounded-xl font-semibold text-sm transition bg-gradient-to-r from-[var(--accent-soft)] to-emerald-500/15 text-[var(--accent)] hover:from-[var(--accent)]/25 hover:to-emerald-500/25 disabled:opacity-50 flex items-center justify-center gap-2 border border-[var(--accent)]/20"
                    >
                      {analysisLoading ? (
                        <>
                          <div className="animate-spin rounded-full h-4 w-4 border-t-2 border-b-2 border-[var(--accent)]" />
                          Analyzing...
                        </>
                      ) : (
                        <>🧠 AI Trade Pattern Analysis</>
                      )}
                    </button>
                    {analysisError && (
                      <div className="mt-2 p-3 rounded bg-red-500/10 border border-red-500/20 text-red-400 text-xs">
                        {analysisError}
                      </div>
                    )}
                    {analysisText && (
                      <div className="mt-4">
                        <TraderAnalysisTable
                          text={analysisText}
                          streaming={analysisLoading}
                        />
                      </div>
                    )}
                  </div>

                  {selectedTrader.positions.length > 0 && (
                    <div className="mb-5">
                      <h3 className="text-sm font-semibold mb-2">
                        Active Positions ({selectedTrader.positions.length})
                      </h3>
                      <div className="space-y-2 max-h-48 overflow-y-auto scroll-soft">
                        {selectedTrader.positions.slice(0, 10).map((pos, i) => (
                          <div
                            key={i}
                            className="surface-soft p-2 rounded text-xs"
                          >
                            <p className="text-white truncate">
                              {String(
                                pos.title || pos.market || `Position ${i + 1}`,
                              )}
                            </p>
                            <p className="text-muted">
                              Size: {formatUSD(Number(pos.size || 0))} · Price:
                              $
                              {Number(pos.avgPrice || pos.price || 0).toFixed(
                                2,
                              )}
                            </p>
                          </div>
                        ))}
                      </div>
                    </div>
                  )}

                  {/* Follow / Copy / Mock actions */}
                  {(() => {
                    const wallet = selectedTrader.wallet_address.toLowerCase();
                    const isCopyFollowed = followingSet.has(wallet);
                    const isNotificationFollowed =
                      notificationFollowingSet.has(wallet);
                    const isMockCopied = hasMockCopyTrader(mockTraders, wallet);
                    const emailEnabled = !!notificationEmailByWallet[wallet];

                    return (
                      <div className="space-y-3">
                        <div className="grid grid-cols-1 md:grid-cols-3 gap-2">
                          <button
                            onClick={() =>
                              requireAuth(() =>
                                isNotificationFollowed
                                  ? handleNotificationUnfollow(wallet)
                                  : handleNotificationFollow(wallet),
                              )
                            }
                            className={`py-2.5 rounded-xl font-semibold text-sm transition border ${
                              isNotificationFollowed
                                ? "bg-emerald-500/15 text-emerald-300 border-emerald-500/30 hover:bg-emerald-500/25"
                                : "bg-[var(--bg-soft)] text-soft border-[var(--line)] hover:border-[var(--line-strong)]"
                            }`}
                          >
                            {isNotificationFollowed
                              ? "Following Notifications"
                              : "Follow Notifications"}
                          </button>

                          <button
                            onClick={() =>
                              requireAuth(() =>
                                isCopyFollowed
                                  ? handleUnfollow(wallet)
                                  : handleFollow(wallet),
                              )
                            }
                            className={`py-2.5 rounded-xl font-semibold text-sm transition border ${
                              isCopyFollowed
                                ? "bg-[var(--accent-soft)] text-[var(--accent)] border-[var(--accent)]/40 hover:bg-[var(--accent)]/25"
                                : "bg-[var(--bg-soft)] text-soft border-[var(--line)] hover:border-[var(--line-strong)]"
                            }`}
                          >
                            {isCopyFollowed
                              ? "Copy Trade Enabled"
                              : "Copy Trade"}
                          </button>

                          <button
                            onClick={() =>
                              requireAuth(() => handleToggleMockCopy(wallet))
                            }
                            className={`py-2.5 rounded-xl font-semibold text-sm transition border ${
                              isMockCopied
                                ? "bg-sky-500/15 text-sky-300 border-sky-500/30 hover:bg-sky-500/25"
                                : "bg-[var(--bg-soft)] text-soft border-[var(--line)] hover:border-[var(--line-strong)]"
                            }`}
                          >
                            {isMockCopied
                              ? "Mock Copy Enabled"
                              : "Mock Copy Trade"}
                          </button>
                        </div>

                        <label className="flex items-center gap-2 text-xs text-soft">
                          <input
                            type="checkbox"
                            checked={emailEnabled}
                            onChange={(e) =>
                              handleNotificationEmailToggle(
                                wallet,
                                e.target.checked,
                              )
                            }
                            disabled={!isNotificationFollowed}
                            className="w-4 h-4 accent-[var(--accent)]"
                          />
                          Enable email for this trader's open/close
                          notifications
                        </label>

                        {popupActionError && (
                          <div className="p-2.5 rounded bg-red-500/10 border border-red-500/20 text-red-300 text-xs">
                            {popupActionError}
                          </div>
                        )}
                        {popupActionMessage && (
                          <div className="p-2.5 rounded bg-emerald-500/10 border border-emerald-500/20 text-emerald-300 text-xs">
                            {popupActionMessage}
                          </div>
                        )}

                        <div className="flex gap-2">
                          {buildPolymarketProfileUrl(
                            selectedTrader.wallet_address,
                          ) ? (
                            <a
                              href={
                                buildPolymarketProfileUrl(
                                  selectedTrader.wallet_address,
                                ) || "#"
                              }
                              target="_blank"
                              rel="noopener noreferrer"
                              className="flex items-center justify-center gap-1.5 px-4 py-2.5 rounded-xl font-semibold text-sm transition bg-[var(--accent-soft)] text-[var(--accent)] hover:bg-[var(--accent)]/25 border border-[var(--accent)]/30"
                            >
                              <svg
                                className="w-4 h-4"
                                fill="none"
                                viewBox="0 0 24 24"
                                stroke="currentColor"
                                strokeWidth={2}
                              >
                                <path
                                  strokeLinecap="round"
                                  strokeLinejoin="round"
                                  d="M10 6H6a2 2 0 00-2 2v10a2 2 0 002 2h10a2 2 0 002-2v-4M14 4h6m0 0v6m0-6L10 14"
                                />
                              </svg>
                              Polymarket Profile
                            </a>
                          ) : (
                            <span className="flex items-center justify-center px-4 py-2.5 rounded-xl text-sm bg-[var(--bg-soft)] text-muted border border-[var(--line)]">
                              Profile link unavailable
                            </span>
                          )}
                          <button
                            onClick={closeProfile}
                            className="btn-muted px-5 rounded-xl"
                          >
                            Close
                          </button>
                        </div>
                      </div>
                    );
                  })()}
                </div>{" "}
                {/* end px-6 wrapper */}
              </>
            ) : null}
          </div>
        </div>
      )}
    </div>
  );
}
