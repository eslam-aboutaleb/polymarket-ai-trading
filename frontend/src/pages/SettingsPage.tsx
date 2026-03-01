import React, { useState, useEffect, useRef } from "react";
import { useAuthStore } from "../store/authStore";
import {
  settingsService,
  UserSettings,
  BackendsStatus,
  RiskMode,
  InverseBotSizeMode,
  UserProfile,
} from "../services/settingsService";
import { getApiErrorMessage } from "../utils/apiError";

type AIBackend = "llm_chain" | "cli_agent";

const RISK_MODES: { value: RiskMode; label: string; description: string }[] = [
  {
    value: "max_position_daily_loss",
    label: "Max Position + Daily Loss",
    description:
      "Cap each trade size and stop copying if daily loss limit is hit",
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
  const [settings, setSettings] = useState<UserSettings | null>(null);
  const [backendsStatus, setBackendsStatus] = useState<BackendsStatus | null>(
    null,
  );
  const [profile, setProfile] = useState<UserProfile | null>(null);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [successMessage, setSuccessMessage] = useState<string | null>(null);

  // Profile form local state
  const [displayName, setDisplayName] = useState("");
  const [email, setEmail] = useState("");
  const [phone, setPhone] = useState("");
  const [twoFaEnabled, setTwoFaEnabled] = useState(false);
  const [profileDirty, setProfileDirty] = useState(false);
  const fileInputRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    loadSettings();
  }, []);

  // Sync local profile form when profile loads
  useEffect(() => {
    if (profile) {
      setDisplayName(profile.display_name || "");
      setEmail(profile.email || "");
      setPhone(profile.phone || "");
      setTwoFaEnabled(profile.two_fa_enabled);
      setProfileDirty(false);
    }
  }, [profile]);

  const loadSettings = async () => {
    try {
      setLoading(true);
      setError(null);
      const [userSettings, status, userProfile] = await Promise.all([
        settingsService.getUserSettings(),
        settingsService.getBackendsStatus(),
        settingsService.getUserProfile(),
      ]);
      setSettings(userSettings);
      setBackendsStatus(status);
      setProfile(userProfile);
    } catch (err: unknown) {
      setError(getApiErrorMessage(err, "Failed to load settings"));
    } finally {
      setLoading(false);
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
        two_fa_enabled: twoFaEnabled,
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

  const handleCopyTradingUpdate = async (
    field: string,
    value: boolean | string | number,
  ) => {
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

  if (loading) {
    return (
      <div
        className="flex items-center justify-center"
        style={{ minHeight: "calc(100vh - 8rem)" }}
      >
        <div className="animate-spin rounded-full h-12 w-12 border-t-2 border-b-2 border-[var(--accent)]"></div>
      </div>
    );
  }

  return (
    <div className="max-w-2xl mx-auto space-y-6">
      <h1 className="text-3xl font-bold">Settings</h1>

      {error && <div className="alert-error px-4 py-3 rounded">{error}</div>}
      {successMessage && (
        <div className="alert-success px-4 py-3 rounded">{successMessage}</div>
      )}

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
                  {(profile?.display_name ||
                    walletAddress ||
                    "?")[0].toUpperCase()}
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
              <label className="block text-sm font-medium mb-1">
                Display Name
              </label>
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
                Email{" "}
                <span className="text-muted font-normal">
                  (optional — for 2FA)
                </span>
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
                Phone{" "}
                <span className="text-muted font-normal">
                  (optional — for 2FA)
                </span>
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

            {/* 2FA Toggle */}
            <div className="flex items-center justify-between pt-2">
              <div>
                <p className="text-sm font-medium">Two-Factor Authentication</p>
                <p className="text-xs text-muted">
                  {email || phone
                    ? "Secure your account with a verification code"
                    : "Add an email or phone first to enable 2FA"}
                </p>
              </div>
              <label className="relative inline-flex items-center cursor-pointer">
                <input
                  type="checkbox"
                  checked={twoFaEnabled}
                  onChange={(e) => {
                    setTwoFaEnabled(e.target.checked);
                    setProfileDirty(true);
                  }}
                  disabled={saving || (!email && !phone)}
                  className="sr-only peer"
                />
                <div className="w-9 h-5 rounded-full bg-[var(--bg-soft)] border border-[var(--line)] peer-checked:bg-[var(--accent)] peer-disabled:opacity-40 transition-colors after:content-[''] after:absolute after:top-[2px] after:left-[2px] after:bg-white after:rounded-full after:h-4 after:w-4 after:transition-all peer-checked:after:translate-x-full"></div>
              </label>
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

      {/* AI Backend Selection — admin only */}
      {isAdmin && (
        <div className="surface-panel p-6">
          <h2 className="text-xl font-semibold mb-4">AI Analysis Backend</h2>
          <p className="text-soft mb-6">
            Choose which AI backend to use for market analysis.
          </p>
          <div className="space-y-4">
            {/* LLM Chain */}
            <label
              className={`block cursor-pointer p-4 rounded-lg border-2 transition-all ${
                settings?.ai_backend === "llm_chain"
                  ? "border-[var(--accent)] bg-[var(--accent-soft)]"
                  : "border-[var(--line)] hover:border-[var(--line-strong)]"
              }`}
            >
              <div className="flex items-start">
                <input
                  type="radio"
                  name="ai_backend"
                  value="llm_chain"
                  checked={settings?.ai_backend === "llm_chain"}
                  onChange={() => handleBackendChange("llm_chain")}
                  disabled={saving}
                  className="mt-1 mr-4"
                />
                <div className="flex-1">
                  <div className="flex items-center justify-between">
                    <span className="font-semibold text-lg">
                      {backendsStatus?.llm_chain.name || "LLM Chain (OpenAI)"}
                    </span>
                    <StatusBadge healthy={backendsStatus?.llm_chain.healthy} />
                  </div>
                  <p className="text-soft mt-1">
                    {backendsStatus?.llm_chain.description ||
                      "LangChain with OpenAI GPT-4 and web search"}
                  </p>
                </div>
              </div>
            </label>
            {/* CLI Agent */}
            <label
              className={`block cursor-pointer p-4 rounded-lg border-2 transition-all ${
                settings?.ai_backend === "cli_agent"
                  ? "border-[var(--accent)] bg-[var(--accent-soft)]"
                  : "border-[var(--line)] hover:border-[var(--line-strong)]"
              }`}
            >
              <div className="flex items-start">
                <input
                  type="radio"
                  name="ai_backend"
                  value="cli_agent"
                  checked={settings?.ai_backend === "cli_agent"}
                  onChange={() => handleBackendChange("cli_agent")}
                  disabled={saving}
                  className="mt-1 mr-4"
                />
                <div className="flex-1">
                  <div className="flex items-center justify-between">
                    <span className="font-semibold text-lg">
                      {backendsStatus?.cli_agent.name ||
                        "CLI Agent (GitHub Copilot)"}
                    </span>
                    <StatusBadge healthy={backendsStatus?.cli_agent.healthy} />
                  </div>
                  <p className="text-soft mt-1">
                    {backendsStatus?.cli_agent.description ||
                      "GitHub Copilot CLI with MCP tools"}
                  </p>
                </div>
              </div>
            </label>
          </div>
        </div>
      )}

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
                handleCopyTradingUpdate(
                  "copy_trading_enabled",
                  e.target.checked,
                )
              }
              disabled={saving}
              className="w-5 h-5 accent-[var(--accent)]"
            />
          </label>
        </div>

        <p className="text-soft mb-6 text-sm">
          Automatically mirror trades from followed traders. Configure risk
          controls below.
        </p>

        {/* AI Gate */}
        <label className="flex items-center gap-3 mb-6 cursor-pointer">
          <input
            type="checkbox"
            checked={settings?.require_ai_approval ?? true}
            onChange={(e) =>
              handleCopyTradingUpdate("require_ai_approval", e.target.checked)
            }
            disabled={saving}
            className="w-4 h-4 accent-[var(--accent)]"
          />
          <div>
            <p className="text-sm font-medium">Require AI Approval</p>
            <p className="text-xs text-muted">
              Run LLM analysis on each trade before copying. Trades rated
              "avoid" will be skipped.
            </p>
          </div>
        </label>

        <label className="flex items-center gap-3 mb-6 cursor-pointer">
          <input
            type="checkbox"
            checked={settings?.follow_email_notifications_enabled ?? false}
            onChange={(e) =>
              handleCopyTradingUpdate(
                "follow_email_notifications_enabled",
                e.target.checked,
              )
            }
            disabled={saving || !email}
            className="w-4 h-4 accent-[var(--accent)]"
          />
          <div>
            <p className="text-sm font-medium">
              Email Notifications for Followed Traders
            </p>
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
                    onChange={() =>
                      handleCopyTradingUpdate("risk_mode", mode.value)
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

        {/* Mode-specific parameters */}
        <div className="space-y-4">
          {(settings?.risk_mode === "max_position_daily_loss" ||
            !settings?.risk_mode) && (
            <>
              <NumberInput
                label="Max Position Size ($)"
                value={settings?.max_position_size ?? 100}
                onChange={(v) =>
                  handleCopyTradingUpdate("max_position_size", v)
                }
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
              onChange={(e) =>
                handleCopyTradingUpdate("inverse_bot_enabled", e.target.checked)
              }
              disabled={saving}
              className="w-5 h-5 accent-[var(--accent)]"
            />
          </label>
        </div>

        <p className="text-soft mb-6 text-sm">
          Auto-reverses tracked positions when market distribution and 24h web/X
          evidence strongly favor an alternative outcome.
        </p>

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
              handleCopyTradingUpdate("inverse_bot_confidence_threshold", Math.min(100, Math.max(1, Math.round(v))))
            }
            disabled={saving}
            max={100}
          />
          <NumberInput
            label="Cooldown Minutes"
            value={settings?.inverse_bot_cooldown_minutes ?? 30}
            onChange={(v) =>
              handleCopyTradingUpdate("inverse_bot_cooldown_minutes", Math.max(1, Math.round(v)))
            }
            disabled={saving}
          />
          <NumberInput
            label="Max Reversals Per Day"
            value={settings?.inverse_bot_max_reversals_per_day ?? 3}
            onChange={(v) =>
              handleCopyTradingUpdate("inverse_bot_max_reversals_per_day", Math.max(1, Math.round(v)))
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
