/**
 * Detail overlay for a multi-outcome event group, listing every sub-market of the group.
 *
 * Renders a centered modal on mobile and a right-side slide-in drawer from `md` up, sharing the
 * same header (image, leading option, volume/liquidity) and sub-market list. Sub-market rows are
 * delegated to the `renderSubMarket` prop and prices to `parsePrices`, so this component stays
 * generic over any `GroupableMarket`. Escape closes it and body scroll is locked while open.
 *
 * @module components/EventGroupDetailDrawer
 */
import React, { useEffect, useCallback, ReactNode } from "react";
import { EventGroup, getSubMarketLabel } from "../utils/groupMarkets";
import { findLeadingOption, formatVolume, GroupableMarket } from "./EventGroupPanel";

interface EventGroupDetailDrawerProps<T extends GroupableMarket> {
  group: EventGroup<T>;
  /** Parse a market's yes/no prices */
  parsePrices: (m: T) => { yes: number; no: number };
  /** Render a single sub-market row */
  renderSubMarket: (market: T, subLabel: string, index: number) => ReactNode;
  /** Optional badge in the header (e.g. AI score) */
  headerBadge?: ReactNode;
  /** Close callback */
  onClose: () => void;
}

/**
 * Desktop (md+): slides in from the right as a fixed panel.
 * Mobile (<md): full-screen centered modal matching the existing modal pattern.
 */
export default function EventGroupDetailDrawer<T extends GroupableMarket>({
  group,
  parsePrices,
  renderSubMarket,
  headerBadge,
  onClose,
}: EventGroupDetailDrawerProps<T>) {
  // ── Escape key closes ──
  const handleKey = useCallback(
    (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    },
    [onClose],
  );

  useEffect(() => {
    document.addEventListener("keydown", handleKey);
    // Prevent body scroll on mobile when modal is open
    const prev = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    return () => {
      document.removeEventListener("keydown", handleKey);
      document.body.style.overflow = prev;
    };
  }, [handleKey]);

  const imgUrl = group.eventImage;
  const leader = findLeadingOption(group.markets, parsePrices, group.eventTitle, (m, title) =>
    getSubMarketLabel(m, title),
  );
  const volStr = formatVolume(group.eventVolume);
  const liqStr = formatVolume(group.eventLiquidity);

  // ── Shared header content ──
  const headerContent = (
    <div className="p-5 border-b border-[var(--line)]">
      <div className="flex items-start gap-3">
        {imgUrl && (
          <img
            src={imgUrl}
            alt=""
            className="w-12 h-12 rounded-lg object-cover flex-shrink-0"
            onError={(e) => {
              (e.target as HTMLImageElement).style.display = "none";
            }}
          />
        )}
        <div className="flex-1 min-w-0">
          <h2 className="text-base font-bold text-white leading-snug">{group.eventTitle}</h2>
          <div className="flex items-center gap-2 mt-1 flex-wrap">
            {leader && (
              <span className="text-xs font-medium text-[var(--accent)]">
                {leader.pct}% {leader.label}
              </span>
            )}
            {leader && (volStr || liqStr) && <span className="text-xs text-muted">·</span>}
            {volStr && <span className="text-xs text-muted">{volStr} Vol.</span>}
            {liqStr && (
              <>
                <span className="text-xs text-muted">·</span>
                <span className="text-xs text-muted">{liqStr} Liq.</span>
              </>
            )}
            <span className="text-xs text-muted">·</span>
            <span className="text-xs text-muted">{group.markets.length} options</span>
          </div>
          {group.markets.length > 1 && (
            <p className="text-[11px] text-muted mt-1">Other options available below</p>
          )}
        </div>
        {headerBadge && <div className="flex-shrink-0 mt-0.5">{headerBadge}</div>}
      </div>
    </div>
  );

  // ── Sub-markets list ──
  const subMarketsList = (
    <div className="divide-y divide-[var(--line)] overflow-y-auto flex-1">
      {group.markets.map((market, i) => {
        const subLabel = getSubMarketLabel(market, group.eventTitle);
        return (
          <div key={i} className="px-5 py-4">
            {renderSubMarket(market, subLabel, i)}
          </div>
        );
      })}
    </div>
  );

  return (
    <>
      {/* ═══════ MOBILE (<md): Centered modal ═══════ */}
      <div className="md:hidden fixed inset-0 z-[120]">
        {/* Backdrop */}
        <div className="absolute inset-0 modal-overlay" onClick={onClose} />
        {/* Modal panel */}
        <div className="relative flex items-end sm:items-center justify-center min-h-full p-4">
          <div
            className="surface-panel w-full max-w-lg max-h-[85vh] rounded-xl overflow-hidden flex flex-col animate-[lift-in_200ms_ease-out]"
            onClick={(e) => e.stopPropagation()}
          >
            {/* Close handle (drag-style bar for mobile) */}
            <div className="flex justify-center pt-3 pb-1">
              <div className="w-10 h-1 rounded-full bg-[var(--line-strong)]" />
            </div>

            {/* Close button */}
            <button
              onClick={onClose}
              className="absolute top-3 right-3 text-muted hover:text-white transition z-10 p-1"
              aria-label="Close"
            >
              <svg
                className="w-5 h-5"
                fill="none"
                viewBox="0 0 24 24"
                stroke="currentColor"
                strokeWidth={2}
              >
                <path strokeLinecap="round" strokeLinejoin="round" d="M6 18L18 6M6 6l12 12" />
              </svg>
            </button>

            {headerContent}
            {subMarketsList}
          </div>
        </div>
      </div>

      {/* ═══════ DESKTOP (md+): Right-side slide-in drawer ═══════ */}
      <div className="hidden md:block fixed inset-x-0 bottom-0 top-16 z-[120]">
        {/* Backdrop (semi-transparent, left side) */}
        <div
          className="absolute inset-0 z-[120] bg-black/40 backdrop-blur-[2px] animate-[fade-in_150ms_ease-out]"
          onClick={onClose}
        />
        {/* Drawer panel */}
        <div
          className="absolute top-0 right-0 z-[121] h-full w-[460px] max-w-[90vw] surface-panel border-l border-[var(--line)] flex flex-col shadow-2xl animate-[slide-in-right_250ms_ease-out]"
          onClick={(e) => e.stopPropagation()}
        >
          {/* Close button */}
          <button
            onClick={onClose}
            className="absolute top-4 right-4 text-muted hover:text-white transition z-10 p-1"
            aria-label="Close drawer"
          >
            <svg
              className="w-5 h-5"
              fill="none"
              viewBox="0 0 24 24"
              stroke="currentColor"
              strokeWidth={2}
            >
              <path strokeLinecap="round" strokeLinejoin="round" d="M6 18L18 6M6 6l12 12" />
            </svg>
          </button>

          {headerContent}
          {subMarketsList}
        </div>
      </div>
    </>
  );
}
