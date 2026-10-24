/**
 * Copy-trading dashboard with backend copy-trading and a separate local wallet watchlist.
 *
 * The copy tab manages followed wallets through `tradesService` (follow/unfollow, per-trader sizing
 * and copy-wallet settings with draft/save status tracking) and polls copy-evaluation rows every
 * 15 seconds. The watchlist tab stores wallet bookmarks only, with clearly labeled historical
 * leader metrics from the leaderboard. It does not simulate follower execution or portfolio P&L.
 *
 * @module components/CopyTrading
 */
import FollowerExperiments from "./FollowerExperiments";
import LiveExecutionControls from "./LiveExecutionControls";
import { useEffect, useState, useCallback, useMemo, useRef } from "react";
import {
  tradesService,
  FollowedTrader,
  CopyTradeRecord,
  LeaderboardEntry,
  FollowTraderRequest,
  CopyEvaluationRow,
  CopyAttributionResponse,
} from "../services/tradesService";
import { getApiErrorMessage } from "../utils/apiError";
import {
  MockCopyTrader,
  exportMockCopyTraders,
  hasMockCopyTrader,
  importMockCopyTraders,
  loadMockCopyTraders,
  removeMockCopyTraderAndPersist,
  resetMockCopyTraders,
  saveMockCopyTraders,
  setMockCopyTraderAlias,
  subscribeMockCopyTraders,
  upsertMockCopyTrader,
} from "../utils/mockCopyTraders";
import { buildPolymarketProfileUrl } from "../utils/urlSafety";
import { useAuthStore } from "../store/authStore";
import { useLoginModal } from "../context/LoginModalContext";
import { Banner, Skeleton, ConfirmModal, useToast } from "./ui";

type MockPeriod = "24h" | "7d" | "30d" | "all_time";

const MOCK_PERIODS: { value: MockPeriod; label: string }[] = [
  { value: "24h", label: "24 H" },
  { value: "7d", label: "7 D" },
  { value: "30d", label: "30 D" },
  { value: "all_time", label: "All Time" },
];

const COPY_TRADING_TAB_STORAGE_KEY = "pm:copy-trading:active-tab";

// Rolling windows offered by the trader-attribution selector, mirroring the
// execution-analytics windows.
const ATTRIBUTION_WINDOW_OPTIONS = [7, 30, 90];

const SIZING_MODE_OPTIONS = [
  { value: "inherit_global", label: "Inherit Global" },
  { value: "fixed_amount", label: "Fixed Amount" },
  { value: "trader_wallet_ratio", label: "Trader Wallet Ratio" },
] as const;

const COPY_WALLET_MODE_OPTIONS = [
  {
    value: "dynamic_main_wallet_percentage",
    label: "Dynamic % of Main Wallet",
  },
  { value: "fixed_snapshot_amount", label: "Fixed Snapshot Amount" },
] as const;

type TraderConfigDraft = FollowTraderRequest;
type SaveStatus = "idle" | "unsaved" | "saving" | "saved" | "error";
type MockIndicator = "added" | "already_exists";
type ConfirmActionType = "save" | "unfollow";

const defaultTraderConfig = (f: FollowedTrader): TraderConfigDraft => ({
  max_position_size: f.max_position_size ?? null,
  trader_alias: f.trader_alias ?? null,
  sizing_mode: f.sizing_mode ?? "inherit_global",
  fixed_trade_amount_override: f.fixed_trade_amount_override ?? null,
  copy_wallet_mode: f.copy_wallet_mode ?? "dynamic_main_wallet_percentage",
  copy_wallet_percentage: f.copy_wallet_percentage ?? 100,
  copy_wallet_fixed_amount: f.copy_wallet_fixed_amount ?? null,
});

const normalizeTraderConfig = (cfg: TraderConfigDraft) => ({
  max_position_size: cfg.max_position_size == null ? null : Number(cfg.max_position_size),
  trader_alias: cfg.trader_alias == null ? null : cfg.trader_alias.trim() || null,
  sizing_mode: cfg.sizing_mode ?? "inherit_global",
  fixed_trade_amount_override:
    cfg.fixed_trade_amount_override == null ? null : Number(cfg.fixed_trade_amount_override),
  copy_wallet_mode: cfg.copy_wallet_mode ?? "dynamic_main_wallet_percentage",
  copy_wallet_percentage:
    cfg.copy_wallet_percentage == null ? null : Number(cfg.copy_wallet_percentage),
  copy_wallet_fixed_amount:
    cfg.copy_wallet_fixed_amount == null ? null : Number(cfg.copy_wallet_fixed_amount),
});

