/**
 * Settings screen with Profile, Trading, and (admin-only) Admin Settings tabs.
 *
 * Profile covers avatar, contact details, and personal LLM provider/model preferences; Trading
 * edits copy-trading risk controls and inverse-bot sizing; Admin manages system-wide LLM defaults
 * and per-user overrides. Initial loads use `Promise.allSettled` so one failing endpoint (backend
 * status, LLM providers) does not blank the whole page.
 *
 * @module pages/SettingsPage
 */

import React, { useState, useEffect, useRef } from "react";
import { useAuthStore } from "../store/authStore";
import {
  settingsService,
  UserSettings,
  BackendsStatus,
  RiskMode,
  InverseBotSizeMode,
  UserProfile,
  AdminProviderStatus,
  AdminProvidersResponse,
  AdminUserLLMSettings,
  AdminUsersLLMResponse,
  LLMProvidersResponse,
  LLMCurrentSettings,
} from "../services/settingsService";
import {
  CreateChannelRequest,
  NotificationChannel,
  NotificationChannelType,
  NotificationPreference,
  createNotificationChannel,
  deleteNotificationChannel,
  getNotificationPreferences,
  listNotificationChannels,
  sendTestAlert,
  updateNotificationPreferences,
} from "../services/notificationsService";
import { getApiErrorMessage } from "../utils/apiError";
import { tradingKeyService } from "../services/tradingKeyService";
import { useTradingKeyStatus } from "../hooks/useTradingKeyStatus";

type AIBackend = "llm_chain" | "cli_agent";
type SettingsTab = "profile" | "trading" | "notifications" | "admin";

const RISK_MODES: { value: RiskMode; label: string; description: string }[] = [
  {
    value: "max_position_daily_loss",
    label: "Max Position + Daily Loss",
    description: "Cap each trade size and stop copying if daily loss limit is hit",
  },
  {
    value: "percentage_mirror",
    label: "Percentage Mirror",
    description: "Copy a fixed percentage of the trader's position size",
  },
  {
    value: "fixed_amount",
    label: "Fixed Amount",
    description: "Use a fixed dollar amount for every copied trade",
  },
];

const INVERSE_SIZE_MODES: {
  value: InverseBotSizeMode;
  label: string;
  description: string;
}[] = [
  {
    value: "full_notional",
    label: "Full Notional",
    description: "Use sell proceeds (minus 1.5% buffer) for the buy leg",
  },
  {
    value: "fixed_amount",
    label: "Fixed Amount",
    description: "Always buy with a fixed USDC amount",
  },
];

