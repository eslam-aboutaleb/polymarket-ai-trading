/**
 * Authenticated app shell: navigation, routed page outlet, and the floating tutorial launcher.
 *
 * Renders `Navigation` with the current wallet and a logout handler that always clears the auth
 * store, hosts the router `Outlet`, and mounts `TutorialOverlay` when requested. Also installs a
 * global `/` keyboard shortcut that dispatches `FOCUS_MARKET_SEARCH_EVENT` unless focus is inside
 * an editable element.
 *
 * A persistent Paper/Live mode toggle sits below the navigation: it reads the user's
 * `simulation_mode` setting, shows a PAPER/LIVE badge at all times, and flips the setting
 * through the copy-trading settings endpoint. In paper mode every strategy path (copy trading,
 * market maker, inverse bot, stop-loss/take-profit) records simulated fills instead of
 * submitting real orders.
 *
 * @module components/AppLayout
 */
import { useCallback, useEffect, useState } from "react";
import { Outlet } from "react-router-dom";
import { useAuthStore } from "../store/authStore";
import { authService } from "../services/authService";
import { settingsService } from "../services/settingsService";
import { getApiErrorMessage } from "../utils/apiError";
import Navigation from "./Navigation";
import TutorialLauncher from "./TutorialLauncher";
import { FOCUS_MARKET_SEARCH_EVENT } from "../utils/tradingWorkspace";
import { Banner } from "./ui";

function isEditableTarget(target: EventTarget | null): boolean {
  const el = target as HTMLElement | null;
  if (!el) return false;
  if (el.isContentEditable) return true;
  const tag = el.tagName.toLowerCase();
  if (tag === "input" || tag === "textarea" || tag === "select") return true;
  return !!el.closest("input, textarea, select, [contenteditable='true']");
}

export default function AppLayout() {
  const { walletAddress, isAuthenticated, clearSession } = useAuthStore();
  const [simulationMode, setSimulationMode] = useState<boolean | null>(null);
  const [paperBalance, setPaperBalance] = useState<number>(1000);
  const [paperBusy, setPaperBusy] = useState(false);
  const [settingsError, setSettingsError] = useState<string | null>(null);
  const [paperModeError, setPaperModeError] = useState<string | null>(null);
  const [settingsReloadKey, setSettingsReloadKey] = useState(0);

  const handleLogout = async () => {
    try {
      await authService.logout();
    } catch (err) {
      // The server-side cookie may fail to clear, but the session is dropped
      // below regardless — a failed logout must not become an unhandled
      // rejection or leave the confirm modal stuck open.
      console.warn("Logout request failed; clearing session anyway", err);
    } finally {
      clearSession();
    }
  };

  useEffect(() => {
    if (!isAuthenticated) {
      setSimulationMode(null);
      return;
    }
    let cancelled = false;
    void (async () => {
      try {
        const settings = await settingsService.getUserSettings();
        if (!cancelled) {
          setSimulationMode(settings.simulation_mode);
          setPaperBalance(settings.paper_balance);
          setSettingsError(null);
        }
      } catch (err: unknown) {
        // Settings unavailable — the toggle stays hidden, so say why instead
        // of leaving the user to guess whether they are trading paper or live.
        if (!cancelled) {
          setSettingsError(getApiErrorMessage(err, "Failed to load your trading mode"));
        }
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [walletAddress, isAuthenticated, settingsReloadKey]);

  const reloadSettings = useCallback(() => setSettingsReloadKey((k) => k + 1), []);

  const togglePaperMode = useCallback(async () => {
    if (simulationMode === null || paperBusy) return;
    const next = !simulationMode;
    setPaperBusy(true);
    setPaperModeError(null);
    try {
      const updated = await settingsService.updateCopyTradingSettings({
        simulation_mode: next,
      });
      // The badge only reflects server-confirmed state — a failed switch
      // must never silently look like it took effect.
      setSimulationMode(updated.simulation_mode);
      setPaperBalance(updated.paper_balance);
    } catch (err: unknown) {
      setPaperModeError(getApiErrorMessage(err, "Failed to switch trading mode"));
    } finally {
      setPaperBusy(false);
    }
  }, [simulationMode, paperBusy]);

  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.defaultPrevented) return;
      if (event.key !== "/") return;
      if (event.metaKey || event.ctrlKey || event.altKey) return;
      if (isEditableTarget(event.target)) return;
      event.preventDefault();
      window.dispatchEvent(new CustomEvent(FOCUS_MARKET_SEARCH_EVENT));
    };

    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, []);

  return (
    <div className="app-shell page-enter">
      <Navigation walletAddress={walletAddress || ""} onLogout={handleLogout} />

      {/* Persistent Paper/Live mode toggle */}
      {(simulationMode !== null || settingsError || paperModeError) && (
        <div className="relative z-0 max-w-7xl mx-auto px-4 pt-4">
          {settingsError && (
            <Banner
              variant="error"
              className="mb-2"
              onDismiss={() => setSettingsError(null)}
              onRetry={reloadSettings}
            >
              {settingsError}
            </Banner>
          )}
          {simulationMode !== null && (
            <div className="flex items-center justify-end gap-2 flex-wrap">
              <span
                className={`chip text-[10px] ${simulationMode ? "chip-warning" : "chip-success"}`}
                title={
                  simulationMode
                    ? "Paper mode: strategies record simulated fills, no real orders"
                    : "Live mode: strategies submit real orders"
                }
              >
                {simulationMode ? "PAPER" : "LIVE"}
              </span>
              {simulationMode && (
                <span className="text-xs text-muted">
                  Paper balance: ${paperBalance.toFixed(2)}
                </span>
              )}
              <button
                onClick={() => void togglePaperMode()}
                disabled={paperBusy}
                className="btn-muted text-xs px-3 py-1.5"
              >
                {paperBusy ? "Switching…" : simulationMode ? "Switch to Live" : "Switch to Paper"}
              </button>
            </div>
          )}
          {paperModeError && (
            <Banner variant="error" className="mt-2" onDismiss={() => setPaperModeError(null)}>
              {paperModeError}
            </Banner>
          )}
        </div>
      )}

      <main className="relative z-0 max-w-7xl mx-auto px-4 py-8">
        <Outlet />
      </main>

      {/* Floating tutorial launcher + overlay */}
      <TutorialLauncher />
    </div>
  );
}
