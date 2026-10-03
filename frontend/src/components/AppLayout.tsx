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
import Navigation from "./Navigation";
import TutorialOverlay, { useTutorialState } from "./TutorialOverlay";
import { FOCUS_MARKET_SEARCH_EVENT } from "../utils/tradingWorkspace";

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
  const { showTutorial, startTutorial, completeTutorial, dismissTutorial } = useTutorialState();
  const [simulationMode, setSimulationMode] = useState<boolean | null>(null);
  const [paperBalance, setPaperBalance] = useState<number>(1000);
  const [paperBusy, setPaperBusy] = useState(false);

  const handleLogout = async () => {
    try {
      await authService.logout();
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
        }
      } catch {
        // Settings unavailable — the toggle stays hidden.
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [walletAddress, isAuthenticated]);

  const togglePaperMode = useCallback(async () => {
    if (simulationMode === null || paperBusy) return;
    const next = !simulationMode;
    setPaperBusy(true);
    try {
      const updated = await settingsService.updateCopyTradingSettings({
        simulation_mode: next,
      });
      setSimulationMode(updated.simulation_mode);
      setPaperBalance(updated.paper_balance);
    } catch {
      // Keep the current mode when the update fails.
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
      {simulationMode !== null && (
        <div className="relative z-0 max-w-7xl mx-auto px-4 pt-4">
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
              <span className="text-xs text-muted">Paper balance: ${paperBalance.toFixed(2)}</span>
            )}
            <button
              onClick={() => void togglePaperMode()}
              disabled={paperBusy}
              className="btn-muted text-xs px-3 py-1.5"
            >
              {paperBusy ? "Switching…" : simulationMode ? "Switch to Live" : "Switch to Paper"}
            </button>
          </div>
        </div>
      )}

      <main className="relative z-0 max-w-7xl mx-auto px-4 py-8">
        <Outlet />
      </main>

      {/* Floating tutorial button */}
      <button
        onClick={startTutorial}
        className="fixed bottom-6 right-6 z-50 w-12 h-12 rounded-full btn-accent flex items-center justify-center shadow-lg hover:scale-110 transition-transform"
        title="Start Tutorial"
        aria-label="Start guided tutorial"
      >
        <svg className="w-5 h-5" fill="none" viewBox="0 0 24 24" stroke="currentColor">
          <path
            strokeLinecap="round"
            strokeLinejoin="round"
            strokeWidth={2}
            d="M9.663 17h4.673M12 3v1m6.364 1.636l-.707.707M21 12h-1M4 12H3m3.343-5.657l-.707-.707m2.828 9.9a5 5 0 117.072 0l-.548.547A3.374 3.374 0 0014 18.469V19a2 2 0 11-4 0v-.531c0-.895-.356-1.754-.988-2.386l-.548-.547z"
          />
        </svg>
      </button>

      {/* Tutorial overlay */}
      {showTutorial && (
        <TutorialOverlay onComplete={completeTutorial} onDismiss={dismissTutorial} />
      )}
    </div>
  );
}
