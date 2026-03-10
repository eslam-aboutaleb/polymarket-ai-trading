import React, { ReactNode } from "react";
import {
  EventGroup,
  resolvePrimaryGroupOption,
} from "../utils/groupMarkets";
import { AnalysisDisplayInline } from "./AnalysisDisplay";

/**
 * Find the leading (highest Yes-price) sub-market in a group.
 * Returns its label + probability percentage.
 */
export function findLeadingOption<T>(
  markets: T[],
  priceFn: (m: T) => { yes: number; no: number },
  eventTitle: string,
  getLabel: (m: T, title: string) => string,
): { label: string; pct: number } | null {
  if (markets.length === 0) return null;
  let bestIdx = 0;
  let bestYes = -1;
  for (let i = 0; i < markets.length; i++) {
    const p = priceFn(markets[i]);
    if (p.yes > bestYes) {
      bestYes = p.yes;
      bestIdx = i;
    }
  }
  if (bestYes <= 0) return null;
  return {
    label: getLabel(markets[bestIdx], eventTitle),
    pct: Math.round(bestYes * 100),
  };
}

/**
 * Format a volume number into a short human-readable string.
 * e.g. 480123456 → "$480.1M", 12345 → "$12.3K"
 */
export function formatVolume(vol: string | number | undefined): string | null {
  if (vol == null) return null;
  const n = typeof vol === "string" ? parseFloat(vol) : vol;
  if (isNaN(n) || n <= 0) return null;
  if (n >= 1_000_000) return `$${(n / 1_000_000).toFixed(1)}M`;
  if (n >= 1_000) return `$${(n / 1_000).toFixed(1)}K`;
  return `$${n.toFixed(0)}`;
}

/** Minimal type constraint shared by EventGroupPanel and EventGroupDetailDrawer */
export type GroupableMarket = {
  _event_slug?: string;
  _event_title?: string;
  _event_image?: string;
  _event_volume?: string;
  _event_liquidity?: string;
  _event_volume_24hr?: number;
  question?: string;
  groupItemTitle?: string;
};

interface EventGroupPanelProps<T extends GroupableMarket> {
  group: EventGroup<T>;
  /** Parse a market's yes/no prices — must be provided by the consuming page */
  parsePrices: (m: T) => { yes: number; no: number };
  /** Optional AI-recommended option label to prioritize as primary */
  preferredOptionLabel?: string;
  /** Called when user clicks the card — opens the detail drawer/modal */
  onSelect?: () => void;
  /** Trade callback for the resolved primary option */
  onTradePrimary?: (market: T) => void;
  /** Analysis callback for the resolved primary option */
  onAnalyzePrimary?: (market: T) => void;
  /** Quick group-level analysis callback (analyze event as one trade) */
  onQuickAnalyzeGroup?: () => void;
  /** Group quick-analysis loading state */
  quickAnalyzeLoading?: boolean;
  /** Group quick-analysis error text */
  quickAnalyzeError?: string;
  /** Group quick-analysis text */
  quickAnalyzeText?: string;
  /** CTA label for the group quick-analysis button */
  quickCtaLabel?: string;
  /** CTA label for the primary analysis button */
  analyzeCtaLabel?: string;
  /** Optional badge to show in the header (e.g. AI score) */
  headerBadge?: ReactNode;
  /** Whether this group is currently selected / active */
  isSelected?: boolean;
  /** Whether this event is favorited */
  isFavorite?: boolean;
  /** Toggle event favorite status */
  onToggleFavorite?: () => void;
  /** Extra CSS classes on the container */
  className?: string;
}

/**
 * Compact event-group card. Clicking it calls `onSelect` which opens
 * the detail drawer (desktop) or modal (mobile) in the parent page.
 * No inline expansion — sub-markets are shown in the drawer instead.
 */
