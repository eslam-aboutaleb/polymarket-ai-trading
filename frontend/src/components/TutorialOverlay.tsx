/**
 * Modal onboarding tour driven by defaultTutorialSteps, plus its localStorage-backed state hook.
 *
 * Renders through a portal (so transformed ancestors cannot break fixed positioning) with arrow,
 * Enter and Escape navigation and clickable step dots; onComplete fires only on the last step.
 * Steps that declare a targetSelector resolve the element with `document.querySelector`, measure it
 * with `getBoundingClientRect`, and render a fixed highlight ring plus the tooltip card placed next
 * to it (honouring the step's `position`, clamped to the viewport). Because the tutorial is global
 * while the user may sit on any route, a missing or off-screen target falls back to the centered
 * card — the tour never renders nothing.
 *
 * @module components/TutorialOverlay
 */
import { useState, useEffect, useLayoutEffect, useCallback } from "react";
import { createPortal } from "react-dom";
import { useModalA11y } from "./ui/useModalA11y";
import { resolveZ } from "../lib/zIndex";

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
    id: "whales",
    title: "Whale Watch",
    description:
      "The Whales page streams whale-sized position movements detected on-chain — typically 3–30 seconds ahead of the public positions API. Set a minimum notional, track the leaderboard whale set, and optionally auto-copy followed-whale trades.",
    targetSelector: '[data-tutorial="whales"]',
    position: "bottom",
    tip: "Alerts fire only above your minimum notional — lower it to catch more wallets.",
  },
  {
    id: "news",
    title: "AI Market News",
    description:
      "News turns market data, trader behaviour and social signals into AI-generated articles. Refresh the feed to cover trending markets, or generate an article on demand for any market by pasting its condition id.",
    targetSelector: '[data-tutorial="news"]',
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
    id: "latency-arb",
    title: "Latency Arbitrage",
    description:
      "Latency Arb watches BTC/ETH/SOL up-down markets and flags mispricings caught in the Binance feed before Polymarket odds adjust. The board shows Model P, Market P, edge, feed lag and a countdown — paper-first, so simulated fills never touch the exchange.",
    targetSelector: '[data-tutorial="latency-arb"]',
    position: "bottom",
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
    id: "calibration",
    title: "Is the AI Actually Right?",
    description:
      "AI Calibration scores the AI's probability forecasts against what really happened. Watch the Brier score against the 0.25 coin-flip baseline, the reliability curve per model, and each recent verdict with its edge and resolution.",
    targetSelector: '[data-tutorial="calibration"]',
    position: "bottom",
    tip: "A mean Brier below 0.25 beats a coin flip — treat anything above it as noise.",
  },
  {
    id: "execution-analytics",
    title: "Execution Analytics",
    description:
      "Execution Analytics grades how well your orders execute: fill rate, average slippage, and fee drag against gross realized PnL. The Edge Score per strategy runs 0–100, where 50 is break-even.",
    targetSelector: '[data-tutorial="execution-analytics"]',
    position: "bottom",
  },
  {
    id: "risk",
    title: "Risk Report",
    description:
      "The Risk Report grades your portfolio exposure: concentration per position, a worst-case loss bound, a VaR estimate, correlated clusters, suggested hedges and upcoming catalysts — computed from your wallet.",
    targetSelector: '[data-tutorial="risk"]',
    position: "bottom",
  },
  {
    id: "analysis",
    title: "Deep Dive on One Market",
    description:
      "Analysis streams an independent AI deep-dive on a single market — probability, confidence, recommended action and copy-worthiness. Open it from any market's Analyze button.",
    targetSelector: '[data-tutorial="analysis"]',
    position: "bottom",
  },
  {
    id: "ops",
    title: "Ops Center",
    description:
      "Ops Center is the control room: emergency stop, resume, trader rescoring, arbitrage scanning, Binance signal refresh and the analysis endpoints. Start here when something needs immediate intervention.",
    targetSelector: '[data-tutorial="ops"]',
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

/* ─── Spotlight geometry ─── */

interface TargetRect {
  top: number;
  left: number;
  width: number;
  height: number;
}

const RING_GAP = 6;
const TOOLTIP_GAP = 14;
const VIEWPORT_MARGIN = 12;
const FALLBACK_POSITION = "bottom" as const;

function isOnScreen(rect: TargetRect): boolean {
  if (rect.width <= 0 || rect.height <= 0) return false;
  return (
    rect.top + rect.height > 0 &&
    rect.left + rect.width > 0 &&
    rect.top < window.innerHeight &&
    rect.left < window.innerWidth
  );
}

type SpotlightSide = NonNullable<TutorialStep["position"]>;

function placeTooltip(
  rect: TargetRect,
  preferred: TutorialStep["position"],
  card: { width: number; height: number },
): { top: number; left: number } {
  const slots: Record<SpotlightSide, { top: number; left: number }> = {
    top: {
      top: rect.top - TOOLTIP_GAP - card.height,
      left: rect.left + rect.width / 2 - card.width / 2,
    },
    bottom: {
      top: rect.top + rect.height + TOOLTIP_GAP,
      left: rect.left + rect.width / 2 - card.width / 2,
    },
    left: {
      top: rect.top + rect.height / 2 - card.height / 2,
      left: rect.left - TOOLTIP_GAP - card.width,
    },
    right: {
      top: rect.top + rect.height / 2 - card.height / 2,
      left: rect.left + rect.width + TOOLTIP_GAP,
    },
  };

  const fits = (side: SpotlightSide): boolean => {
    const slot = slots[side];
    return (
      slot.left >= VIEWPORT_MARGIN &&
      slot.top >= VIEWPORT_MARGIN &&
      slot.left + card.width <= window.innerWidth - VIEWPORT_MARGIN &&
      slot.top + card.height <= window.innerHeight - VIEWPORT_MARGIN
    );
  };

  const desired = preferred ?? FALLBACK_POSITION;
  const order: SpotlightSide[] = [desired, "bottom", "top", "right", "left"];
  const slot = slots[order.find(fits) ?? desired];

  const clamp = (value: number, extent: number) =>
    Math.round(Math.min(Math.max(VIEWPORT_MARGIN, value), Math.max(VIEWPORT_MARGIN, extent)));

  return {
    top: clamp(slot.top, window.innerHeight - card.height - VIEWPORT_MARGIN),
    left: clamp(slot.left, window.innerWidth - card.width - VIEWPORT_MARGIN),
  };
}

/* ─── Spotlight Overlay ─── */

function SpotlightOverlay({ targetRect }: { targetRect: TargetRect | null }) {
  return (
    <div className={`fixed inset-0 ${resolveZ("tutorial")} pointer-events-none`}>
      <div className="absolute inset-0 bg-black/60" />
      {targetRect && (
        <div
          data-testid="tutorial-spotlight-ring"
          className="absolute rounded-xl border-2 border-[var(--accent)]"
          style={{
            top: targetRect.top - RING_GAP,
            left: targetRect.left - RING_GAP,
            width: targetRect.width + RING_GAP * 2,
            height: targetRect.height + RING_GAP * 2,
            boxShadow: "0 0 0 4px rgba(247, 166, 0, 0.22), 0 0 28px rgba(247, 166, 0, 0.35)",
          }}
        />
      )}
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
  const { panelRef, titleId } = useModalA11y({
    open: true,
    onClose: onDismiss,
    disableEscape: true,
  });
  const [currentStep, setCurrentStep] = useState(0);
  const step = steps[currentStep];
  const isFirst = currentStep === 0;
  const isLast = currentStep === steps.length - 1;
  const [targetRect, setTargetRect] = useState<TargetRect | null>(null);

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

  const targetSelector = step.targetSelector;
  useEffect(() => {
    setTargetRect(null);
    if (!targetSelector) return;
    const measure = () => {
      const el = document.querySelector(targetSelector);
      if (!el) {
        setTargetRect(null);
        return;
      }
      const r = el.getBoundingClientRect();
      const rect: TargetRect = { top: r.top, left: r.left, width: r.width, height: r.height };
      if (!isOnScreen(rect)) {
        setTargetRect(null);
        return;
      }
      setTargetRect((prev) =>
        prev &&
        prev.top === rect.top &&
        prev.left === rect.left &&
        prev.width === rect.width &&
        prev.height === rect.height
          ? prev
          : rect,
      );
    };
    measure();
    window.addEventListener("resize", measure);
    window.addEventListener("scroll", measure, true);
    return () => {
      window.removeEventListener("resize", measure);
      window.removeEventListener("scroll", measure, true);
    };
  }, [targetSelector]);

  const targetPosition = step.position;
  useLayoutEffect(() => {
    const panel = panelRef.current;
    if (!panel) return;
    if (!targetRect) {
      panel.style.position = "";
      panel.style.top = "";
      panel.style.left = "";
      panel.style.width = "";
      return;
    }
    const width = panel.offsetWidth;
    const height = panel.offsetHeight;
    const placement = placeTooltip(targetRect, targetPosition, { width, height });
    panel.style.position = "fixed";
    panel.style.width = `${width}px`;
    panel.style.top = `${placement.top}px`;
    panel.style.left = `${placement.left}px`;
  }, [panelRef, targetRect, targetPosition]);

  // Use a portal so the overlay is rendered directly in document.body,
  // escaping any ancestor with CSS transform (which breaks fixed positioning).
  return createPortal(
    <>
      {/* Overlay backdrop + highlight ring */}
      <SpotlightOverlay targetRect={targetRect} />

      {/* Modal card */}
      <div
        className={`fixed inset-0 ${resolveZ("tutorial")} flex items-center justify-center p-4 pointer-events-none`}
      >
        <div
          ref={panelRef}
          role="dialog"
          aria-modal="true"
          aria-labelledby={titleId}
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
              <h3 id={titleId} className="text-base font-bold text-white">
                {step.title}
              </h3>
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
const TUTORIAL_VALUE_COMPLETED = "true";
const TUTORIAL_VALUE_DISMISSED = "dismissed";

function readTutorialValue(): string | null {
  try {
    return localStorage.getItem(TUTORIAL_STORAGE_KEY);
  } catch {
    return null;
  }
}

function writeTutorialValue(value: string): void {
  try {
    localStorage.setItem(TUTORIAL_STORAGE_KEY, value);
  } catch {
    // ignore
  }
}

function clearTutorialValue(): void {
  try {
    localStorage.removeItem(TUTORIAL_STORAGE_KEY);
  } catch {
    // ignore
  }
}

export function useTutorialState() {
  const [showTutorial, setShowTutorial] = useState(false);
  const [completed, setCompleted] = useState(
    () => readTutorialValue() === TUTORIAL_VALUE_COMPLETED,
  );
  const [seen, setSeen] = useState(() => {
    const value = readTutorialValue();
    return value === TUTORIAL_VALUE_COMPLETED || value === TUTORIAL_VALUE_DISMISSED;
  });

  const startTutorial = useCallback(() => setShowTutorial(true), []);

  const completeTutorial = useCallback(() => {
    setShowTutorial(false);
    setCompleted(true);
    setSeen(true);
    writeTutorialValue(TUTORIAL_VALUE_COMPLETED);
  }, []);

  const dismissTutorial = useCallback(() => {
    setShowTutorial(false);
    setSeen(true);
    if (!completed) {
      writeTutorialValue(TUTORIAL_VALUE_DISMISSED);
    }
  }, [completed]);

  const resetTutorial = useCallback(() => {
    setCompleted(false);
    setSeen(false);
    clearTutorialValue();
  }, []);

  return {
    showTutorial,
    completed,
    seen,
    startTutorial,
    completeTutorial,
    dismissTutorial,
    resetTutorial,
  };
}
