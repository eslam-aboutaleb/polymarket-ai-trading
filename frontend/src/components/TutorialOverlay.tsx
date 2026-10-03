/**
 * Modal onboarding tour driven by defaultTutorialSteps, plus its localStorage-backed state hook.
 *
 * Renders through a portal (so transformed ancestors cannot break fixed positioning) with arrow,
 * Enter and Escape navigation and clickable step dots; onComplete fires only on the last step.
 * Step targetSelector/position metadata is currently unused beyond the plain dimming backdrop.
 *
 * @module components/TutorialOverlay
 */
import { useState, useEffect, useCallback } from "react";
import { createPortal } from "react-dom";

/* ─── Tutorial Step Definition ─── */

export interface TutorialStep {
  /** Unique key for the step */
  id: string;
  /** Short title shown in the header */
  title: string;
  /** Main description / explanation */
  description: string;
  /** Optional: CSS selector of the element to highlight */
  targetSelector?: string;
  /** Optional: position of the tooltip relative to the highlighted element */
  position?: "top" | "bottom" | "left" | "right";
  /** Optional: screenshot/image URL */
  image?: string;
  /** Optional: small tip text shown below the description */
  tip?: string;
}

/* ─── Default Tutorial Steps ─── */

export const defaultTutorialSteps: TutorialStep[] = [
  {
    id: "welcome",
    title: "Welcome to Polymarket AI",
    description:
      "This tutorial will walk you through the key features of the trading bot. You'll learn how to navigate the platform, copy top traders, run backtests, and use AI analysis.",
    tip: "You can restart this tutorial anytime from the Guide page.",
  },
  {
    id: "dashboard",
    title: "Dashboard Overview",
    description:
      "The Dashboard is your home base. It shows your portfolio value, open positions with live prices that update every 15 seconds, profit/loss tracking, and a feed of trades from traders you follow.",
    targetSelector: '[data-tutorial="dashboard"]',
    position: "bottom",
    tip: "Click any position card to set stop-loss or take-profit orders.",
  },
  {
    id: "markets",
    title: "Browse Markets",
    description:
      "The Markets page lets you explore all active Polymarket events. Search by keyword, view current prices, and click any market to trade or analyze it. Use the '/' keyboard shortcut for quick search.",
    targetSelector: '[data-tutorial="markets"]',
    position: "bottom",
  },
  {
    id: "leaderboard",
    title: "Discover Top Traders",
    description:
      "The Leaderboard ranks traders by performance. Click any trader to view their profile, trading history, and win rate. Use the Follow button to start tracking their trades in your feed.",
    targetSelector: '[data-tutorial="leaderboard"]',
    position: "bottom",
    tip: "Look for traders with high win rates AND high trade counts for reliability.",
  },
  {
    id: "copy-trading",
    title: "Copy Trading Setup",
    description:
      "Once you follow traders from the Leaderboard, go to Copy Trading to enable automatic trade copying. Choose sizing mode (fixed or proportional), set risk limits, and toggle copying ON. The bot mirrors trades in real time with multi-phase execution.",
    targetSelector: '[data-tutorial="copy-trading"]',
    position: "bottom",
    tip: "Start with small position sizes while learning a trader's patterns.",
  },
  {
    id: "opportunities",
    title: "AI Opportunities",
    description:
      "The Opportunities page shows AI-scored markets ranked by potential. Each opportunity includes a confidence score, recommended action, and detailed reasoning. Click 'Analyze' for an in-depth AI report.",
    targetSelector: '[data-tutorial="opportunities"]',
    position: "bottom",
  },
  {
    id: "market-making",
    title: "Market Making",
    description:
      "Become a liquidity provider! Market Making places buy and sell orders around the current price to earn the spread. Choose between Bands strategy (multiple order levels) or AMM strategy (curve-based pricing). Configure and start from this page.",
    targetSelector: '[data-tutorial="market-making"]',
    position: "bottom",
    tip: "Market making works best in liquid markets with consistent volume.",
  },
  {
    id: "backtesting",
    title: "Backtest Strategies",
    description:
      "Test strategies against historical data before risking real capital. Run copy-trade replays or indicator-based strategies (RSI, MACD, Bollinger Bands). View detailed metrics including Sharpe ratio, max drawdown, and trade-by-trade logs.",
    targetSelector: '[data-tutorial="backtesting"]',
    position: "bottom",
    tip: "A Sharpe ratio above 1.5 with 50+ trades is a strong signal.",
  },
  {
    id: "trades",
    title: "Trade History & Risk Tools",
    description:
      "The Trades page shows all executed trades, open positions, available stop-loss orders, and the Inverse Bot. Enable the Inverse Bot on any position to let AI automatically reverse positions when sentiment shifts.",
    targetSelector: '[data-tutorial="trades"]',
    position: "bottom",
  },
  {
    id: "settings",
    title: "Configure Your Bot",
    description:
      "In Settings, configure: default trading parameters, risk limits, AI backend provider (GPT-4, Claude, Gemini, etc.), and notification preferences. Admins can manage user roles and switch LLM providers.",
    targetSelector: '[data-tutorial="settings"]',
    position: "bottom",
  },
  {
    id: "guide",
    title: "Your Guide",
    description:
      "Need help later? Visit 'Your Guide' from the navigation menu for comprehensive documentation on every feature, strategy explanations, and troubleshooting tips.",
    targetSelector: '[data-tutorial="guide"]',
    position: "bottom",
  },
  {
    id: "complete",
    title: "You're All Set! 🎉",
    description:
      "You now know the essential features of Polymarket AI. Start by exploring the Dashboard, follow some top traders from the Leaderboard, and try a backtest before going live. Happy trading!",
    tip: "Remember: always start with small positions and use stop-losses.",
  },
];