export default function EventGroupPanel<T extends GroupableMarket>({
  group,
  parsePrices,
  preferredOptionLabel,
  onSelect,
  onTradePrimary,
  onAnalyzePrimary,
  onQuickAnalyzeGroup,
  quickAnalyzeLoading = false,
  quickAnalyzeError,
  quickAnalyzeText,
  quickCtaLabel = "Quick Analyze",
  analyzeCtaLabel = "AI Analysis",
  headerBadge,
  isSelected = false,
  isFavorite = false,
  onToggleFavorite,
  className = "",
}: EventGroupPanelProps<T>) {
  const imgUrl = group.eventImage;

  const primary = resolvePrimaryGroupOption(group, {
    parsePrices,
    preferredOptionLabel,
  });

  // ── Aggregate volume ──
  const volStr = formatVolume(group.eventVolume);

  return (
    <div
      className={`surface-panel overflow-hidden transition-all duration-200 cursor-pointer w-full h-[320px] flex flex-col ${
        isSelected
          ? "ring-1 ring-[var(--accent)] border-[var(--accent)]"
          : "hover:border-[var(--line-strong)]"
      } ${className}`}
      onClick={onSelect}
    >
      <div className="flex items-center gap-3 p-4 pb-3">
        {/* Event image */}
        {imgUrl && (
          <img
            src={imgUrl}
            alt=""
            className="w-9 h-9 rounded object-cover flex-shrink-0"
            onError={(e) => {
              (e.target as HTMLImageElement).style.display = "none";
            }}
          />
        )}

        {/* Title + leading option + stats */}
        <div className="flex-1 min-w-0">
          <h3 className="text-sm font-semibold text-white leading-snug truncate">
            {group.eventTitle}
          </h3>
          <div className="flex items-center gap-2 mt-0.5 flex-wrap">
            {primary && (
              <span className="text-xs font-medium text-[var(--accent)]">
                {Math.round(primary.prices.yes * 100)}% {primary.label}
              </span>
            )}
            {primary && (volStr || group.markets.length > 1) && (
              <span className="text-xs text-muted">·</span>
            )}
            {volStr && (
              <span className="text-xs text-muted">{volStr} Vol.</span>
            )}
            {volStr && group.markets.length > 1 && (
              <span className="text-xs text-muted">·</span>
            )}
            {group.markets.length > 1 && (
              <span className="text-xs text-muted">
                {group.markets.length} options
              </span>
            )}
          </div>
        </div>

        {/* Optional header badge (e.g. best AI score) */}
        {headerBadge && <div className="flex-shrink-0">{headerBadge}</div>}

        {onToggleFavorite && (
          <button
            type="button"
            title={isFavorite ? "Remove from favorites" : "Add to favorites"}
            onClick={(e) => {
              e.stopPropagation();
              onToggleFavorite();
            }}
            className={`w-6 h-6 rounded-full border text-xs transition ${
              isFavorite
                ? "border-[var(--accent)] bg-[var(--accent-soft)] text-[var(--accent)]"
                : "border-[var(--line)] text-muted hover:text-white"
            }`}
          >
            {isFavorite ? "★" : "☆"}
          </button>
        )}

        {/* Right arrow — indicates opens detail */}
        <svg
          className="w-4 h-4 text-muted flex-shrink-0"
          fill="none"
          viewBox="0 0 24 24"
          stroke="currentColor"
          strokeWidth={2}
        >
          <path strokeLinecap="round" strokeLinejoin="round" d="M9 5l7 7-7 7" />
        </svg>
      </div>

      {primary && (
        <div className="px-4 pb-4 mt-auto">
          <div className="surface-soft border border-[var(--line)] rounded-lg p-3 space-y-2">
            <div className="flex items-center gap-2 min-w-0">
              <span
                className={`text-[10px] uppercase tracking-wide px-1.5 py-0.5 rounded border ${
                  primary.source === "ai_recommended"
                    ? "text-emerald-300 bg-emerald-500/15 border-emerald-500/30"
                    : "text-blue-300 bg-blue-500/15 border-blue-500/30"
                }`}
              >
                {primary.source === "ai_recommended" ? "AI Pick" : "Most popular"}
              </span>
              <span className="text-xs text-white truncate">{primary.label}</span>
            </div>
            <div className="flex items-center justify-between gap-2 flex-wrap">
              <div className="flex items-center gap-2">
                <span className="chip chip-success px-2 py-0.5 text-xs">
                  Yes {(primary.prices.yes * 100).toFixed(1)}¢
                </span>
                <span className="chip chip-danger px-2 py-0.5 text-xs">
                  No {(primary.prices.no * 100).toFixed(1)}¢
                </span>
              </div>
              <div className="flex items-center gap-2 ml-auto">
                {onQuickAnalyzeGroup && (
                  <button
                    onClick={(e) => {
                      e.stopPropagation();
                      onQuickAnalyzeGroup();
                    }}
                    disabled={quickAnalyzeLoading}
                    className={`btn-muted text-xs px-2 py-1 ${
                      quickAnalyzeLoading ? "opacity-70 cursor-not-allowed" : ""
                    }`}
                  >
                    {quickAnalyzeLoading ? "Analyzing..." : quickCtaLabel}
                  </button>
                )}
                <button
                  onClick={(e) => {
                    e.stopPropagation();
                    onAnalyzePrimary?.(primary.market);
                  }}
                  disabled={!onAnalyzePrimary}
                  className={`btn-accent text-xs px-2 py-1 ${
                    !onAnalyzePrimary ? "opacity-50 cursor-not-allowed" : ""
                  }`}
                >
                  {analyzeCtaLabel}
                </button>
                <button
                  onClick={(e) => {
                    e.stopPropagation();
                    onTradePrimary?.(primary.market);
                  }}
                  disabled={!onTradePrimary}
                  className={`btn-success text-xs px-2 py-1 ${
                    !onTradePrimary ? "opacity-50 cursor-not-allowed" : ""
                  }`}
                >
                  Trade
                </button>
              </div>
            </div>
            {quickAnalyzeError && (
              <div className="p-2 alert-error rounded text-xs">
                {quickAnalyzeError}
              </div>
            )}
            {quickAnalyzeText && (
              <div className="p-2 bg-[var(--accent-soft)] border border-[#f0b74166] rounded text-xs max-h-24 overflow-y-auto">
                <AnalysisDisplayInline text={quickAnalyzeText} />
              </div>
            )}
          </div>
        </div>
      )}
    </div>
  );
}