export default function CopyTrading() {
  const { isAuthenticated } = useAuthStore();
  const { openLoginModal } = useLoginModal();
  const toast = useToast();

  const [activeTab, setActiveTab] = useState<"copy" | "mock">(() => {
    try {
      const stored = localStorage.getItem(COPY_TRADING_TAB_STORAGE_KEY);
      return stored === "mock" ? "mock" : "copy";
    } catch {
      return "copy";
    }
  });
  const [following, setFollowing] = useState<FollowedTrader[]>([]);
  const [copyTrades, setCopyTrades] = useState<CopyTradeRecord[]>([]);
  const [dailyPnl, setDailyPnl] = useState<number>(0);
  const [attributionWindow, setAttributionWindow] = useState(30);
  const [attribution, setAttribution] = useState<CopyAttributionResponse | null>(null);
  const [attributionLoading, setAttributionLoading] = useState(true);
  const [attributionError, setAttributionError] = useState<string | null>(null);
  const [walletInput, setWalletInput] = useState("");
  const [maxPositionInput, setMaxPositionInput] = useState("");
  const [addingWallet, setAddingWallet] = useState(false);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const [traderConfigDrafts, setTraderConfigDrafts] = useState<Record<string, TraderConfigDraft>>(
    {},
  );
  const [savingConfigWallet, setSavingConfigWallet] = useState<string | null>(null);
  const [saveStatusByWallet, setSaveStatusByWallet] = useState<Record<string, SaveStatus>>({});
  const [saveStatusMessageByWallet, setSaveStatusMessageByWallet] = useState<
    Record<string, string | null>
  >({});
  const [mockIndicatorByWallet, setMockIndicatorByWallet] = useState<Record<string, MockIndicator>>(
    {},
  );
  const [confirmState, setConfirmState] = useState<{
    open: boolean;
    type: ConfirmActionType | null;
    wallet: string | null;
  }>({
    open: false,
    type: null,
    wallet: null,
  });
  const [confirmLoading, setConfirmLoading] = useState(false);
  const [mockRemoveWallet, setMockRemoveWallet] = useState<string | null>(null);
  const [mockResetConfirmOpen, setMockResetConfirmOpen] = useState(false);

  const [evaluationWallet, setEvaluationWallet] = useState("");
  const [evaluationRows, setEvaluationRows] = useState<CopyEvaluationRow[]>([]);
  const [evaluationLoading, setEvaluationLoading] = useState(false);
  const [evaluationError, setEvaluationError] = useState<string | null>(null);
  const [evaluationUpdatedAt, setEvaluationUpdatedAt] = useState<string | null>(null);

  const [mockWalletInput, setMockWalletInput] = useState("");
  const [mockPeriod, setMockPeriod] = useState<MockPeriod>("30d");
  const [mockTraders, setMockTraders] = useState<MockCopyTrader[]>(() => loadMockCopyTraders());
  const [mockLeaderboard, setMockLeaderboard] = useState<LeaderboardEntry[]>([]);
  const [mockLoading, setMockLoading] = useState(false);
  const [mockError, setMockError] = useState<string | null>(null);
  // Provenance of the leaderboard figures shown in the watchlist: when they
  // were produced and for which window. Without them the panel would present
  // numbers with no indication of how old they are.
  const [mockLeaderboardUpdatedAt, setMockLeaderboardUpdatedAt] = useState<string | null>(null);
  const [mockLeaderboardPeriod, setMockLeaderboardPeriod] = useState<string | null>(null);
  const mockImportInputRef = useRef<HTMLInputElement | null>(null);

  const fetchData = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const [followedList, trades, pnlData] = await Promise.all([
        tradesService.getFollowing(),
        tradesService.getCopyTrades(50),
        tradesService.getCopyTradePnl(),
      ]);
      setFollowing(followedList);
      setCopyTrades(trades);
      setDailyPnl(pnlData.daily_pnl);
      setTraderConfigDrafts((prev) => {
        const next: Record<string, TraderConfigDraft> = {};
        followedList.forEach((f) => {
          const key = f.trader_wallet.toLowerCase();
          next[key] = prev[key] ?? defaultTraderConfig(f);
        });
        return next;
      });
    } catch (err: unknown) {
      setError(getApiErrorMessage(err, "Failed to load copy trading data"));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    fetchData();
  }, [fetchData]);

  const fetchAttribution = useCallback(async () => {
    setAttributionLoading(true);
    setAttributionError(null);
    try {
      const data = await tradesService.getCopyAttribution(attributionWindow);
      setAttribution(data);
    } catch (err: unknown) {
      setAttributionError(getApiErrorMessage(err, "Failed to load copy attribution"));
    } finally {
      setAttributionLoading(false);
    }
  }, [attributionWindow]);

  useEffect(() => {
    if (activeTab !== "copy") return;
    fetchAttribution();
  }, [activeTab, fetchAttribution]);

  useEffect(() => {
    localStorage.setItem(COPY_TRADING_TAB_STORAGE_KEY, activeTab);
  }, [activeTab]);

  useEffect(() => {
    if (following.length === 0) {
      setEvaluationWallet("");
      setEvaluationRows([]);
      return;
    }
    const exists = following.some(
      (f) => f.trader_wallet.toLowerCase() === evaluationWallet.toLowerCase(),
    );
    if (!evaluationWallet || !exists) {
      setEvaluationWallet(following[0].trader_wallet);
    }
  }, [following, evaluationWallet]);

  const fetchEvaluation = useCallback(async (wallet: string) => {
    if (!wallet) return;
    setEvaluationLoading(true);
    setEvaluationError(null);
    try {
      const data = await tradesService.getCopyEvaluation(wallet, 50);
      setEvaluationRows(data.rows);
      setEvaluationUpdatedAt(data.updated_at);
    } catch (err: unknown) {
      setEvaluationError(getApiErrorMessage(err, "Failed to load copy evaluation"));
    } finally {
      setEvaluationLoading(false);
    }
  }, []);

  useEffect(() => {
    if (!evaluationWallet || activeTab !== "copy") return;
    fetchEvaluation(evaluationWallet);
    const id = window.setInterval(() => {
      fetchEvaluation(evaluationWallet);
    }, 15_000);
    return () => window.clearInterval(id);
  }, [activeTab, evaluationWallet, fetchEvaluation]);

  useEffect(() => subscribeMockCopyTraders(setMockTraders), []);

  useEffect(() => {
    setSaveStatusByWallet((prev) => {
      const next: Record<string, SaveStatus> = {};
      following.forEach((f) => {
        const key = f.trader_wallet.toLowerCase();
        next[key] = prev[key] ?? "idle";
      });
      return next;
    });
    setSaveStatusMessageByWallet((prev) => {
      const next: Record<string, string | null> = {};
      following.forEach((f) => {
        const key = f.trader_wallet.toLowerCase();
        next[key] = prev[key] ?? null;
      });
      return next;
    });
    setMockIndicatorByWallet((prev) => {
      const next: Record<string, MockIndicator> = {};
      following.forEach((f) => {
        const key = f.trader_wallet.toLowerCase();
        if (prev[key]) next[key] = prev[key];
      });
      return next;
    });
  }, [following]);

  useEffect(() => {
    const mockedSet = new Set(mockTraders.map((t) => t.wallet.toLowerCase()));
    setMockIndicatorByWallet((prev) => {
      const next: Record<string, MockIndicator> = {};
      Object.entries(prev).forEach(([wallet, indicator]) => {
        if (indicator === "already_exists" || mockedSet.has(wallet)) {
          next[wallet] = indicator;
        }
      });
      return next;
    });
  }, [mockTraders]);

  const isValidWallet = (wallet: string) => /^0x[a-fA-F0-9]{40}$/.test(wallet);

  const shortAddress = (addr: string) =>
    addr.length >= 10 ? `${addr.slice(0, 6)}...${addr.slice(-4)}` : addr;

  const getPolymarketProfileUrl = (wallet: string) => buildPolymarketProfileUrl(wallet);

  const getTraderPrimaryLabel = (trader: FollowedTrader) =>
    trader.trader_alias?.trim() || trader.display_name || shortAddress(trader.trader_wallet);

  const getPersistedConfigForWallet = useCallback(
    (wallet: string): TraderConfigDraft | null => {
      const followed = following.find(
        (f) => f.trader_wallet.toLowerCase() === wallet.toLowerCase(),
      );
      return followed ? defaultTraderConfig(followed) : null;
    },
    [following],
  );

  const isDraftDirtyForWallet = useCallback(
    (wallet: string, draft: TraderConfigDraft): boolean => {
      const persisted = getPersistedConfigForWallet(wallet);
      if (!persisted) return false;
      const persistedNormalized = normalizeTraderConfig(persisted);
      const draftNormalized = normalizeTraderConfig(draft);
      return JSON.stringify(draftNormalized) !== JSON.stringify(persistedNormalized);
    },
    [getPersistedConfigForWallet],
  );

  const formatUSD = (v: number) => {
    const abs = Math.abs(v);
    if (abs >= 1_000_000) return `$${(v / 1_000_000).toFixed(1)}M`;
    if (abs >= 1_000) return `$${(v / 1_000).toFixed(1)}K`;
    return `$${v.toLocaleString("en-US", {
      minimumFractionDigits: 2,
      maximumFractionDigits: 2,
    })}`;
  };

  const formatPct = (v: number) => `${v >= 0 ? "+" : ""}${(v * 100).toFixed(2)}%`;

  const handleUnfollow = async (wallet: string): Promise<boolean> => {
    const key = wallet.toLowerCase();
    try {
      await tradesService.unfollowTrader(wallet);
      setFollowing((prev) => prev.filter((f) => f.trader_wallet !== wallet));
      toast.success(`Stopped copying trades from ${shortAddress(wallet)}.`);
      setSaveStatusByWallet((prev) => {
        const next = { ...prev };
        delete next[key];
        return next;
      });
      setSaveStatusMessageByWallet((prev) => {
        const next = { ...prev };
        delete next[key];
        return next;
      });
      return true;
    } catch (err: unknown) {
      setError(getApiErrorMessage(err, "Failed to unfollow"));
      return false;
    }
  };

  const handleFollowByWallet = async () => {
    const wallet = walletInput.trim().toLowerCase();
    const parsedMaxPosition = maxPositionInput.trim() ? Number(maxPositionInput.trim()) : undefined;

    if (!isValidWallet(wallet)) {
      setError("Enter a valid Polymarket wallet address (0x + 40 hex chars).");
      return;
    }

    if (
      parsedMaxPosition != null &&
      (!Number.isFinite(parsedMaxPosition) || parsedMaxPosition <= 0)
    ) {
      setError("Max position size must be a positive number.");
      return;
    }

    setAddingWallet(true);
    setError(null);

    try {
      const record = await tradesService.followTrader(wallet, {
        max_position_size: parsedMaxPosition ?? null,
        sizing_mode: "inherit_global",
        copy_wallet_mode: "dynamic_main_wallet_percentage",
        copy_wallet_percentage: 100,
      });

      setFollowing((prev) => {
        const idx = prev.findIndex((f) => f.trader_wallet.toLowerCase() === wallet);
        if (idx >= 0) {
          const next = [...prev];
          next[idx] = record;
          return next;
        }
        return [record, ...prev];
      });
      setTraderConfigDrafts((prev) => ({
        ...prev,
        [wallet]: defaultTraderConfig(record),
      }));
      setWalletInput("");
      setMaxPositionInput("");
      toast.success(`Now copying trades from ${shortAddress(wallet)}.`);
      if (!evaluationWallet) {
        setEvaluationWallet(wallet);
      }
    } catch (err: unknown) {
      setError(getApiErrorMessage(err, "Failed to follow trader by wallet address"));
    } finally {
      setAddingWallet(false);
    }
  };

  const updateTraderDraft = (wallet: string, patch: Partial<TraderConfigDraft>) => {
    const key = wallet.toLowerCase();
    setTraderConfigDrafts((prev) => {
      const nextDraft: TraderConfigDraft = {
        ...(prev[key] ?? {}),
        ...patch,
      };
      const dirty = isDraftDirtyForWallet(key, nextDraft);
      setSaveStatusByWallet((prevStatus) => ({
        ...prevStatus,
        [key]: dirty ? "unsaved" : "saved",
      }));
      setSaveStatusMessageByWallet((prevMessage) => ({
        ...prevMessage,
        [key]: dirty ? "Unsaved changes" : "Saved",
      }));
      return {
        ...prev,
        [key]: nextDraft,
      };
    });
  };

  const saveTraderConfig = async (wallet: string): Promise<boolean> => {
    const key = wallet.toLowerCase();
    const draft = traderConfigDrafts[key];
    if (!draft) return false;

    if (
      draft.max_position_size != null &&
      (!Number.isFinite(draft.max_position_size) || draft.max_position_size <= 0)
    ) {
      setError("Max position size must be a positive number.");
      setSaveStatusByWallet((prev) => ({ ...prev, [key]: "error" }));
      setSaveStatusMessageByWallet((prev) => ({
        ...prev,
        [key]: "Max position size must be positive",
      }));
      return false;
    }

    if (draft.sizing_mode === "fixed_amount") {
      if (
        draft.fixed_trade_amount_override == null ||
        !Number.isFinite(draft.fixed_trade_amount_override) ||
        draft.fixed_trade_amount_override <= 0
      ) {
        setError("Fixed amount mode requires a positive fixed amount override.");
        setSaveStatusByWallet((prev) => ({ ...prev, [key]: "error" }));
        setSaveStatusMessageByWallet((prev) => ({
          ...prev,
          [key]: "Fixed amount override is invalid",
        }));
        return false;
      }
    }

    if (draft.sizing_mode === "trader_wallet_ratio") {
      if (draft.copy_wallet_mode === "fixed_snapshot_amount") {
        if (
          draft.copy_wallet_fixed_amount == null ||
          !Number.isFinite(draft.copy_wallet_fixed_amount) ||
          draft.copy_wallet_fixed_amount <= 0
        ) {
          setError("Fixed snapshot mode requires a positive copy wallet fixed amount.");
          setSaveStatusByWallet((prev) => ({ ...prev, [key]: "error" }));
          setSaveStatusMessageByWallet((prev) => ({
            ...prev,
            [key]: "Copy wallet fixed amount is invalid",
          }));
          return false;
        }
      } else {
        if (
          draft.copy_wallet_percentage == null ||
          !Number.isFinite(draft.copy_wallet_percentage) ||
          draft.copy_wallet_percentage <= 0 ||
          draft.copy_wallet_percentage > 100
        ) {
          setError("Copy wallet percentage must be between 0 and 100.");
          setSaveStatusByWallet((prev) => ({ ...prev, [key]: "error" }));
          setSaveStatusMessageByWallet((prev) => ({
            ...prev,
            [key]: "Copy wallet % must be between 0 and 100",
          }));
          return false;
        }
      }
    }

    setSavingConfigWallet(key);
    setSaveStatusByWallet((prev) => ({ ...prev, [key]: "saving" }));
    setSaveStatusMessageByWallet((prev) => ({ ...prev, [key]: null }));
    setError(null);

    try {
      const normalizedAlias = draft.trader_alias == null ? null : draft.trader_alias.trim() || null;
      const payload: FollowTraderRequest = {
        max_position_size: draft.max_position_size == null ? null : draft.max_position_size,
        trader_alias: normalizedAlias,
        sizing_mode: draft.sizing_mode,
        fixed_trade_amount_override:
          draft.fixed_trade_amount_override == null ? null : draft.fixed_trade_amount_override,
        copy_wallet_mode: draft.copy_wallet_mode,
        copy_wallet_percentage:
          draft.copy_wallet_percentage == null ? null : draft.copy_wallet_percentage,
        copy_wallet_fixed_amount:
          draft.copy_wallet_fixed_amount == null ? null : draft.copy_wallet_fixed_amount,
      };

      const updated = await tradesService.followTrader(key, payload);
      setFollowing((prev) =>
        prev.map((f) => (f.trader_wallet.toLowerCase() === key ? updated : f)),
      );
      setTraderConfigDrafts((prev) => ({
        ...prev,
        [key]: defaultTraderConfig(updated),
      }));
      toast.success(`Saved copy config for ${shortAddress(key)}.`);
      setSaveStatusByWallet((prev) => ({ ...prev, [key]: "saved" }));
      setSaveStatusMessageByWallet((prev) => ({ ...prev, [key]: "Saved" }));
      return true;
    } catch (err: unknown) {
      const message = getApiErrorMessage(err, "Failed to save trader copy settings");
      setError(message);
      setSaveStatusByWallet((prev) => ({ ...prev, [key]: "error" }));
      setSaveStatusMessageByWallet((prev) => ({ ...prev, [key]: message }));
      return false;
    } finally {
      setSavingConfigWallet(null);
    }
  };

  const upsertMockTrader = (wallet: string, alias?: string | null) => {
    setMockTraders((prev) => {
      const next = upsertMockCopyTrader(prev, wallet, alias);
      saveMockCopyTraders(next);
      return next;
    });
  };

  const handleAddMockTrader = () => {
    const wallet = mockWalletInput.trim().toLowerCase();
    if (!isValidWallet(wallet)) {
      setMockError("Enter a valid Polymarket wallet address (0x + 40 hex chars).");
      return;
    }
    if (hasMockCopyTrader(mockTraders, wallet)) {
      setMockError("This wallet is already on the watchlist.");
      setMockIndicatorByWallet((prev) => ({
        ...prev,
        [wallet]: "already_exists",
      }));
      return;
    }
    upsertMockTrader(wallet);
    setMockWalletInput("");
    setMockError(null);
    toast.success(`Watchlisted ${shortAddress(wallet)}.`);
    setMockIndicatorByWallet((prev) => ({
      ...prev,
      [wallet]: "added",
    }));
  };

  const handleAddFollowedToMock = (wallet: string, alias?: string | null) => {
    const normalized = wallet.toLowerCase();
    if (hasMockCopyTrader(mockTraders, normalized)) {
      setMockError("This wallet is already on the watchlist.");
      setMockIndicatorByWallet((prev) => ({
        ...prev,
        [normalized]: "already_exists",
      }));
      return;
    }
    upsertMockTrader(normalized, alias);
    setMockError(null);
    toast.success(`Added ${shortAddress(normalized)} to the watchlist.`);
    setMockIndicatorByWallet((prev) => ({
      ...prev,
      [normalized]: "added",
    }));
  };

  const handleRemoveMockTrader = (wallet: string) => {
    // The removal is applied to the persisted list so entries added by
    // another tab are not lost, and it is never re-merged back in.
    setMockTraders(removeMockCopyTraderAndPersist(wallet));
    toast.success(`Removed ${shortAddress(wallet)} from the watchlist.`);
    setMockError(null);
    setMockIndicatorByWallet((prev) => {
      const next = { ...prev };
      delete next[wallet.toLowerCase()];
      return next;
    });
  };

  const handleExportWatchlist = () => {
    const payload = exportMockCopyTraders();
    const blob = new Blob([payload], { type: "application/json" });
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = url;
    link.download = "polymarket-watchlist.json";
    document.body.appendChild(link);
    link.click();
    link.remove();
    URL.revokeObjectURL(url);
    toast.success("Watchlist exported.");
  };

  const handleImportWatchlist = async (file: File | undefined) => {
    if (!file) return;
    try {
      const raw = await file.text();
      const next = importMockCopyTraders(raw);
      setMockTraders(next);
      setMockError(null);
      toast.success(`Watchlist imported — ${next.length} wallet(s) watched.`);
    } catch {
      setMockError("Could not read that file. Expected a watchlist JSON export.");
    }
  };

  const handleResetWatchlist = () => {
    resetMockCopyTraders();
    setMockTraders([]);
    setMockError(null);
    toast.success("Watchlist cleared.");
  };

  const updateMockTraderAlias = (wallet: string, alias: string) => {
    setMockTraders((prev) => {
      const next = setMockCopyTraderAlias(prev, wallet, alias);
      saveMockCopyTraders(next);
      return next;
    });
  };

  const openConfirmModal = (type: ConfirmActionType, wallet: string) => {
    setConfirmState({
      open: true,
      type,
      wallet: wallet.toLowerCase(),
    });
  };

  const closeConfirmModal = () => {
    if (confirmLoading) return;
    setConfirmState({
      open: false,
      type: null,
      wallet: null,
    });
  };

  const handleConfirmAction = async () => {
    if (!confirmState.open || !confirmState.type || !confirmState.wallet) return;
    setConfirmLoading(true);
    let ok = false;
    try {
      if (confirmState.type === "save") {
        ok = await saveTraderConfig(confirmState.wallet);
      } else {
        ok = await handleUnfollow(confirmState.wallet);
      }
    } finally {
      setConfirmLoading(false);
    }
    if (ok) {
      setConfirmState({
        open: false,
        type: null,
        wallet: null,
      });
    }
  };

  const refreshMockLeaderboard = useCallback(async () => {
    if (mockTraders.length === 0) {
      setMockLeaderboard([]);
      setMockLeaderboardUpdatedAt(null);
      setMockLeaderboardPeriod(null);
      return;
    }
    setMockLoading(true);
    setMockError(null);
    try {
      const data = await tradesService.getLeaderboard(1000, mockPeriod);
      setMockLeaderboard(data.entries);
      setMockLeaderboardUpdatedAt(data.updated_at ?? null);
      setMockLeaderboardPeriod(data.period ?? null);
    } catch (err: unknown) {
      setMockError(getApiErrorMessage(err, "Failed to refresh leaderboard data"));
    } finally {
      setMockLoading(false);
    }
  }, [mockPeriod, mockTraders.length]);

  useEffect(() => {
    refreshMockLeaderboard();
  }, [refreshMockLeaderboard]);

  const mockRows = useMemo(() => {
    const byWallet = new Map(
      mockLeaderboard.map((entry) => [entry.address.toLowerCase(), entry] as const),
    );

    return mockTraders.map((trader) => {
      const entry = byWallet.get(trader.wallet.toLowerCase());
      // A leader without traded volume has no meaningful performance figure:
      // rendering $0.00 would read like a flat result instead of "no data".
      const hasVolume = Boolean(entry && Number.isFinite(entry.volume) && entry.volume > 0);
      const leaderPnl =
        entry && hasVolume && Number.isFinite(entry.profit_loss) ? entry.profit_loss : null;
      const leaderVolume = entry && hasVolume ? entry.volume : null;

      return {
        ...trader,
        display_name: entry?.display_name,
        primary_name: trader.alias?.trim() || entry?.display_name || shortAddress(trader.wallet),
        leaderPnl,
        leaderVolume,
        hasLeaderboardData: Boolean(entry),
      };
    });
  }, [mockLeaderboard, mockTraders]);

  const followedByWallet = useMemo(
    () => new Map(following.map((f) => [f.trader_wallet.toLowerCase(), f] as const)),
    [following],
  );

  const getTraderLabelByWallet = (wallet: string) => {
    const followed = followedByWallet.get(wallet.toLowerCase());
    return followed ? getTraderPrimaryLabel(followed) : shortAddress(wallet);
  };

  const getSaveStatusChip = (status: SaveStatus) => {
    if (status === "unsaved") {
      return <span className="chip chip-warning text-xs">Unsaved</span>;
    }
    if (status === "saving") {
      return <span className="chip chip-accent text-xs">Saving...</span>;
    }
    if (status === "saved") {
      return <span className="chip chip-success text-xs">Saved</span>;
    }
    if (status === "error") {
      return <span className="chip chip-danger text-xs">Save Failed</span>;
    }
    return null;
  };

  const mockTotals = useMemo(
    () => ({ matched: mockRows.filter((row) => row.hasLeaderboardData).length }),
    [mockRows],
  );

  // Best trader first; the backend already returns this order, so the sort
  // also survives a payload from an older API version.
  const attributionRows = useMemo(() => {
    const rows = attribution?.traders ?? [];
    return [...rows].sort((a, b) => b.realized_pnl - a.realized_pnl);
  }, [attribution]);

  // ── Unauthenticated visitors see a connect-wallet prompt ──
  // This gate must stay below every hook above: returning early before the
  // hooks would change the hook count between the unauthenticated first paint
  // and the authenticated re-render once the session is restored.
  if (!isAuthenticated) {
    return (
      <div className="max-w-3xl mx-auto mt-16 px-4 text-center space-y-6">
        <h1 className="text-2xl font-bold text-white">Copy Trading</h1>
        <p className="text-soft text-base leading-relaxed max-w-lg mx-auto">
          Automatically mirror the trades of top Polymarket traders. Follow wallets, configure
          position sizing, and track performance — all from one dashboard.
        </p>
        <button onClick={openLoginModal} className="btn-accent font-semibold text-sm px-6 py-3">
          Connect Wallet to Get Started
        </button>
      </div>
    );
  }

  if (loading) {
    return (
      <div className="space-y-6">
        <h1 className="text-3xl font-bold">Copy Trading</h1>
        <p className="text-soft">Loading...</p>
        <div className="space-y-3">
          {[1, 2, 3].map((i) => (
            <div key={i} className="surface-panel p-4 animate-pulse h-16" />
          ))}
        </div>
      </div>
    );
  }

  return (
    <div className="space-y-6">
      <div className="flex justify-between items-start">
        <div>
          <h1 className="text-3xl font-bold mb-2">Copy Trading</h1>
          <p className="text-soft">
            {activeTab === "copy"
              ? "Automatically mirror trades from top traders"
              : "Watch Polymarket traders without executing or simulating trades"}
          </p>
        </div>
        <button
          onClick={activeTab === "copy" ? fetchData : refreshMockLeaderboard}
          className="btn-muted"
        >
          {activeTab === "copy" ? "Refresh" : mockLoading ? "Refreshing..." : "Refresh Leaderboard"}
        </button>
      </div>

      <FollowerExperiments />

      <div className="surface-panel p-2 rounded-lg inline-flex gap-2">
        <button
          onClick={() => setActiveTab("copy")}
          className={`px-4 py-2 rounded-md text-sm font-semibold transition ${
            activeTab === "copy"
              ? "bg-[var(--accent-soft)] text-[var(--accent)]"
              : "text-soft hover:text-white"
          }`}
        >
          Copy Trading
        </button>
        <button
          onClick={() => setActiveTab("mock")}
          className={`px-4 py-2 rounded-md text-sm font-semibold transition ${
            activeTab === "mock"
              ? "bg-[var(--accent-soft)] text-[var(--accent)]"
              : "text-soft hover:text-white"
          }`}
        >
          Trader Watchlist (Illustrative)
        </button>
      </div>

      {activeTab === "copy" && error && (
        <Banner variant="error" onDismiss={() => setError(null)} onRetry={fetchData}>
          {error}
        </Banner>
      )}

      {activeTab === "mock" && (
        <div className="surface-panel p-6 rounded-lg space-y-4">
          <div className="flex items-start justify-between gap-4 flex-wrap">
            <div>
              <h2 className="text-lg font-semibold">Trader Watchlist (Illustrative)</h2>
              <p className="text-soft text-sm">
                Bookmark wallets locally and review historical leaderboard results. No funds are
                allocated, and no follower returns are calculated.
              </p>
            </div>
            <button onClick={refreshMockLeaderboard} className="btn-muted">
              {mockLoading ? "Refreshing..." : "Refresh Leaderboard"}
            </button>
          </div>

          {mockError && (
            <Banner
              variant="error"
              onDismiss={() => setMockError(null)}
              onRetry={refreshMockLeaderboard}
            >
              {mockError}
            </Banner>
          )}

          <div
            className="surface-soft p-3 rounded-lg text-xs text-soft space-y-1"
            data-testid="watchlist-status"
          >
            <p className="text-white font-semibold">
              Leaderboard figures as of{" "}
              {mockLeaderboardUpdatedAt
                ? new Date(mockLeaderboardUpdatedAt).toLocaleString()
                : "not loaded yet"}
            </p>
            <p>
              source: Polymarket leaderboard
              {mockLeaderboardPeriod ? ` · window: ${mockLeaderboardPeriod}` : ""} ·{" "}
              {mockRows.length} wallet(s) watched · no follower returns are calculated · no funds
              are involved
            </p>
          </div>

          <div className="flex items-center gap-2 flex-wrap">
            <div className="flex bg-[var(--bg-soft)] rounded-lg p-1">
              {MOCK_PERIODS.map((p) => (
                <button
                  key={p.value}
                  onClick={() => setMockPeriod(p.value)}
                  className={`px-3 py-1.5 rounded-md text-xs font-semibold transition ${
                    mockPeriod === p.value
                      ? "bg-[var(--accent-soft)] text-[var(--accent)]"
                      : "text-soft hover:text-white"
                  }`}
                >
                  {p.label}
                </button>
              ))}
            </div>
            <span className="text-xs text-muted">
              These are the leader&apos;s own historical leaderboard figures for the selected
              window, not returns earned by following. No follower returns are calculated and no
              funds are involved.
            </span>
          </div>

          <div className="flex flex-col md:flex-row gap-3">
            <input
              type="text"
              value={mockWalletInput}
              onChange={(e) => setMockWalletInput(e.target.value)}
              placeholder="Add wallet to watchlist (0x...)"
              className="flex-1 bg-[var(--bg-soft)] border border-[var(--line)] rounded px-3 py-2 text-sm mono"
            />
            <button onClick={handleAddMockTrader} className="btn-primary">
              Add to Watchlist
            </button>
          </div>

          <div className="flex items-center gap-2 flex-wrap">
            <button onClick={handleExportWatchlist} className="btn-muted text-sm">
              Export Watchlist
            </button>
            <button
              onClick={() => mockImportInputRef.current?.click()}
              className="btn-muted text-sm"
            >
              Import Watchlist
            </button>
            <button onClick={() => setMockResetConfirmOpen(true)} className="btn-muted text-sm">
              Reset Watchlist
            </button>
            <input
              ref={mockImportInputRef}
              type="file"
              accept="application/json,.json"
              className="hidden"
              data-testid="watchlist-import-input"
              onChange={(e) => {
                const file = e.target.files?.[0];
                e.target.value = "";
                void handleImportWatchlist(file);
              }}
            />
          </div>

          <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
            <div className="surface-soft p-4 rounded-lg">
              <p className="text-muted text-xs uppercase tracking-wide">Watchlisted Traders</p>
              <p className="text-2xl font-bold mt-1">{mockRows.length}</p>
            </div>
            <div className="surface-soft p-4 rounded-lg">
              <p className="text-muted text-xs uppercase tracking-wide">
                Matched Leaderboard Profiles
              </p>
              <p className="text-2xl font-bold mt-1">{mockTotals.matched}</p>
            </div>
          </div>

          {mockRows.length === 0 ? (
            <div className="surface-soft p-6 rounded text-sm text-soft">
              No traders added yet. Save any Polymarket wallet to your local watchlist. No trades or
              returns are simulated.
            </div>
          ) : (
            <div className="space-y-3">
              {mockLeaderboard.length === 0 && (
                <div
                  className="surface-soft p-4 rounded text-sm text-soft"
                  data-testid="watchlist-no-leaderboard"
                >
                  No leaderboard data available. The leader figures for the selected window could
                  not be loaded, so every column below is unknown. No follower returns are
                  calculated and no funds are involved.
                </div>
              )}
              <div className="overflow-x-auto">
                <table className="table-theme text-sm">
                  <thead>
                    <tr>
                      <th className="text-left p-3">Trader</th>
                      <th className="text-right p-3">Leader P&amp;L proxy (period)</th>
                      <th className="text-right p-3">Leader Volume proxy (period)</th>
                      <th className="text-right p-3">Started</th>
                      <th className="text-center p-3">Action</th>
                    </tr>
                  </thead>
                  <tbody>
                    {mockRows.map((row) => (
                      <tr key={row.wallet}>
                        <td className="p-3">
                          <p className="text-white font-medium text-xs">{row.primary_name}</p>
                          <p className="text-muted text-xs mono">{shortAddress(row.wallet)}</p>
                          {getPolymarketProfileUrl(row.wallet) && (
                            <a
                              href={getPolymarketProfileUrl(row.wallet) || "#"}
                              target="_blank"
                              rel="noopener noreferrer"
                              className="text-[11px] text-[var(--accent)] hover:underline"
                            >
                              Polymarket Profile
                            </a>
                          )}
                          <input
                            type="text"
                            value={row.alias ?? row.display_name ?? ""}
                            onChange={(e) =>
                              updateMockTraderAlias(
                                row.wallet,
                                row.display_name &&
                                  e.target.value.trim().toLowerCase() ===
                                    row.display_name.trim().toLowerCase()
                                  ? ""
                                  : e.target.value,
                              )
                            }
                            placeholder="Defaults to Polymarket username"
                            className="mt-2 w-full bg-[var(--bg-soft)] border border-[var(--line)] rounded px-2 py-1.5 text-xs"
                          />
                        </td>
                        <td className="p-3 text-right mono">
                          {row.leaderPnl == null ? (
                            <span className="text-muted">Unknown</span>
                          ) : (
                            formatUSD(row.leaderPnl)
                          )}
                        </td>
                        <td className="p-3 text-right mono">
                          {row.leaderVolume == null ? (
                            <span className="text-muted">Unknown</span>
                          ) : (
                            formatUSD(row.leaderVolume)
                          )}
                        </td>
                        <td className="p-3 text-right text-muted text-xs">
                          {row.created_at
                            ? new Date(row.created_at).toLocaleDateString()
                            : "Unknown"}
                        </td>
                        <td className="p-3 text-center">
                          <button
                            onClick={() => setMockRemoveWallet(row.wallet)}
                            className="px-3 py-1 rounded text-xs font-semibold bg-red-500/20 text-red-400 hover:bg-red-500/30 transition"
                          >
                            Remove
                          </button>
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

      {activeTab === "copy" && <LiveExecutionControls />}

      {activeTab === "copy" && (
        <div className="surface-panel p-6 rounded-lg space-y-4">
          <h2 className="text-lg font-semibold">Follow by Wallet Address</h2>
          <p className="text-soft text-sm">
            Paste a Polymarket trader wallet address to start copy trading.
          </p>
          <div className="flex flex-col md:flex-row gap-3">
            <input
              type="text"
              value={walletInput}
              onChange={(e) => setWalletInput(e.target.value)}
              placeholder="0x..."
              className="flex-1 bg-[var(--bg-soft)] border border-[var(--line)] rounded px-3 py-2 text-sm mono"
            />
            <input
              type="number"
              min="0"
              step="0.01"
              value={maxPositionInput}
              onChange={(e) => setMaxPositionInput(e.target.value)}
              placeholder="Max position (optional)"
              className="w-full md:w-56 bg-[var(--bg-soft)] border border-[var(--line)] rounded px-3 py-2 text-sm"
            />
            <button
              onClick={handleFollowByWallet}
              disabled={addingWallet}
              className="btn-primary disabled:opacity-60 disabled:cursor-not-allowed"
            >
              {addingWallet ? "Adding..." : "Follow Wallet"}
            </button>
          </div>
        </div>
      )}

      {activeTab === "copy" && (
        <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
          <div className="surface-panel p-4 rounded-lg">
            <p className="text-muted text-xs uppercase tracking-wide">Following</p>
            <p className="text-2xl font-bold mt-1">{following.length}</p>
          </div>
          <div className="surface-panel p-4 rounded-lg">
            <p className="text-muted text-xs uppercase tracking-wide">Copy Trades (Today)</p>
            <p className="text-2xl font-bold mt-1">{copyTrades.length}</p>
          </div>
          <div className="surface-panel p-4 rounded-lg">
            <p className="text-muted text-xs uppercase tracking-wide">Daily P&L</p>
            <p
              className={`text-2xl font-bold mt-1 ${dailyPnl >= 0 ? "status-good" : "status-bad"}`}
            >
              {dailyPnl >= 0 ? "+" : ""}
              {formatUSD(dailyPnl)}
            </p>
          </div>
        </div>
      )}

      {activeTab === "copy" && (
        <div className="surface-panel p-6 rounded-lg space-y-4">
          <div className="flex items-center justify-between gap-3 flex-wrap">
            <div>
              <h2 className="text-lg font-semibold">Trader Attribution</h2>
              <p className="text-soft text-sm">
                Realized PnL and win rate per copied trader over the last {attributionWindow} days.
              </p>
            </div>
            <div className="flex items-center gap-2">
              {ATTRIBUTION_WINDOW_OPTIONS.map((days) => (
                <button
                  key={days}
                  onClick={() => setAttributionWindow(days)}
                  className={`px-3 py-1.5 rounded-lg text-sm ${
                    attributionWindow === days
                      ? "btn-primary"
                      : "bg-surface border border-border text-soft"
                  }`}
                >
                  {days}d
                </button>
              ))}
            </div>
          </div>

          {attributionError && (
            <Banner
              variant="error"
              onDismiss={() => setAttributionError(null)}
              onRetry={fetchAttribution}
            >
              {attributionError}
            </Banner>
          )}

          {attribution?.data_quality && attribution.data_quality.unreconciled_sells > 0 && (
            <div className="p-3 rounded text-sm border border-yellow-500/30 bg-yellow-500/10 text-yellow-300">
              {attribution.data_quality.unreconciled_sells} closed copy sell(s) are still awaiting
              PnL reconciliation, so their profit is missing from every trader below.
            </div>
          )}

          {attribution?.data_quality && attribution.data_quality.missing_executed_at > 0 && (
            <div className="p-3 rounded text-sm border border-yellow-500/30 bg-yellow-500/10 text-yellow-300">
              {attribution.data_quality.missing_executed_at} pasted copy trade(s) have no execution
              timestamp, so they are missing from the per-trader figures below.
            </div>
          )}

          {attributionLoading && !attribution ? (
            <div className="py-4" role="status" aria-live="polite">
              <Skeleton variant="text" lines={3} />
              <p className="text-soft text-sm mt-2">Loading attribution...</p>
            </div>
          ) : attributionRows.length === 0 ? (
            <p className="text-soft text-sm">
              No copy trades with settled PnL in this window. Follow a trader and let positions
              close before attribution appears here.
            </p>
          ) : (
            <>
              <div className="overflow-x-auto">
                <table className="table-theme text-sm">
                  <thead>
                    <tr>
                      <th className="text-left p-3">Trader</th>
                      <th className="text-right p-3">Realized PnL</th>
                      <th className="text-right p-3">Win Rate</th>
                      <th className="text-right p-3">Trades</th>
                      <th className="text-right p-3">Volume</th>
                      <th className="text-right p-3">Open</th>
                      <th className="text-right p-3">Last Trade</th>
                    </tr>
                  </thead>
                  <tbody>
                    {attributionRows.map((row) => (
                      <tr key={row.wallet}>
                        <td className="p-3 text-xs">
                          <p className="text-white">{row.label}</p>
                          <p className="text-muted mono">{shortAddress(row.wallet)}</p>
                        </td>
                        <td
                          className={`p-3 text-right mono ${
                            row.realized_pnl >= 0 ? "status-good" : "status-bad"
                          }`}
                        >
                          {row.realized_pnl >= 0 ? "+" : ""}
                          {formatUSD(row.realized_pnl)}
                        </td>
                        <td className="p-3 text-right mono">
                          {row.win_rate == null ? (
                            <span className="text-muted">—</span>
                          ) : (
                            `${(row.win_rate * 100).toFixed(1)}%`
                          )}
                        </td>
                        <td className="p-3 text-right mono">{row.trade_count}</td>
                        <td className="p-3 text-right mono">{formatUSD(row.volume_usdc)}</td>
                        <td className="p-3 text-right mono">{row.open_positions}</td>
                        <td className="p-3 text-right text-muted text-xs">
                          {row.last_trade_at
                            ? new Date(row.last_trade_at).toLocaleDateString()
                            : "—"}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>

              {attribution?.totals && (
                <div className="flex flex-wrap items-center gap-x-6 gap-y-2 text-sm border-t border-[var(--line)] pt-3">
                  <span className="text-muted">
                    Total realized
                    <span
                      className={`mono ml-2 ${
                        attribution.totals.realized_pnl >= 0 ? "status-good" : "status-bad"
                      }`}
                    >
                      {attribution.totals.realized_pnl >= 0 ? "+" : ""}
                      {formatUSD(attribution.totals.realized_pnl)}
                    </span>
                  </span>
                  <span className="text-muted">
                    Copy trades
                    <span className="mono ml-2 text-white">{attribution.totals.trade_count}</span>
                  </span>
                  <span className="text-muted">
                    Win rate
                    <span className="mono ml-2 text-white">
                      {attribution.totals.win_rate == null
                        ? "—"
                        : `${(attribution.totals.win_rate * 100).toFixed(1)}%`}
                    </span>
                  </span>
                </div>
              )}
            </>
          )}
        </div>
      )}

      {activeTab === "copy" && (
        <div className="surface-panel p-6 rounded-lg space-y-4">
          <div className="flex items-center justify-between gap-3 flex-wrap">
            <h2 className="text-lg font-semibold">Copy Evaluation</h2>
            <div className="flex items-center gap-2">
              <select
                value={evaluationWallet}
                onChange={(e) => setEvaluationWallet(e.target.value)}
                className="bg-[var(--bg-soft)] border border-[var(--line)] rounded px-3 py-2 text-xs mono"
                disabled={following.length === 0}
              >
                {following.map((f) => (
                  <option key={f.id} value={f.trader_wallet}>
                    {getTraderPrimaryLabel(f)}
                  </option>
                ))}
              </select>
              <button
                onClick={() => evaluationWallet && fetchEvaluation(evaluationWallet)}
                className="btn-muted"
                disabled={!evaluationWallet || evaluationLoading}
              >
                {evaluationLoading ? "Refreshing..." : "Refresh"}
              </button>
            </div>
          </div>

          <p className="text-soft text-sm">
            Side-by-side source trader trades and your copied outcomes. Auto-refreshes every 15
            seconds.
            {evaluationUpdatedAt && (
              <span className="text-muted ml-2">
                · Updated {new Date(evaluationUpdatedAt).toLocaleTimeString()}
              </span>
            )}
          </p>

          {evaluationError && (
            <Banner
              variant="error"
              onDismiss={() => setEvaluationError(null)}
              onRetry={() => {
                if (evaluationWallet) fetchEvaluation(evaluationWallet);
              }}
            >
              {evaluationError}
            </Banner>
          )}

          {!evaluationWallet ? (
            <div className="surface-soft p-6 rounded text-sm text-soft">
              Follow a trader first to view evaluation.
            </div>
          ) : evaluationRows.length === 0 ? (
            <div className="surface-soft p-6 rounded text-sm text-soft">
              No source trades found yet for this trader.
            </div>
          ) : (
            <div className="overflow-x-auto">
              <table className="table-theme text-xs">
                <thead>
                  <tr>
                    <th className="text-right p-3">Time</th>
                    <th className="text-left p-3">Market</th>
                    <th className="text-center p-3">Side</th>
                    <th className="text-right p-3">Source Notional</th>
                    <th className="text-right p-3">Trader Wallet</th>
                    <th className="text-right p-3">Ratio</th>
                    <th className="text-right p-3">Copy Wallet Base</th>
                    <th className="text-right p-3">Copied Size</th>
                    <th className="text-center p-3">Status</th>
                    <th className="text-left p-3">Warning</th>
                  </tr>
                </thead>
                <tbody>
                  {evaluationRows.map((row) => (
                    <tr key={row.source_trade_id}>
                      <td className="p-3 text-right text-muted">
                        {row.source_timestamp
                          ? new Date(row.source_timestamp).toLocaleTimeString()
                          : "—"}
                      </td>
                      <td className="p-3 text-white truncate max-w-[210px]">
                        {row.market_id ? shortAddress(row.market_id) : "—"}
                      </td>
                      <td className="p-3 text-center">
                        <span
                          className={`text-xs font-semibold ${
                            row.side === "BUY" ? "status-good" : "status-bad"
                          }`}
                        >
                          {row.side}
                        </span>
                      </td>
                      <td className="p-3 text-right mono">
                        {formatUSD(row.source_trade_notional)}
                      </td>
                      <td className="p-3 text-right mono">
                        {row.trader_wallet_balance != null
                          ? formatUSD(row.trader_wallet_balance)
                          : "—"}
                      </td>
                      <td className="p-3 text-right mono">
                        {row.ratio != null ? formatPct(row.ratio) : "—"}
                      </td>
                      <td className="p-3 text-right mono">
                        {row.copy_wallet_base != null ? formatUSD(row.copy_wallet_base) : "—"}
                      </td>
                      <td className="p-3 text-right mono">
                        {row.copied_size != null ? formatUSD(row.copied_size) : "—"}
                      </td>
                      <td className="p-3 text-center">
                        <span
                          className={`chip text-[10px] ${
                            row.copy_status === "executed"
                              ? "chip-success"
                              : row.copy_status === "failed" || row.copy_status === "rejected"
                                ? "chip-danger"
                                : "bg-[var(--bg-soft)] text-muted"
                          }`}
                        >
                          {row.copy_status}
                        </span>
                      </td>
                      <td className="p-3 text-left">
                        {row.warning ? (
                          <span className="inline-block px-2 py-1 rounded bg-yellow-500/20 text-yellow-300 text-[10px]">
                            {row.warning}
                          </span>
                        ) : (
                          <span className="text-muted">—</span>
                        )}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </div>
      )}

      {activeTab === "copy" && (
        <div className="surface-panel p-6 rounded-lg" data-tutorial="copy-trading">
          <h2 className="text-lg font-semibold mb-4">Followed Traders</h2>
          {following.length === 0 ? (
            <p className="text-soft text-sm">
              You&apos;re not following any traders yet. Visit the{" "}
              <span className="text-[var(--accent)] font-semibold">Leaderboard</span> to find
              traders to follow.
            </p>
          ) : (
            <div className="space-y-4">
              {following.map((f) => {
                const key = f.trader_wallet.toLowerCase();
                const draft = traderConfigDrafts[key] ?? defaultTraderConfig(f);
                const saving = savingConfigWallet === key;
                const saveStatus: SaveStatus =
                  saveStatusByWallet[key] ??
                  (isDraftDirtyForWallet(key, draft) ? "unsaved" : "idle");
                const saveStatusMessage = saveStatusMessageByWallet[key];
                const isMocked = hasMockCopyTrader(mockTraders, key);
                const mockIndicator = mockIndicatorByWallet[key];

                return (
                  <div key={f.id} className="surface-soft p-4 rounded-lg space-y-4">
                    <div className="flex items-start justify-between gap-3 flex-wrap">
                      <div>
                        <p className="text-white font-medium">{getTraderPrimaryLabel(f)}</p>
                        <p className="text-muted text-xs mono">{shortAddress(f.trader_wallet)}</p>
                        {getPolymarketProfileUrl(f.trader_wallet) && (
                          <a
                            href={getPolymarketProfileUrl(f.trader_wallet) || "#"}
                            target="_blank"
                            rel="noopener noreferrer"
                            className="text-[11px] text-[var(--accent)] hover:underline"
                          >
                            Polymarket Profile
                          </a>
                        )}
                      </div>
                      <div className="flex items-center gap-2 flex-wrap">
                        <span className="chip chip-success text-xs">Active</span>
                        {getSaveStatusChip(saveStatus)}
                        {isMocked && <span className="chip chip-success text-xs">Watchlisted</span>}
                        {mockIndicator === "already_exists" && (
                          <span className="chip chip-warning text-xs">Already on watchlist</span>
                        )}
                        {mockIndicator === "added" && (
                          <span className="chip chip-success text-xs">Added to watchlist</span>
                        )}
                        <button
                          onClick={() =>
                            handleAddFollowedToMock(
                              f.trader_wallet,
                              f.trader_alias || f.display_name || null,
                            )
                          }
                          className={`px-3 py-1 rounded text-xs font-semibold transition ${
                            isMocked
                              ? "bg-yellow-500/20 text-yellow-300 hover:bg-yellow-500/30"
                              : "bg-[var(--accent-soft)] text-[var(--accent)] hover:opacity-90"
                          }`}
                        >
                          {isMocked ? "On watchlist" : "Add to Watchlist"}
                        </button>
                        <button
                          onClick={() => openConfirmModal("unfollow", f.trader_wallet)}
                          className="px-3 py-1 rounded text-xs font-semibold bg-red-500/20 text-red-400 hover:bg-red-500/30 transition"
                        >
                          Unfollow
                        </button>
                      </div>
                    </div>

                    <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
                      <label className="text-xs text-muted">
                        Wallet Username
                        <input
                          type="text"
                          maxLength={100}
                          value={draft.trader_alias ?? f.display_name ?? ""}
                          onChange={(e) =>
                            updateTraderDraft(f.trader_wallet, {
                              trader_alias:
                                f.display_name &&
                                e.target.value.trim().toLowerCase() ===
                                  f.display_name.trim().toLowerCase()
                                  ? null
                                  : e.target.value,
                            })
                          }
                          className="mt-1 w-full bg-[var(--bg-soft)] border border-[var(--line)] rounded px-2 py-2 text-xs"
                          placeholder="Defaults to Polymarket username"
                        />
                      </label>
                    </div>

                    <div className="grid grid-cols-1 md:grid-cols-3 gap-3">
                      <label className="text-xs text-muted">
                        Sizing Mode
                        <select
                          value={draft.sizing_mode || "inherit_global"}
                          onChange={(e) =>
                            updateTraderDraft(f.trader_wallet, {
                              sizing_mode: e.target.value as TraderConfigDraft["sizing_mode"],
                            })
                          }
                          className="mt-1 w-full bg-[var(--bg-soft)] border border-[var(--line)] rounded px-2 py-2 text-xs"
                        >
                          {SIZING_MODE_OPTIONS.map((opt) => (
                            <option key={opt.value} value={opt.value}>
                              {opt.label}
                            </option>
                          ))}
                        </select>
                      </label>

                      <label className="text-xs text-muted">
                        Max Position (Safety Cap)
                        <input
                          type="number"
                          min="0"
                          step="0.01"
                          value={draft.max_position_size ?? ""}
                          onChange={(e) =>
                            updateTraderDraft(f.trader_wallet, {
                              max_position_size: e.target.value ? Number(e.target.value) : null,
                            })
                          }
                          className="mt-1 w-full bg-[var(--bg-soft)] border border-[var(--line)] rounded px-2 py-2 text-xs"
                          placeholder="Optional"
                        />
                      </label>

                      <label className="text-xs text-muted">
                        Fixed Amount Override
                        <input
                          type="number"
                          min="0"
                          step="0.01"
                          value={draft.fixed_trade_amount_override ?? ""}
                          onChange={(e) =>
                            updateTraderDraft(f.trader_wallet, {
                              fixed_trade_amount_override: e.target.value
                                ? Number(e.target.value)
                                : null,
                            })
                          }
                          className="mt-1 w-full bg-[var(--bg-soft)] border border-[var(--line)] rounded px-2 py-2 text-xs"
                          placeholder="Used by fixed mode"
                        />
                      </label>
                    </div>

                    <div className="grid grid-cols-1 md:grid-cols-3 gap-3">
                      <label className="text-xs text-muted">
                        Copy Wallet Basis
                        <select
                          value={draft.copy_wallet_mode || "dynamic_main_wallet_percentage"}
                          onChange={(e) =>
                            updateTraderDraft(f.trader_wallet, {
                              copy_wallet_mode: e.target
                                .value as TraderConfigDraft["copy_wallet_mode"],
                            })
                          }
                          className="mt-1 w-full bg-[var(--bg-soft)] border border-[var(--line)] rounded px-2 py-2 text-xs"
                        >
                          {COPY_WALLET_MODE_OPTIONS.map((opt) => (
                            <option key={opt.value} value={opt.value}>
                              {opt.label}
                            </option>
                          ))}
                        </select>
                      </label>

                      <label className="text-xs text-muted">
                        Copy Wallet %
                        <input
                          type="number"
                          min="0"
                          max="100"
                          step="0.1"
                          value={draft.copy_wallet_percentage ?? ""}
                          onChange={(e) =>
                            updateTraderDraft(f.trader_wallet, {
                              copy_wallet_percentage: e.target.value
                                ? Number(e.target.value)
                                : null,
                            })
                          }
                          className="mt-1 w-full bg-[var(--bg-soft)] border border-[var(--line)] rounded px-2 py-2 text-xs"
                          placeholder="For dynamic mode"
                          disabled={draft.copy_wallet_mode === "fixed_snapshot_amount"}
                        />
                      </label>

                      <label className="text-xs text-muted">
                        Copy Wallet Fixed ($)
                        <input
                          type="number"
                          min="0"
                          step="0.01"
                          value={draft.copy_wallet_fixed_amount ?? ""}
                          onChange={(e) =>
                            updateTraderDraft(f.trader_wallet, {
                              copy_wallet_fixed_amount: e.target.value
                                ? Number(e.target.value)
                                : null,
                            })
                          }
                          className="mt-1 w-full bg-[var(--bg-soft)] border border-[var(--line)] rounded px-2 py-2 text-xs"
                          placeholder="For fixed snapshot mode"
                          disabled={draft.copy_wallet_mode !== "fixed_snapshot_amount"}
                        />
                      </label>
                    </div>

                    {saveStatus === "error" && saveStatusMessage && (
                      <p className="text-xs text-red-300">{saveStatusMessage}</p>
                    )}

                    <div className="flex justify-end">
                      <button
                        onClick={() => openConfirmModal("save", f.trader_wallet)}
                        disabled={saving}
                        className="btn-accent disabled:opacity-60"
                      >
                        {saving ? "Saving..." : "Save Copy Config"}
                      </button>
                    </div>
                  </div>
                );
              })}
            </div>
          )}
        </div>
      )}

      {activeTab === "copy" && (
        <div className="surface-panel overflow-hidden rounded-lg">
          <div className="p-4 border-b border-[var(--line)]">
            <h2 className="text-lg font-semibold">Recent Copy Trades</h2>
          </div>
          {copyTrades.length === 0 ? (
            <div className="p-8 text-center text-soft text-sm">
              No copy trades executed yet. Trades will appear here when followed traders make moves.
            </div>
          ) : (
            <div className="overflow-x-auto">
              <table className="table-theme text-sm">
                <thead>
                  <tr>
                    <th className="text-left p-3">Trader</th>
                    <th className="text-left p-3">Market</th>
                    <th className="text-center p-3">Side</th>
                    <th className="text-right p-3">Size</th>
                    <th className="text-right p-3">Price</th>
                    <th className="text-center p-3">Status</th>
                    <th className="text-left p-3">Calc</th>
                    <th className="text-right p-3">P&L</th>
                    <th className="text-right p-3">Time</th>
                  </tr>
                </thead>
                <tbody>
                  {copyTrades.map((t) => (
                    <tr key={t.id}>
                      <td className="p-3 text-xs">
                        <p className="text-white">{getTraderLabelByWallet(t.trader_wallet)}</p>
                        <p className="text-muted mono">{shortAddress(t.trader_wallet)}</p>
                      </td>
                      <td className="p-3 text-white text-xs truncate max-w-[200px]">
                        {t.market_id ? shortAddress(t.market_id) : "—"}
                      </td>
                      <td className="p-3 text-center">
                        <span
                          className={`text-xs font-semibold ${
                            t.side === "BUY" ? "status-good" : "status-bad"
                          }`}
                        >
                          {t.side}
                        </span>
                      </td>
                      <td className="p-3 text-right mono">{formatUSD(t.size)}</td>
                      <td className="p-3 text-right mono">${t.price.toFixed(3)}</td>
                      <td className="p-3 text-center">
                        <span
                          className={`chip text-xs ${
                            t.status === "executed"
                              ? "chip-success"
                              : t.status === "failed" || t.status === "rejected"
                                ? "chip-danger"
                                : "bg-[var(--bg-soft)] text-muted"
                          }`}
                        >
                          {t.status}
                        </span>
                      </td>
                      <td className="p-3 text-left text-xs">
                        {t.calculation_warning ? (
                          <span className="inline-block px-2 py-1 rounded bg-yellow-500/20 text-yellow-300">
                            {t.calculation_warning}
                          </span>
                        ) : (
                          <span className="text-muted">—</span>
                        )}
                      </td>
                      <td className="p-3 text-right mono">
                        {t.pnl != null ? (
                          <span className={t.pnl >= 0 ? "status-good" : "status-bad"}>
                            {t.pnl >= 0 ? "+" : ""}
                            {formatUSD(t.pnl)}
                          </span>
                        ) : (
                          "—"
                        )}
                      </td>
                      <td className="p-3 text-right text-muted text-xs">
                        {t.timestamp ? new Date(t.timestamp).toLocaleTimeString() : "—"}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </div>
      )}

      <ConfirmModal
        open={Boolean(confirmState.open && confirmState.type && confirmState.wallet)}
        title={confirmState.type === "save" ? "Confirm Save Configuration" : "Confirm Unfollow"}
        body={
          confirmState.type === "save" ? (
            <p className="text-sm text-soft">
              {`Are you sure you want to save this configuration for ${getTraderLabelByWallet(
                confirmState.wallet ?? "",
              )}?`}
            </p>
          ) : (
            <p className="text-sm text-soft">
              {`Are you sure you want to unfollow ${getTraderLabelByWallet(
                confirmState.wallet ?? "",
              )}? Copying will stop.`}
            </p>
          )
        }
        destructive={confirmState.type === "unfollow"}
        confirmDisabled={confirmLoading}
        confirmLabel={
          confirmLoading
            ? confirmState.type === "save"
              ? "Saving..."
              : "Unfollowing..."
            : confirmState.type === "save"
              ? "Confirm Save"
              : "Confirm Unfollow"
        }
        onConfirm={handleConfirmAction}
        onCancel={closeConfirmModal}
      />

      <ConfirmModal
        open={mockRemoveWallet !== null}
        title="Remove from watchlist?"
        body="Removing this wallet removes it from your local watchlist. No funds or returns are involved."
        destructive
        confirmLabel="Remove"
        onConfirm={() => {
          const wallet = mockRemoveWallet;
          setMockRemoveWallet(null);
          if (wallet) handleRemoveMockTrader(wallet);
        }}
        onCancel={() => setMockRemoveWallet(null)}
      />

      <ConfirmModal
        open={mockResetConfirmOpen}
        title="Reset watchlist?"
        body="This removes every watched wallet from this browser. No funds or returns are involved."
        destructive
        confirmLabel="Reset"
        onConfirm={() => {
          setMockResetConfirmOpen(false);
          handleResetWatchlist();
        }}
        onCancel={() => setMockResetConfirmOpen(false)}
      />
    </div>
  );
}