export default function SettingsPage() {
  const { walletAddress, isAdmin } = useAuthStore();
  const [activeTab, setActiveTab] = useState<SettingsTab>("profile");
  const [settings, setSettings] = useState<UserSettings | null>(null);
  const [backendsStatus, setBackendsStatus] = useState<BackendsStatus | null>(null);
  const [profile, setProfile] = useState<UserProfile | null>(null);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [successMessage, setSuccessMessage] = useState<string | null>(null);

  // Admin-specific state
  const [adminProviders, setAdminProviders] = useState<AdminProvidersResponse | null>(null);
  const [adminUsers, setAdminUsers] = useState<AdminUsersLLMResponse | null>(null);
  const [adminLoading, setAdminLoading] = useState(false);
  const [llmProviders, setLlmProviders] = useState<LLMProvidersResponse | null>(null);
  const [currentLlmSettings, setCurrentLlmSettings] = useState<LLMCurrentSettings | null>(null);
  const [llmProviderChoice, setLlmProviderChoice] = useState("");
  const [llmModelChoice, setLlmModelChoice] = useState("");
  const [llmDirty, setLlmDirty] = useState(false);

  // Profile form local state
  const [displayName, setDisplayName] = useState("");
  const [email, setEmail] = useState("");
  const [phone, setPhone] = useState("");
  const [profileDirty, setProfileDirty] = useState(false);
  const fileInputRef = useRef<HTMLInputElement>(null);

  // Notification channel / preference state
  const [notifChannels, setNotifChannels] = useState<NotificationChannel[]>([]);
  const [notifPreferences, setNotifPreferences] = useState<NotificationPreference[]>([]);
  const [notifLoading, setNotifLoading] = useState(false);
  const [notifSaving, setNotifSaving] = useState(false);
  const [channelType, setChannelType] = useState<NotificationChannelType>("telegram");
  const [channelName, setChannelName] = useState("");
  const [chatId, setChatId] = useState("");
  const [discordUrl, setDiscordUrl] = useState("");
  const [webhookUrl, setWebhookUrl] = useState("");
  const [webhookSecret, setWebhookSecret] = useState("");
  const [testingChannelId, setTestingChannelId] = useState<number | null>(null);
  const [prefsDirty, setPrefsDirty] = useState(false);

  // Trading key (auto-trading) state
  const { hasTradingKey, refresh: refreshTradingKeyStatus } = useTradingKeyStatus();
  const [tradingPrivateKey, setTradingPrivateKey] = useState("");
  const [clobCredentialsJson, setClobCredentialsJson] = useState("");
  const [tradingKeySaving, setTradingKeySaving] = useState(false);
  const [tradingKeyError, setTradingKeyError] = useState<string | null>(null);

  useEffect(() => {
    loadSettings();
  }, []);

  // Load admin data when switching to admin tab
  useEffect(() => {
    if (activeTab === "admin" && isAdmin && !adminProviders) {
      loadAdminData();
    }
  }, [activeTab, isAdmin]);

  // Load notification channels/preferences when switching to the tab
  useEffect(() => {
    if (activeTab === "notifications" && notifChannels.length === 0) {
      loadNotificationData();
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [activeTab]);

  const loadNotificationData = async () => {
    try {
      setNotifLoading(true);
      const [channelsResult, preferencesResult] = await Promise.allSettled([
        listNotificationChannels(),
        getNotificationPreferences(),
      ]);
      if (channelsResult.status === "fulfilled") {
        setNotifChannels(channelsResult.value);
      } else {
        setError(getApiErrorMessage(channelsResult.reason, "Failed to load channels"));
      }
      if (preferencesResult.status === "fulfilled") {
        setNotifPreferences(preferencesResult.value);
      }
    } catch (err: unknown) {
      setError(getApiErrorMessage(err, "Failed to load notification settings"));
    } finally {
      setNotifLoading(false);
    }
  };

  const handleCreateChannel = async () => {
    try {
      setNotifSaving(true);
      setError(null);
      const request: CreateChannelRequest = {
        channel_type: channelType,
        name: channelName,
      };
      if (channelType === "telegram") request.chat_id = chatId;
      if (channelType === "discord") request.webhook_url = discordUrl;
      if (channelType === "webhook") {
        request.url = webhookUrl;
        request.secret = webhookSecret;
      }
      const created = await createNotificationChannel(request);
      setNotifChannels((prev) => [...prev, created]);
      setChannelName("");
      setChatId("");
      setDiscordUrl("");
      setWebhookUrl("");
      setWebhookSecret("");
      setSuccessMessage("Notification channel created!");
      setTimeout(() => setSuccessMessage(null), 3000);
    } catch (err: unknown) {
      setError(getApiErrorMessage(err, "Failed to create notification channel"));
    } finally {
      setNotifSaving(false);
    }
  };

  const handleDeleteChannel = async (id: number) => {
    try {
      setNotifSaving(true);
      setError(null);
      await deleteNotificationChannel(id);
      setNotifChannels((prev) => prev.filter((c) => c.id !== id));
      setNotifPreferences((prev) =>
        prev.map((p) => ({
          ...p,
          channel_ids: p.channel_ids.filter((cid) => cid !== id),
        })),
      );
      setSuccessMessage("Notification channel deleted!");
      setTimeout(() => setSuccessMessage(null), 3000);
    } catch (err: unknown) {
      setError(getApiErrorMessage(err, "Failed to delete notification channel"));
    } finally {
      setNotifSaving(false);
    }
  };

  const handleTestChannel = async (id: number) => {
    try {
      setTestingChannelId(id);
      setError(null);
      const result = await sendTestAlert({ channel_id: id });
      if (result.status === "dispatched") {
        setSuccessMessage("Test alert sent — check your channel.");
      } else {
        setError(`Test alert not delivered (${result.reason || result.status}).`);
      }
      setTimeout(() => setSuccessMessage(null), 3000);
    } catch (err: unknown) {
      setError(getApiErrorMessage(err, "Failed to send test alert"));
    } finally {
      setTestingChannelId(null);
    }
  };

  const handleTogglePreferenceChannel = (eventType: string, channelId: number) => {
    setNotifPreferences((prev) =>
      prev.map((p) => {
        if (p.event_type !== eventType) return p;
        const has = p.channel_ids.includes(channelId);
        return {
          ...p,
          channel_ids: has
            ? p.channel_ids.filter((cid) => cid !== channelId)
            : [...p.channel_ids, channelId],
          is_default: false,
        };
      }),
    );
    setPrefsDirty(true);
  };

  const handleTogglePreferenceEnabled = (eventType: string) => {
    setNotifPreferences((prev) =>
      prev.map((p) =>
        p.event_type === eventType ? { ...p, enabled: !p.enabled, is_default: false } : p,
      ),
    );
    setPrefsDirty(true);
  };

  const handleSavePreferences = async () => {
    try {
      setNotifSaving(true);
      setError(null);
      // Untouched defaults (no channels selected, enabled) must not be
      // persisted — an empty channel_ids row would silence the event type,
      // whereas the absence of a row means "all active channels".
      const toSave = notifPreferences.filter(
        (p) => !p.is_default || p.channel_ids.length > 0 || !p.enabled,
      );
      const updated = await updateNotificationPreferences(toSave);
      setNotifPreferences(updated);
      setPrefsDirty(false);
      setSuccessMessage("Notification preferences saved!");
      setTimeout(() => setSuccessMessage(null), 3000);
    } catch (err: unknown) {
      setError(getApiErrorMessage(err, "Failed to save notification preferences"));
    } finally {
      setNotifSaving(false);
    }
  };

  // Sync local profile form when profile loads
  useEffect(() => {
    if (profile) {
      setDisplayName(profile.display_name || "");
      setEmail(profile.email || "");
      setPhone(profile.phone || "");
      setProfileDirty(false);
    }
  }, [profile]);

  useEffect(() => {
    if (!currentLlmSettings) return;
    setLlmProviderChoice(currentLlmSettings.provider || "");
    setLlmModelChoice(currentLlmSettings.model || "");
    setLlmDirty(false);
  }, [currentLlmSettings]);

  const loadSettings = async () => {
    try {
      setLoading(true);
      setError(null);
      // Use allSettled so a single failure doesn't block the rest
      const [settingsResult, statusResult, profileResult, llmProvidersResult, llmCurrentResult] =
        await Promise.allSettled([
          settingsService.getUserSettings(),
          settingsService.getBackendsStatus(),
          settingsService.getUserProfile(),
          settingsService.getLLMProviders(),
          settingsService.getCurrentLLMSettings(),
        ]);
      if (settingsResult.status === "fulfilled") {
        setSettings(settingsResult.value);
      } else {
        console.error("Failed to load user settings:", settingsResult.reason);
        setError(getApiErrorMessage(settingsResult.reason, "Failed to load user settings"));
      }
      if (statusResult.status === "fulfilled") {
        setBackendsStatus(statusResult.value);
      } else {
        console.warn("Failed to load backend status (non-critical)");
      }
      if (profileResult.status === "fulfilled") {
        setProfile(profileResult.value);
      } else {
        console.error("Failed to load profile:", profileResult.reason);
      }
      if (llmProvidersResult.status === "fulfilled") {
        setLlmProviders(llmProvidersResult.value);
      } else {
        console.warn("Failed to load LLM providers (non-critical)");
      }
      if (llmCurrentResult.status === "fulfilled") {
        setCurrentLlmSettings(llmCurrentResult.value);
      } else {
        console.warn("Failed to load current LLM settings (non-critical)");
      }
    } catch (err: unknown) {
      setError(getApiErrorMessage(err, "Failed to load settings"));
    } finally {
      setLoading(false);
    }
  };

  const loadAdminData = async () => {
    try {
      setAdminLoading(true);
      setError(null);
      const [providersResult, usersResult] = await Promise.allSettled([
        settingsService.getAdminProviders(),
        settingsService.getAdminUsersLLM(0, 50),
      ]);
      if (providersResult.status === "fulfilled") {
        setAdminProviders(providersResult.value);
      } else {
        console.error("Failed to load providers:", providersResult.reason);
        setError(getApiErrorMessage(providersResult.reason, "Failed to load LLM providers"));
      }
      if (usersResult.status === "fulfilled") {
        setAdminUsers(usersResult.value);
      } else {
        console.error("Failed to load admin users:", usersResult.reason);
        setError(getApiErrorMessage(usersResult.reason, "Failed to load admin users"));
      }
    } catch (err: unknown) {
      setError(getApiErrorMessage(err, "Failed to load admin data"));
    } finally {
      setAdminLoading(false);
    }
  };

  const handleUpdateDefaults = async (provider?: string, model?: string) => {
    try {
      setSaving(true);
      setError(null);
      const result = await settingsService.updateAdminDefaults({
        default_provider: provider,
        default_model: model,
      });
      if (adminProviders) {
        setAdminProviders({
          ...adminProviders,
          default_provider: result.default_provider,
          default_model: result.default_model,
        });
      }
      setSuccessMessage("System defaults updated!");
      setTimeout(() => setSuccessMessage(null), 3000);
    } catch (err: unknown) {
      setError(getApiErrorMessage(err, "Failed to update defaults"));
    } finally {
      setSaving(false);
    }
  };

  const handleUpdateUserLLM = async (
    userId: number,
    provider: string | null,
    model: string | null,
  ) => {
    try {
      setSaving(true);
      setError(null);
      const updated = await settingsService.updateAdminUserLLM(userId, {
        preferred_llm_provider: provider,
        preferred_llm_model: model,
      });
      if (adminUsers) {
        setAdminUsers({
          ...adminUsers,
          users: adminUsers.users.map((u) => (u.user_id === userId ? updated : u)),
        });
      }
      setSuccessMessage("User settings updated!");
      setTimeout(() => setSuccessMessage(null), 3000);
    } catch (err: unknown) {
      setError(getApiErrorMessage(err, "Failed to update user settings"));
    } finally {
      setSaving(false);
    }
  };

  const handleBackendChange = async (backend: AIBackend) => {
    if (!settings) return;
    try {
      setSaving(true);
      setError(null);
      setSuccessMessage(null);
      const updated = await settingsService.updateUserSettings({
        ai_backend: backend,
      });
      setSettings(updated);
      setSuccessMessage("Settings saved!");
      setTimeout(() => setSuccessMessage(null), 3000);
    } catch (err: unknown) {
      setError(getApiErrorMessage(err, "Failed to save settings"));
    } finally {
      setSaving(false);
    }
  };

  const handleProfileSave = async () => {
    try {
      setSaving(true);
      setError(null);
      setSuccessMessage(null);
      const updated = await settingsService.updateUserProfile({
        display_name: displayName,
        email: email || null,
        phone: phone || null,
      });
      setProfile(updated);
      setSuccessMessage("Profile saved!");
      setTimeout(() => setSuccessMessage(null), 3000);
    } catch (err: unknown) {
      setError(getApiErrorMessage(err, "Failed to save profile"));
    } finally {
      setSaving(false);
    }
  };

  const handleSaveLlmSettings = async () => {
    try {
      setSaving(true);
      setError(null);
      setSuccessMessage(null);
      const updated = await settingsService.updateLLMSettings({
        provider: llmProviderChoice || null,
        model: llmModelChoice || null,
      });
      setCurrentLlmSettings(updated);
      setLlmDirty(false);
      setSuccessMessage("LLM preferences saved!");
      setTimeout(() => setSuccessMessage(null), 3000);
    } catch (err: unknown) {
      setError(getApiErrorMessage(err, "Failed to save LLM preferences"));
    } finally {
      setSaving(false);
    }
  };

  const handleAvatarChange = async (e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0];
    if (!file) return;
    if (file.size > 512_000) {
      setError("Image must be under 500 KB");
      return;
    }
    const reader = new FileReader();
    reader.onload = async () => {
      try {
        setSaving(true);
        setError(null);
        const dataUrl = reader.result as string;
        const updated = await settingsService.updateUserProfile({
          profile_picture_url: dataUrl,
        });
        setProfile(updated);
        setSuccessMessage("Avatar updated!");
        setTimeout(() => setSuccessMessage(null), 3000);
      } catch (err: unknown) {
        setError(getApiErrorMessage(err, "Failed to upload avatar"));
      } finally {
        setSaving(false);
      }
    };
    reader.readAsDataURL(file);
  };

  const handleRemoveAvatar = async () => {
    try {
      setSaving(true);
      const updated = await settingsService.updateUserProfile({
        profile_picture_url: "",
      });
      setProfile(updated);
      setSuccessMessage("Avatar removed!");
      setTimeout(() => setSuccessMessage(null), 3000);
    } catch (err: unknown) {
      setError(getApiErrorMessage(err, "Failed to remove avatar"));
    } finally {
      setSaving(false);
    }
  };

  const handleCopyTradingUpdate = async (field: string, value: boolean | string | number) => {
    if (!settings) return;
    try {
      setSaving(true);
      setError(null);
      setSuccessMessage(null);
      const updated = await settingsService.updateCopyTradingSettings({
        [field]: value,
      });
      setSettings(updated);
      setSuccessMessage("Copy-trading settings saved!");
      setTimeout(() => setSuccessMessage(null), 3000);
    } catch (err: unknown) {
      setError(getApiErrorMessage(err, "Failed to save copy-trading settings"));
    } finally {
      setSaving(false);
    }
  };

  const handleConnectTradingKey = async () => {
    try {
      setTradingKeySaving(true);
      setTradingKeyError(null);
      let clobCredentials: Record<string, string> | undefined;
      if (clobCredentialsJson.trim()) {
        const parsed: unknown = JSON.parse(clobCredentialsJson);
        if (typeof parsed !== "object" || parsed === null || Array.isArray(parsed)) {
          throw new Error("CLOB credentials must be a JSON object");
        }
        clobCredentials = parsed as Record<string, string>;
      }
      await tradingKeyService.connectTradingKey(tradingPrivateKey, clobCredentials);
      setTradingPrivateKey("");
      setClobCredentialsJson("");
      await refreshTradingKeyStatus();
      setSuccessMessage("Trading key connected!");
      setTimeout(() => setSuccessMessage(null), 3000);
    } catch (err: unknown) {
      setTradingKeyError(getApiErrorMessage(err, "Failed to connect trading key"));
    } finally {
      setTradingKeySaving(false);
    }
  };

  const handleDisconnectTradingKey = async () => {
    try {
      setTradingKeySaving(true);
      setTradingKeyError(null);
      await tradingKeyService.disconnectTradingKey();
      await refreshTradingKeyStatus();
      setSuccessMessage("Trading key disconnected!");
      setTimeout(() => setSuccessMessage(null), 3000);
    } catch (err: unknown) {
      setTradingKeyError(getApiErrorMessage(err, "Failed to disconnect trading key"));
    } finally {
      setTradingKeySaving(false);
    }
  };

  const handleInverseBotToggle = async (enabled: boolean) => {
    if (enabled && hasTradingKey === false) {
      setError(
        "The inverse position bot requires a trading key. Connect one in the Trading key section above.",
      );
      return;
    }
    await handleCopyTradingUpdate("inverse_bot_enabled", enabled);
  };

  const selectedLlmProviderInfo = llmProviders?.providers.find(
    (p) => p.id === (llmProviderChoice || currentLlmSettings?.effective_provider),
  );
  const availableUserModels =
    selectedLlmProviderInfo?.models || currentLlmSettings?.available_models || [];

  if (loading) {
    return (
      <div className="flex items-center justify-center" style={{ minHeight: "calc(100vh - 8rem)" }}>
        <div className="animate-spin rounded-full h-12 w-12 border-t-2 border-b-2 border-[var(--accent)]"></div>
      </div>
    );
  }

  return (
    <div className="max-w-2xl mx-auto space-y-6">
      <h1 className="text-3xl font-bold">Settings</h1>

      {error && <div className="alert-error px-4 py-3 rounded">{error}</div>}
      {successMessage && <div className="alert-success px-4 py-3 rounded">{successMessage}</div>}

      {/* Tab Navigation */}
      <div className="flex gap-1 border-b border-[var(--line)]">
        <button
          onClick={() => setActiveTab("profile")}
          className={`px-4 py-2 text-sm font-medium transition-colors border-b-2 -mb-px ${
            activeTab === "profile"
              ? "border-[var(--accent)] text-[var(--accent)]"
              : "border-transparent text-muted hover:text-white"
          }`}
        >
          Profile
        </button>
        <button
          onClick={() => setActiveTab("trading")}
          className={`px-4 py-2 text-sm font-medium transition-colors border-b-2 -mb-px ${
            activeTab === "trading"
              ? "border-[var(--accent)] text-[var(--accent)]"
              : "border-transparent text-muted hover:text-white"
          }`}
        >
          Trading
        </button>
        <button
          onClick={() => setActiveTab("notifications")}
          className={`px-4 py-2 text-sm font-medium transition-colors border-b-2 -mb-px ${
            activeTab === "notifications"
              ? "border-[var(--accent)] text-[var(--accent)]"
              : "border-transparent text-muted hover:text-white"
          }`}
        >
          Notifications
        </button>
        {isAdmin && (
          <button
            onClick={() => setActiveTab("admin")}
            className={`px-4 py-2 text-sm font-medium transition-colors border-b-2 -mb-px ${
              activeTab === "admin"
                ? "border-[var(--accent)] text-[var(--accent)]"
                : "border-transparent text-muted hover:text-white"
            }`}
          >
            Admin Settings
          </button>
        )}
      </div>

      {/* Profile Tab */}
      {activeTab === "profile" && (
        <>
          {/* Profile Section */}
          <div className="surface-panel p-6">
            <h2 className="text-xl font-semibold mb-4">Profile</h2>
            <div className="flex items-start gap-6 mb-6">
              {/* Avatar */}
              <div className="flex flex-col items-center gap-2">
                <button
                  type="button"
                  onClick={() => fileInputRef.current?.click()}
                  className="relative w-20 h-20 rounded-full overflow-hidden border-2 border-[var(--line)] hover:border-[var(--accent)] transition-all group flex-shrink-0"
                  title="Change profile picture"
                >
                  {profile?.profile_picture_url ? (
                    <img
                      src={profile.profile_picture_url}
                      alt="Avatar"
                      className="w-full h-full object-cover"
                    />
                  ) : (
                    <div className="w-full h-full bg-[var(--accent-soft)] flex items-center justify-center text-2xl font-bold text-[var(--accent)]">
                      {(profile?.display_name || walletAddress || "?")[0].toUpperCase()}
                    </div>
                  )}
                  <div className="absolute inset-0 bg-black/50 opacity-0 group-hover:opacity-100 transition-opacity flex items-center justify-center">
                    <svg
                      xmlns="http://www.w3.org/2000/svg"
                      className="h-6 w-6 text-white"
                      fill="none"
                      viewBox="0 0 24 24"
                      stroke="currentColor"
                    >
                      <path
                        strokeLinecap="round"
                        strokeLinejoin="round"
                        strokeWidth={2}
                        d="M3 9a2 2 0 012-2h.93a2 2 0 001.664-.89l.812-1.22A2 2 0 0110.07 4h3.86a2 2 0 011.664.89l.812 1.22A2 2 0 0018.07 7H19a2 2 0 012 2v9a2 2 0 01-2 2H5a2 2 0 01-2-2V9z"
                      />
                      <path
                        strokeLinecap="round"
                        strokeLinejoin="round"
                        strokeWidth={2}
                        d="M15 13a3 3 0 11-6 0 3 3 0 016 0z"
                      />
                    </svg>
                  </div>
                </button>
                {profile?.profile_picture_url && (
                  <button
                    type="button"
                    onClick={handleRemoveAvatar}
                    className="text-xs text-muted hover:text-red-400 transition-colors"
                    disabled={saving}
                  >
                    Remove
                  </button>
                )}
                <input
                  ref={fileInputRef}
                  type="file"
                  accept="image/*"
                  onChange={handleAvatarChange}
                  className="hidden"
                />
              </div>

              {/* Name / Contact Fields */}
              <div className="flex-1 space-y-4">
                <div>
                  <label className="block text-sm font-medium mb-1">Display Name</label>
                  <input
                    type="text"
                    value={displayName}
                    onChange={(e) => {
                      setDisplayName(e.target.value);
                      setProfileDirty(true);
                    }}
                    placeholder={walletAddress || "Username"}
                    disabled={saving}
                    className="w-full px-3 py-2 rounded-md bg-[var(--bg-soft)] border border-[var(--line)] text-white text-sm focus:border-[var(--accent)] focus:outline-none"
                  />
                </div>
                <div>
                  <label className="block text-sm font-medium mb-1">
                    Email <span className="text-muted font-normal">(optional)</span>
                  </label>
                  <input
                    type="email"
                    value={email}
                    onChange={(e) => {
                      setEmail(e.target.value);
                      setProfileDirty(true);
                    }}
                    placeholder="you@example.com"
                    disabled={saving}
                    className="w-full px-3 py-2 rounded-md bg-[var(--bg-soft)] border border-[var(--line)] text-white text-sm focus:border-[var(--accent)] focus:outline-none"
                  />
                </div>
                <div>
                  <label className="block text-sm font-medium mb-1">
                    Phone <span className="text-muted font-normal">(optional)</span>
                  </label>
                  <input
                    type="tel"
                    value={phone}
                    onChange={(e) => {
                      setPhone(e.target.value);
                      setProfileDirty(true);
                    }}
                    placeholder="+1 555 123 4567"
                    disabled={saving}
                    className="w-full px-3 py-2 rounded-md bg-[var(--bg-soft)] border border-[var(--line)] text-white text-sm focus:border-[var(--accent)] focus:outline-none"
                  />
                </div>

                {/* Save Button */}
                {profileDirty && (
                  <button
                    onClick={handleProfileSave}
                    disabled={saving}
                    className="mt-2 px-4 py-2 rounded-md bg-[var(--accent)] text-white text-sm font-medium hover:opacity-90 disabled:opacity-50 transition-opacity"
                  >
                    {saving ? "Saving…" : "Save Profile"}
                  </button>
                )}
              </div>
            </div>
          </div>

          {/* Personal LLM Preferences */}
          <div className="surface-panel p-6">
            <h2 className="text-xl font-semibold mb-4">Personal AI Model</h2>
            <p className="text-soft text-sm mb-4">
              Choose your preferred provider/model for personal analyses. Leave empty to use the
              system default.
            </p>

            <div className="grid gap-4 md:grid-cols-2">
              <div>
                <label className="block text-sm font-medium mb-1">Provider</label>
                <select
                  value={llmProviderChoice}
                  onChange={(e) => {
                    setLlmProviderChoice(e.target.value);
                    setLlmModelChoice("");
                    setLlmDirty(true);
                  }}
                  disabled={saving}
                  className="w-full px-3 py-2 rounded-md bg-[var(--bg-soft)] border border-[var(--line)] text-white text-sm focus:border-[var(--accent)] focus:outline-none"
                >
                  <option value="">
                    System default ({currentLlmSettings?.effective_provider || "openai"})
                  </option>
                  {(llmProviders?.providers || []).map((provider) => (
                    <option key={provider.id} value={provider.id}>
                      {provider.name}
                    </option>
                  ))}
                </select>
              </div>

              <div>
                <label className="block text-sm font-medium mb-1">Model</label>
                <select
                  value={llmModelChoice}
                  onChange={(e) => {
                    setLlmModelChoice(e.target.value);
                    setLlmDirty(true);
                  }}
                  disabled={saving}
                  className="w-full px-3 py-2 rounded-md bg-[var(--bg-soft)] border border-[var(--line)] text-white text-sm focus:border-[var(--accent)] focus:outline-none"
                >
                  <option value="">Provider default</option>
                  {availableUserModels.map((model) => (
                    <option key={model} value={model}>
                      {model}
                    </option>
                  ))}
                </select>
              </div>
            </div>

            <div className="mt-4 flex flex-wrap items-center gap-2">
              <button
                onClick={handleSaveLlmSettings}
                disabled={saving || !llmDirty}
                className="px-4 py-2 rounded-md bg-[var(--accent)] text-white text-sm font-medium hover:opacity-90 disabled:opacity-50 transition-opacity"
              >
                {saving ? "Saving..." : "Save AI Preferences"}
              </button>
              <button
                onClick={() => {
                  setLlmProviderChoice(currentLlmSettings?.provider || "");
                  setLlmModelChoice(currentLlmSettings?.model || "");
                  setLlmDirty(false);
                }}
                disabled={saving || !llmDirty}
                className="btn-muted text-sm"
              >
                Reset
              </button>
            </div>

            <p className="text-xs text-muted mt-3">
              Effective provider now: {currentLlmSettings?.effective_provider || "unknown"}
            </p>
          </div>
        </>
      )}

      {/* Trading Tab */}
      {activeTab === "trading" && (
        <>
          {/* Trading Key */}
          <div className="surface-panel p-6" id="trading-key">
            <div className="flex items-center justify-between mb-4">
              <h2 className="text-xl font-semibold">Trading Key</h2>
              <span
                className={
                  hasTradingKey
                    ? "chip chip-success text-xs"
                    : "chip bg-[var(--bg-soft)] text-muted border border-[var(--line)] text-xs"
                }
              >
                {hasTradingKey ? "Connected" : "Not connected"}
              </span>
            </div>

            <p className="text-soft mb-6 text-sm">
              Enable auto-trading by storing your wallet private key. Required for stop-loss orders,
              the inverse position bot, and latency arbitrage. The key is encrypted at rest and is
              never used for login.
            </p>

            {hasTradingKey ? (
              <div className="space-y-4">
                <p className="text-sm text-soft">
                  A trading key is stored for your wallet. Automated strategies can sign orders on
                  your behalf.
                </p>
                <button
                  onClick={handleDisconnectTradingKey}
                  disabled={tradingKeySaving || saving}
                  className="px-4 py-2 rounded-md bg-red-500/10 border border-red-500/30 text-red-400 text-sm font-medium hover:bg-red-500/20 disabled:opacity-50 transition-opacity"
                >
                  {tradingKeySaving ? "Disconnecting…" : "Disconnect Trading Key"}
                </button>
              </div>
            ) : (
              <div className="space-y-4">
                <div>
                  <label className="block text-sm font-medium mb-1">Private Key</label>
                  <input
                    type="password"
                    autoComplete="off"
                    value={tradingPrivateKey}
                    onChange={(e) => setTradingPrivateKey(e.target.value)}
                    placeholder="0x…"
                    disabled={tradingKeySaving || saving}
                    className="w-full px-3 py-2 rounded-md bg-[var(--bg-soft)] border border-[var(--line)] text-white text-sm mono focus:border-[var(--accent)] focus:outline-none"
                  />
                  <p className="text-xs text-muted mt-1">
                    Only enter your key on trusted devices. It is transmitted over HTTPS and
                    encrypted at rest.
                  </p>
                </div>
                <div>
                  <label className="block text-sm font-medium mb-1">
                    CLOB Credentials <span className="text-muted font-normal">(optional JSON)</span>
                  </label>
                  <textarea
                    value={clobCredentialsJson}
                    onChange={(e) => setClobCredentialsJson(e.target.value)}
                    placeholder='{"api_key": "…", "api_secret": "…", "api_passphrase": "…"}'
                    rows={3}
                    disabled={tradingKeySaving || saving}
                    className="w-full px-3 py-2 rounded-md bg-[var(--bg-soft)] border border-[var(--line)] text-white text-sm mono focus:border-[var(--accent)] focus:outline-none"
                  />
                  <p className="text-xs text-muted mt-1">
                    Leave empty to let the platform derive CLOB API credentials from your key.
                  </p>
                </div>
                {tradingKeyError && (
                  <div className="p-3 alert-error rounded text-sm">{tradingKeyError}</div>
                )}
                <button
                  onClick={handleConnectTradingKey}
                  disabled={tradingKeySaving || saving || !tradingPrivateKey.trim()}
                  className="px-4 py-2 rounded-md bg-[var(--accent)] text-white text-sm font-medium hover:opacity-90 disabled:opacity-50 transition-opacity"
                >
                  {tradingKeySaving ? "Connecting…" : "Connect Trading Key"}
                </button>
              </div>
            )}
          </div>

          {/* Copy Trading Settings */}
          <div className="surface-panel p-6">
            <div className="flex items-center justify-between mb-4">
              <h2 className="text-xl font-semibold">Copy Trading</h2>
              <label className="flex items-center gap-2 cursor-pointer">
                <span className="text-sm text-soft">
                  {settings?.copy_trading_enabled ? "Enabled" : "Disabled"}
                </span>
                <input
                  type="checkbox"
                  checked={settings?.copy_trading_enabled ?? false}
                  onChange={(e) =>
                    handleCopyTradingUpdate("copy_trading_enabled", e.target.checked)
                  }
                  disabled={saving}
                  className="w-5 h-5 accent-[var(--accent)]"
                />
              </label>
            </div>

            <p className="text-soft mb-6 text-sm">
              Automatically mirror trades from followed traders. Configure risk controls below.
            </p>

            {/* AI Gate */}
            <label className="flex items-center gap-3 mb-6 cursor-pointer">
              <input
                type="checkbox"
                checked={settings?.require_ai_approval ?? true}
                onChange={(e) => handleCopyTradingUpdate("require_ai_approval", e.target.checked)}
                disabled={saving}
                className="w-4 h-4 accent-[var(--accent)]"
              />
              <div>
                <p className="text-sm font-medium">Require AI Approval</p>
                <p className="text-xs text-muted">
                  Run LLM analysis on each trade before copying. Trades rated "avoid" will be
                  skipped.
                </p>
              </div>
            </label>

            <label className="flex items-center gap-3 mb-6 cursor-pointer">
              <input
                type="checkbox"
                checked={settings?.follow_email_notifications_enabled ?? false}
                onChange={(e) =>
                  handleCopyTradingUpdate("follow_email_notifications_enabled", e.target.checked)
                }
                disabled={saving || !email}
                className="w-4 h-4 accent-[var(--accent)]"
              />
              <div>
                <p className="text-sm font-medium">Email Notifications for Followed Traders</p>
                <p className="text-xs text-muted">
                  {email
                    ? "Enables immediate email alerts when followed traders open/close positions."
                    : "Add an email in Profile to enable follow-notification emails."}
                </p>
              </div>
            </label>

            {/* Risk Mode */}
            <div className="mb-6">
              <h3 className="text-sm font-semibold mb-3">Risk Control Mode</h3>
              <div className="space-y-3">
                {RISK_MODES.map((mode) => (
                  <label
                    key={mode.value}
                    className={`block cursor-pointer p-3 rounded-lg border-2 transition-all ${
                      settings?.risk_mode === mode.value
                        ? "border-[var(--accent)] bg-[var(--accent-soft)]"
                        : "border-[var(--line)] hover:border-[var(--line-strong)]"
                    }`}
                  >
                    <div className="flex items-start">
                      <input
                        type="radio"
                        name="risk_mode"
                        value={mode.value}
                        checked={settings?.risk_mode === mode.value}
                        onChange={() => handleCopyTradingUpdate("risk_mode", mode.value)}
                        disabled={saving}
                        className="mt-0.5 mr-3"
                      />
                      <div>
                        <p className="font-medium text-sm">{mode.label}</p>
                        <p className="text-xs text-muted">{mode.description}</p>
                      </div>
                    </div>
                  </label>
                ))}
              </div>
            </div>

            {/* Mode-specific parameters */}
            <div className="space-y-4">
              {(settings?.risk_mode === "max_position_daily_loss" || !settings?.risk_mode) && (
                <>
                  <NumberInput
                    label="Max Position Size ($)"
                    value={settings?.max_position_size ?? 100}
                    onChange={(v) => handleCopyTradingUpdate("max_position_size", v)}
                    disabled={saving}
                  />
                  <NumberInput
                    label="Daily Loss Limit ($)"
                    value={settings?.daily_loss_limit ?? 500}
                    onChange={(v) => handleCopyTradingUpdate("daily_loss_limit", v)}
                    disabled={saving}
                  />
                </>
              )}
              {settings?.risk_mode === "percentage_mirror" && (
                <NumberInput
                  label="Mirror Percentage (%)"
                  value={settings?.mirror_percentage ?? 10}
                  onChange={(v) => handleCopyTradingUpdate("mirror_percentage", v)}
                  disabled={saving}
                  max={100}
                />
              )}
              {settings?.risk_mode === "fixed_amount" && (
                <NumberInput
                  label="Fixed Trade Amount ($)"
                  value={settings?.fixed_trade_amount ?? 50}
                  onChange={(v) => handleCopyTradingUpdate("fixed_trade_amount", v)}
                  disabled={saving}
                />
              )}
            </div>
          </div>

          {/* Inverse Position Bot Settings */}
          <div className="surface-panel p-6">
            <div className="flex items-center justify-between mb-4">
              <h2 className="text-xl font-semibold">Inverse Position Bot</h2>
              <label className="flex items-center gap-2 cursor-pointer">
                <span className="text-sm text-soft">
                  {settings?.inverse_bot_enabled ? "Enabled" : "Disabled"}
                </span>
                <input
                  type="checkbox"
                  checked={settings?.inverse_bot_enabled ?? false}
                  onChange={(e) => void handleInverseBotToggle(e.target.checked)}
                  disabled={saving}
                  className="w-5 h-5 accent-[var(--accent)]"
                />
              </label>
            </div>

            <p className="text-soft mb-6 text-sm">
              Auto-reverses tracked positions when market distribution and 24h web/X evidence
              strongly favor an alternative outcome.
            </p>

            {hasTradingKey === false && (
              <div className="mb-6 p-3 rounded-lg border border-amber-500/30 bg-amber-500/10 text-sm text-amber-400">
                The inverse position bot requires a trading key.{" "}
                <a href="#trading-key" className="underline font-medium">
                  Connect your trading key above
                </a>{" "}
                to enable it.
              </div>
            )}

            <div className="mb-6">
              <h3 className="text-sm font-semibold mb-3">Default Buy Sizing</h3>
              <div className="space-y-3">
                {INVERSE_SIZE_MODES.map((mode) => (
                  <label
                    key={mode.value}
                    className={`block cursor-pointer p-3 rounded-lg border-2 transition-all ${
                      settings?.inverse_bot_default_size_mode === mode.value
                        ? "border-[var(--accent)] bg-[var(--accent-soft)]"
                        : "border-[var(--line)] hover:border-[var(--line-strong)]"
                    }`}
                  >
                    <div className="flex items-start">
                      <input
                        type="radio"
                        name="inverse_size_mode"
                        value={mode.value}
                        checked={settings?.inverse_bot_default_size_mode === mode.value}
                        onChange={() =>
                          handleCopyTradingUpdate("inverse_bot_default_size_mode", mode.value)
                        }
                        disabled={saving}
                        className="mt-0.5 mr-3"
                      />
                      <div>
                        <p className="font-medium text-sm">{mode.label}</p>
                        <p className="text-xs text-muted">{mode.description}</p>
                      </div>
                    </div>
                  </label>
                ))}
              </div>
            </div>

            <div className="space-y-4">
              {settings?.inverse_bot_default_size_mode === "fixed_amount" && (
                <NumberInput
                  label="Fixed Buy Amount ($)"
                  value={settings?.inverse_bot_fixed_amount ?? 50}
                  onChange={(v) => handleCopyTradingUpdate("inverse_bot_fixed_amount", v)}
                  disabled={saving}
                />
              )}
              <NumberInput
                label="Confidence Threshold (0-100)"
                value={settings?.inverse_bot_confidence_threshold ?? 75}
                onChange={(v) =>
                  handleCopyTradingUpdate(
                    "inverse_bot_confidence_threshold",
                    Math.min(100, Math.max(1, Math.round(v))),
                  )
                }
                disabled={saving}
                max={100}
              />
              <NumberInput
                label="Cooldown Minutes"
                value={settings?.inverse_bot_cooldown_minutes ?? 30}
                onChange={(v) =>
                  handleCopyTradingUpdate(
                    "inverse_bot_cooldown_minutes",
                    Math.max(1, Math.round(v)),
                  )
                }
                disabled={saving}
              />
              <NumberInput
                label="Max Reversals Per Day"
                value={settings?.inverse_bot_max_reversals_per_day ?? 3}
                onChange={(v) =>
                  handleCopyTradingUpdate(
                    "inverse_bot_max_reversals_per_day",
                    Math.max(1, Math.round(v)),
                  )
                }
                disabled={saving}
              />
            </div>
          </div>

          {/* Last Updated */}
          {settings?.updated_at && (
            <p className="text-muted text-sm">
              Last updated: {new Date(settings.updated_at).toLocaleString()}
            </p>
          )}
        </>
      )}

      {/* Notifications Tab */}
      {activeTab === "notifications" && (
        <>
          {/* Channel management */}
          <div className="surface-panel p-6">
            <h2 className="text-xl font-semibold mb-4">Notification Channels</h2>
            <p className="text-soft mb-6 text-sm">
              Configure where alerts are delivered: Telegram chats, Discord webhooks, or any HTTPS
              webhook. Channel credentials are encrypted at rest.
            </p>

            {notifLoading ? (
              <div className="flex items-center justify-center py-8">
                <div className="animate-spin rounded-full h-8 w-8 border-t-2 border-b-2 border-[var(--accent)]"></div>
              </div>
            ) : (
              <>
                {notifChannels.length === 0 ? (
                  <p className="text-muted text-sm mb-4">No channels configured yet.</p>
                ) : (
                  <ul className="space-y-3 mb-6">
                    {notifChannels.map((channel) => (
                      <li
                        key={channel.id}
                        className="flex items-center justify-between gap-3 p-3 rounded-lg border border-[var(--line)] bg-[var(--bg-soft)]"
                      >
                        <div className="min-w-0">
                          <span className="font-medium text-sm">
                            {channel.name || channel.channel_type}
                          </span>
                          <span className="chip bg-[var(--bg)] text-muted border border-[var(--line)] ml-2 text-xs">
                            {channel.channel_type}
                          </span>
                          {!channel.is_active && (
                            <span className="chip chip-danger ml-2 text-xs">Inactive</span>
                          )}
                          {channel.created_at && (
                            <p className="text-xs text-muted mt-1">
                              Added {new Date(channel.created_at).toLocaleString()}
                            </p>
                          )}
                        </div>
                        <div className="flex gap-3 flex-shrink-0">
                          <button
                            type="button"
                            onClick={() => handleTestChannel(channel.id)}
                            disabled={notifSaving || testingChannelId !== null}
                            className="text-xs text-[var(--accent)] hover:underline disabled:opacity-50"
                          >
                            {testingChannelId === channel.id ? "Sending…" : "Test"}
                          </button>
                          <button
                            type="button"
                            onClick={() => handleDeleteChannel(channel.id)}
                            disabled={notifSaving}
                            className="text-xs text-red-400 hover:underline disabled:opacity-50"
                          >
                            Delete
                          </button>
                        </div>
                      </li>
                    ))}
                  </ul>
                )}

                {/* Add channel form */}
                <div className="border-t border-[var(--line)] pt-4">
                  <h3 className="text-sm font-semibold mb-3">Add Channel</h3>
                  <div className="grid gap-4 md:grid-cols-2">
                    <div>
                      <label className="block text-sm font-medium mb-1">Type</label>
                      <select
                        value={channelType}
                        onChange={(e) => setChannelType(e.target.value as NotificationChannelType)}
                        disabled={notifSaving}
                        className="w-full px-3 py-2 rounded-md bg-[var(--bg-soft)] border border-[var(--line)] text-white text-sm focus:border-[var(--accent)] focus:outline-none"
                      >
                        <option value="telegram">Telegram</option>
                        <option value="discord">Discord</option>
                        <option value="webhook">Webhook (HTTPS)</option>
                      </select>
                    </div>
                    <div>
                      <label className="block text-sm font-medium mb-1">Name</label>
                      <input
                        type="text"
                        value={channelName}
                        onChange={(e) => setChannelName(e.target.value)}
                        placeholder="e.g. Phone alerts"
                        disabled={notifSaving}
                        className="w-full px-3 py-2 rounded-md bg-[var(--bg-soft)] border border-[var(--line)] text-white text-sm focus:border-[var(--accent)] focus:outline-none"
                      />
                    </div>
                  </div>

                  {channelType === "telegram" && (
                    <div className="mt-4">
                      <label className="block text-sm font-medium mb-1">Telegram Chat ID</label>
                      <input
                        type="text"
                        value={chatId}
                        onChange={(e) => setChatId(e.target.value)}
                        placeholder="123456789"
                        disabled={notifSaving}
                        className="w-full px-3 py-2 rounded-md bg-[var(--bg-soft)] border border-[var(--line)] text-white text-sm focus:border-[var(--accent)] focus:outline-none"
                      />
                      <p className="text-xs text-muted mt-1">
                        Your Telegram chat ID (see @userinfobot). Messages are sent by the platform
                        bot.
                      </p>
                    </div>
                  )}
                  {channelType === "discord" && (
                    <div className="mt-4">
                      <label className="block text-sm font-medium mb-1">Discord Webhook URL</label>
                      <input
                        type="text"
                        value={discordUrl}
                        onChange={(e) => setDiscordUrl(e.target.value)}
                        placeholder="https://discord.com/api/webhooks/…"
                        disabled={notifSaving}
                        className="w-full px-3 py-2 rounded-md bg-[var(--bg-soft)] border border-[var(--line)] text-white text-sm focus:border-[var(--accent)] focus:outline-none"
                      />
                    </div>
                  )}
                  {channelType === "webhook" && (
                    <>
                      <div className="mt-4">
                        <label className="block text-sm font-medium mb-1">
                          Webhook URL (https only)
                        </label>
                        <input
                          type="text"
                          value={webhookUrl}
                          onChange={(e) => setWebhookUrl(e.target.value)}
                          placeholder="https://example.com/alerts"
                          disabled={notifSaving}
                          className="w-full px-3 py-2 rounded-md bg-[var(--bg-soft)] border border-[var(--line)] text-white text-sm focus:border-[var(--accent)] focus:outline-none"
                        />
                      </div>
                      <div className="mt-4">
                        <label className="block text-sm font-medium mb-1">
                          Signing Secret <span className="text-muted font-normal">(optional)</span>
                        </label>
                        <input
                          type="text"
                          value={webhookSecret}
                          onChange={(e) => setWebhookSecret(e.target.value)}
                          placeholder="HMAC-SHA256 secret"
                          disabled={notifSaving}
                          className="w-full px-3 py-2 rounded-md bg-[var(--bg-soft)] border border-[var(--line)] text-white text-sm focus:border-[var(--accent)] focus:outline-none"
                        />
                        <p className="text-xs text-muted mt-1">
                          When set, every POST includes an <code>X-Signature</code> header
                          containing an HMAC-SHA256 signature of the JSON body.
                        </p>
                      </div>
                    </>
                  )}

                  <button
                    type="button"
                    onClick={handleCreateChannel}
                    disabled={notifSaving}
                    className="mt-4 px-4 py-2 rounded-md bg-[var(--accent)] text-white text-sm font-medium hover:opacity-90 disabled:opacity-50 transition-opacity"
                  >
                    {notifSaving ? "Saving…" : "Create Channel"}
                  </button>
                </div>
              </>
            )}
          </div>

          {/* Alert routing preferences */}
          <div className="surface-panel p-6">
            <div className="flex items-center justify-between mb-4">
              <h2 className="text-xl font-semibold">Alert Routing</h2>
              <button
                type="button"
                onClick={handleSavePreferences}
                disabled={notifSaving || !prefsDirty}
                className="px-4 py-2 rounded-md bg-[var(--accent)] text-white text-sm font-medium hover:opacity-90 disabled:opacity-50 transition-opacity"
              >
                {notifSaving ? "Saving…" : "Save Preferences"}
              </button>
            </div>
            <p className="text-soft mb-6 text-sm">
              Choose which channels fire for each event type. With no channels selected, every
              active channel is used.
            </p>
            {notifChannels.length === 0 ? (
              <p className="text-muted text-sm">
                Create a channel above to configure per-event routing.
              </p>
            ) : (
              <div className="overflow-x-auto">
                <table className="w-full text-sm">
                  <thead>
                    <tr className="border-b border-[var(--line)]">
                      <th className="text-left py-2 px-2 font-medium text-muted">Event</th>
                      <th className="text-left py-2 px-2 font-medium text-muted">Enabled</th>
                      {notifChannels.map((c) => (
                        <th key={c.id} className="text-left py-2 px-2 font-medium text-muted">
                          {c.name || c.channel_type}
                        </th>
                      ))}
                    </tr>
                  </thead>
                  <tbody>
                    {notifPreferences.map((pref) => (
                      <tr key={pref.event_type} className="border-b border-[var(--line)]">
                        <td className="py-2 px-2">{pref.event_type.replace(/_/g, " ")}</td>
                        <td className="py-2 px-2">
                          <input
                            type="checkbox"
                            checked={pref.enabled}
                            onChange={() => handleTogglePreferenceEnabled(pref.event_type)}
                            disabled={notifSaving}
                            className="w-4 h-4 accent-[var(--accent)]"
                          />
                        </td>
                        {notifChannels.map((c) => (
                          <td key={c.id} className="py-2 px-2">
                            <input
                              type="checkbox"
                              checked={pref.channel_ids.includes(c.id)}
                              onChange={() => handleTogglePreferenceChannel(pref.event_type, c.id)}
                              disabled={notifSaving || !pref.enabled}
                              className="w-4 h-4 accent-[var(--accent)]"
                            />
                          </td>
                        ))}
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </div>
        </>
      )}

      {/* Admin Tab */}
      {activeTab === "admin" && isAdmin && (
        <AdminSettingsTab
          providers={adminProviders}
          users={adminUsers}
          loading={adminLoading}
          saving={saving}
          onUpdateDefaults={handleUpdateDefaults}
          onUpdateUserLLM={handleUpdateUserLLM}
          onRefresh={loadAdminData}
        />
      )}
    </div>
  );
}

function StatusBadge({ healthy }: { healthy?: boolean }) {
  if (healthy === undefined) {
    return (
      <span className="chip bg-[var(--bg-soft)] text-muted border border-[var(--line)]">
        Unknown
      </span>
    );
  }
  return healthy ? (
    <span className="chip chip-success">Online</span>
  ) : (
    <span className="chip chip-danger">Offline</span>
  );
}

function NumberInput({
  label,
  value,
  onChange,
  disabled,
  max,
}: {
  label: string;
  value: number;
  onChange: (v: number) => void;
  disabled: boolean;
  max?: number;
}) {
  const [local, setLocal] = useState(String(value));

  useEffect(() => {
    setLocal(String(value));
  }, [value]);

  const handleBlur = () => {
    let n = parseFloat(local);
    if (isNaN(n) || n <= 0) n = value;
    if (max && n > max) n = max;
    setLocal(String(n));
    if (n !== value) onChange(n);
  };

  return (
    <div>
      <label className="block text-sm font-medium mb-1">{label}</label>
      <input
        type="number"
        value={local}
        onChange={(e) => setLocal(e.target.value)}
        onBlur={handleBlur}
        disabled={disabled}
        className="w-full px-3 py-2 rounded-md bg-[var(--bg-soft)] border border-[var(--line)] text-white text-sm focus:border-[var(--accent)] focus:outline-none"
        min={0}
        max={max}
      />
    </div>
  );
}

// ── Admin Settings Tab Component ──

interface AdminSettingsTabProps {
  providers: AdminProvidersResponse | null;
  users: AdminUsersLLMResponse | null;
  loading: boolean;
  saving: boolean;
  onUpdateDefaults: (provider?: string, model?: string) => void;
  onUpdateUserLLM: (userId: number, provider: string | null, model: string | null) => void;
  onRefresh: () => void;
}

function AdminSettingsTab({
  providers,
  users,
  loading,
  saving,
  onUpdateDefaults,
  onUpdateUserLLM,
  onRefresh,
}: AdminSettingsTabProps) {
  const [selectedProvider, setSelectedProvider] = useState("");
  const [selectedModel, setSelectedModel] = useState("");
  const [editingUser, setEditingUser] = useState<number | null>(null);
  const [userProvider, setUserProvider] = useState("");
  const [userModel, setUserModel] = useState("");

  useEffect(() => {
    if (providers) {
      setSelectedProvider(providers.default_provider);
      setSelectedModel(providers.default_model);
    }
  }, [providers]);

  if (loading) {
    return (
      <div className="flex items-center justify-center py-12">
        <div className="animate-spin rounded-full h-8 w-8 border-t-2 border-b-2 border-[var(--accent)]"></div>
      </div>
    );
  }

  const currentProviderModels =
    providers?.providers.find((p) => p.id === selectedProvider)?.models || [];

  return (
    <>
      {/* Provider Status */}
      <div className="surface-panel p-6">
        <div className="flex items-center justify-between mb-4">
          <h2 className="text-xl font-semibold">LLM Providers</h2>
          <button
            onClick={onRefresh}
            disabled={loading}
            className="text-sm text-[var(--accent)] hover:underline disabled:opacity-50"
          >
            Refresh
          </button>
        </div>
        <p className="text-soft mb-6 text-sm">
          Status of all available LLM providers. Configure API keys in environment variables.
        </p>
        <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
          {providers?.providers.map((provider) => (
            <div
              key={provider.id}
              className="p-4 rounded-lg border border-[var(--line)] bg-[var(--bg-soft)]"
            >
              <div className="flex items-center justify-between mb-2">
                <span className="font-semibold">{provider.name}</span>
                <div className="flex gap-2">
                  {provider.is_healthy ? (
                    <span className="chip chip-success text-xs">Online</span>
                  ) : (
                    <span className="chip chip-danger text-xs">Offline</span>
                  )}
                  {provider.requires_api_key &&
                    (provider.is_configured ? (
                      <span className="chip bg-green-900/30 text-green-400 border border-green-800 text-xs">
                        Key Set
                      </span>
                    ) : (
                      <span className="chip bg-yellow-900/30 text-yellow-400 border border-yellow-800 text-xs">
                        No Key
                      </span>
                    ))}
                </div>
              </div>
              <p className="text-xs text-muted">{provider.description}</p>
              <p className="text-xs text-muted mt-1">
                Backend: <span className="text-soft">{provider.backend}</span>
              </p>
            </div>
          ))}
        </div>
      </div>

      {/* System Defaults */}
      <div className="surface-panel p-6">
        <h2 className="text-xl font-semibold mb-4">System Defaults</h2>
        <p className="text-soft mb-6 text-sm">
          Default provider and model used when users haven't set a preference.
        </p>
        <div className="grid grid-cols-1 md:grid-cols-2 gap-4 mb-4">
          <div>
            <label className="block text-sm font-medium mb-1">Default Provider</label>
            <select
              value={selectedProvider}
              onChange={(e) => {
                setSelectedProvider(e.target.value);
                setSelectedModel("");
              }}
              disabled={saving}
              className="w-full px-3 py-2 rounded-md bg-[var(--bg-soft)] border border-[var(--line)] text-white text-sm focus:border-[var(--accent)] focus:outline-none"
            >
              {providers?.providers.map((p) => (
                <option key={p.id} value={p.id}>
                  {p.name}
                </option>
              ))}
            </select>
          </div>
          <div>
            <label className="block text-sm font-medium mb-1">Default Model</label>
            <select
              value={selectedModel}
              onChange={(e) => setSelectedModel(e.target.value)}
              disabled={saving}
              className="w-full px-3 py-2 rounded-md bg-[var(--bg-soft)] border border-[var(--line)] text-white text-sm focus:border-[var(--accent)] focus:outline-none"
            >
              <option value="">Provider Default</option>
              {currentProviderModels.map((m) => (
                <option key={m} value={m}>
                  {m}
                </option>
              ))}
            </select>
          </div>
        </div>
        <button
          onClick={() => onUpdateDefaults(selectedProvider, selectedModel)}
          disabled={saving}
          className="px-4 py-2 rounded-md bg-[var(--accent)] text-white text-sm font-medium hover:opacity-90 disabled:opacity-50 transition-opacity"
        >
          {saving ? "Saving…" : "Save Defaults"}
        </button>
      </div>

      {/* User LLM Settings */}
      <div className="surface-panel p-6">
        <h2 className="text-xl font-semibold mb-4">User LLM Settings</h2>
        <p className="text-soft mb-6 text-sm">
          View and override individual user's LLM provider preferences.
        </p>
        <div className="overflow-x-auto">
          <table className="w-full text-sm">
            <thead>
              <tr className="border-b border-[var(--line)]">
                <th className="text-left py-2 px-2 font-medium text-muted">User</th>
                <th className="text-left py-2 px-2 font-medium text-muted">Provider</th>
                <th className="text-left py-2 px-2 font-medium text-muted">Model</th>
                <th className="text-left py-2 px-2 font-medium text-muted">Actions</th>
              </tr>
            </thead>
            <tbody>
              {users?.users.map((user) => (
                <tr key={user.user_id} className="border-b border-[var(--line)]">
                  <td className="py-2 px-2">
                    <div>
                      <span className="font-medium">
                        {user.display_name || user.wallet_address.slice(0, 10) + "..."}
                      </span>
                      <p className="text-xs text-muted truncate max-w-[150px]">
                        {user.wallet_address}
                      </p>
                    </div>
                  </td>
                  <td className="py-2 px-2">
                    {editingUser === user.user_id ? (
                      <select
                        value={userProvider}
                        onChange={(e) => setUserProvider(e.target.value)}
                        className="px-2 py-1 rounded bg-[var(--bg-soft)] border border-[var(--line)] text-sm"
                      >
                        <option value="">System Default</option>
                        {providers?.providers.map((p) => (
                          <option key={p.id} value={p.id}>
                            {p.name}
                          </option>
                        ))}
                      </select>
                    ) : (
                      <span className="chip bg-[var(--bg-soft)] text-soft border border-[var(--line)]">
                        {user.preferred_llm_provider || "Default"}
                      </span>
                    )}
                  </td>
                  <td className="py-2 px-2">
                    {editingUser === user.user_id ? (
                      <input
                        type="text"
                        value={userModel}
                        onChange={(e) => setUserModel(e.target.value)}
                        placeholder="Model name"
                        className="px-2 py-1 rounded bg-[var(--bg-soft)] border border-[var(--line)] text-sm w-32"
                      />
                    ) : (
                      <span className="text-muted">{user.preferred_llm_model || "—"}</span>
                    )}
                  </td>
                  <td className="py-2 px-2">
                    {editingUser === user.user_id ? (
                      <div className="flex gap-2">
                        <button
                          onClick={() => {
                            onUpdateUserLLM(user.user_id, userProvider || null, userModel || null);
                            setEditingUser(null);
                          }}
                          disabled={saving}
                          className="text-xs text-green-400 hover:underline disabled:opacity-50"
                        >
                          Save
                        </button>
                        <button
                          onClick={() => setEditingUser(null)}
                          className="text-xs text-muted hover:underline"
                        >
                          Cancel
                        </button>
                      </div>
                    ) : (
                      <button
                        onClick={() => {
                          setEditingUser(user.user_id);
                          setUserProvider(user.preferred_llm_provider || "");
                          setUserModel(user.preferred_llm_model || "");
                        }}
                        className="text-xs text-[var(--accent)] hover:underline"
                      >
                        Edit
                      </button>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        {users && users.total > 50 && (
          <p className="text-xs text-muted mt-4">
            Showing {users.users.length} of {users.total} users
          </p>
        )}
      </div>
    </>
  );
}