/* ─── Spotlight Overlay ─── */

function SpotlightOverlay() {
  return (
    <div className="fixed inset-0 z-[90] pointer-events-none">
      <div className="absolute inset-0 bg-black/60" />
    </div>
  );
}

/* ─── Progress Bar ─── */

function ProgressBar({ current, total }: { current: number; total: number }) {
  const progress = ((current + 1) / total) * 100;
  return (
    <div className="w-full bg-[var(--bg-soft)] rounded-full h-1.5 overflow-hidden">
      <div
        className="h-full bg-gradient-to-r from-[var(--accent)] to-[var(--accent-strong)] rounded-full transition-all duration-500 ease-out"
        style={{ width: `${progress}%` }}
      />
    </div>
  );
}

/* ─── Tutorial Modal/Tooltip ─── */

interface TutorialOverlayProps {
  steps?: TutorialStep[];
  onComplete: () => void;
  onDismiss: () => void;
}

export default function TutorialOverlay({
  steps = defaultTutorialSteps,
  onComplete,
  onDismiss,
}: TutorialOverlayProps) {
  const [currentStep, setCurrentStep] = useState(0);
  const step = steps[currentStep];
  const isFirst = currentStep === 0;
  const isLast = currentStep === steps.length - 1;

  const handleNext = useCallback(() => {
    if (isLast) {
      onComplete();
    } else {
      setCurrentStep((p) => p + 1);
    }
  }, [isLast, onComplete]);

  const handleBack = useCallback(() => {
    if (!isFirst) {
      setCurrentStep((p) => p - 1);
    }
  }, [isFirst]);

  // Keyboard navigation
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "ArrowRight" || e.key === "Enter") handleNext();
      else if (e.key === "ArrowLeft") handleBack();
      else if (e.key === "Escape") onDismiss();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [handleNext, handleBack, onDismiss]);

  // Scroll to top when tutorial opens so the modal is visible
  useEffect(() => {
    window.scrollTo({ top: 0, behavior: "smooth" });
  }, []);

  // Use a portal so the overlay is rendered directly in document.body,
  // escaping any ancestor with CSS transform (which breaks fixed positioning).
  return createPortal(
    <>
      {/* Overlay backdrop */}
      <SpotlightOverlay />

      {/* Modal card */}
      <div className="fixed inset-0 z-[95] flex items-center justify-center p-4 pointer-events-none">
        <div
          className="pointer-events-auto w-full max-w-lg surface-panel border border-[var(--line-strong)] shadow-2xl"
          style={{
            animation: "lift-in 0.35s ease both",
          }}
        >
          {/* Header */}
          <div className="flex items-center justify-between px-5 pt-5 pb-2">
            <div className="flex items-center gap-2">
              <span className="chip chip-accent text-xs">
                {currentStep + 1} / {steps.length}
              </span>
              <h3 className="text-base font-bold text-white">{step.title}</h3>
            </div>
            <button
              onClick={onDismiss}
              className="text-muted hover:text-[var(--danger)] transition p-1"
              aria-label="Close tutorial"
            >
              <svg className="w-5 h-5" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                <path
                  strokeLinecap="round"
                  strokeLinejoin="round"
                  strokeWidth={2}
                  d="M6 18L18 6M6 6l12 12"
                />
              </svg>
            </button>
          </div>

          {/* Progress */}
          <div className="px-5 py-2">
            <ProgressBar current={currentStep} total={steps.length} />
          </div>

          {/* Content */}
          <div className="px-5 py-3 space-y-3">
            <p className="text-sm text-soft leading-relaxed">{step.description}</p>

            {step.tip && (
              <div className="flex items-start gap-2 bg-[var(--accent-soft)] border border-[rgba(247,166,0,0.3)] rounded-lg px-3 py-2">
                <span className="text-sm mt-0.5">💡</span>
                <p className="text-xs text-[#ffcc66] leading-relaxed">{step.tip}</p>
              </div>
            )}
          </div>

          {/* Step dots */}
          <div className="px-5 py-2 flex justify-center gap-1.5">
            {steps.map((_, idx) => (
              <button
                key={idx}
                onClick={() => setCurrentStep(idx)}
                className={`w-2 h-2 rounded-full transition-all duration-300 ${
                  idx === currentStep
                    ? "bg-[var(--accent)] w-5"
                    : idx < currentStep
                      ? "bg-[var(--accent)]/50"
                      : "bg-[var(--bg-soft)]"
                }`}
                aria-label={`Go to step ${idx + 1}`}
              />
            ))}
          </div>

          {/* Footer buttons */}
          <div className="flex items-center justify-between px-5 pb-5 pt-2">
            <button
              onClick={onDismiss}
              className="text-xs text-muted hover:text-[var(--text-secondary)] transition"
            >
              Skip tutorial
            </button>

            <div className="flex items-center gap-2">
              {!isFirst && (
                <button onClick={handleBack} className="btn-muted text-sm">
                  ← Back
                </button>
              )}
              <button onClick={handleNext} className="btn-accent text-sm">
                {isFirst ? "Start Tutorial →" : isLast ? "Finish Tutorial ✓" : "Next →"}
              </button>
            </div>
          </div>
        </div>
      </div>
    </>,
    document.body,
  );
}

/* ─── Hook for tutorial state persistence ─── */

const TUTORIAL_STORAGE_KEY = "polymarket_tutorial_completed";

export function useTutorialState() {
  const [showTutorial, setShowTutorial] = useState(false);
  const [completed, setCompleted] = useState(() => {
    try {
      return localStorage.getItem(TUTORIAL_STORAGE_KEY) === "true";
    } catch {
      return false;
    }
  });

  const startTutorial = useCallback(() => setShowTutorial(true), []);

  const completeTutorial = useCallback(() => {
    setShowTutorial(false);
    setCompleted(true);
    try {
      localStorage.setItem(TUTORIAL_STORAGE_KEY, "true");
    } catch {
      // ignore
    }
  }, []);

  const dismissTutorial = useCallback(() => {
    setShowTutorial(false);
  }, []);

  const resetTutorial = useCallback(() => {
    setCompleted(false);
    try {
      localStorage.removeItem(TUTORIAL_STORAGE_KEY);
    } catch {
      // ignore
    }
  }, []);

  return {
    showTutorial,
    completed,
    startTutorial,
    completeTutorial,
    dismissTutorial,
    resetTutorial,
  };
}
