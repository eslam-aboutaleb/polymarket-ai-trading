/**
 * Accessible glossary tooltip — the in-app explanation surface for domain jargon.
 *
 * Renders a small info trigger button beside a label and, while open, a
 * popover with one to three plain-English lines. The trigger is a real
 * `<button>`, so focus plus Enter/Space toggles it; Escape and an outside
 * click close it. The popover renders through a portal with viewport-fixed
 * positioning, so no scroll container or transformed ancestor can clip it,
 * and it is measured after open so it flips above the trigger and clamps
 * into the viewport instead of overflowing it.
 *
 * @module components/ui/InfoTooltip
 */

import { useEffect, useId, useLayoutEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { resolveZ } from "../../lib/zIndex";

export interface InfoTooltipProps {
  /** The jargon term being explained — drives the trigger's `aria-label`. */
  term: string;
  /** Plain-English explanation, one to three short lines. */
  children: React.ReactNode;
  /** Extra classes for the wrapper when a call site needs a layout tweak. */
  className?: string;
}

const VIEWPORT_MARGIN = 8;
const TRIGGER_GAP = 6;

export default function InfoTooltip({ term, children, className = "" }: InfoTooltipProps) {
  const [open, setOpen] = useState(false);
  const [placement, setPlacement] = useState({ top: 0, left: 0 });
  const wrapperRef = useRef<HTMLSpanElement>(null);
  const triggerRef = useRef<HTMLButtonElement>(null);
  const popoverRef = useRef<HTMLDivElement>(null);
  const popoverId = useId();

  useEffect(() => {
    if (!open) return;

    const onDocumentMouseDown = (event: MouseEvent) => {
      const target = event.target as Node;
      const insideTrigger = wrapperRef.current?.contains(target);
      const insidePopover = popoverRef.current?.contains(target);
      if (!insideTrigger && !insidePopover) {
        setOpen(false);
      }
    };
    // Capture phase, so a tooltip opened inside a `<Modal>` consumes Escape
    // itself instead of also triggering the modal's own Escape handler.
    const onDocumentKeyDown = (event: KeyboardEvent) => {
      if (event.key !== "Escape") return;
      setOpen(false);
      triggerRef.current?.focus();
      event.stopImmediatePropagation();
    };

    document.addEventListener("mousedown", onDocumentMouseDown);
    document.addEventListener("keydown", onDocumentKeyDown, true);
    return () => {
      document.removeEventListener("mousedown", onDocumentMouseDown);
      document.removeEventListener("keydown", onDocumentKeyDown, true);
    };
  }, [open]);

  useLayoutEffect(() => {
    if (!open) return;
    const trigger = triggerRef.current;
    const popover = popoverRef.current;
    if (!trigger || !popover) return;

    const reposition = () => {
      const triggerRect = trigger.getBoundingClientRect();
      const popoverRect = popover.getBoundingClientRect();
      const fitsBelow =
        triggerRect.bottom + TRIGGER_GAP + popoverRect.height <=
        window.innerHeight - VIEWPORT_MARGIN;
      const maxLeft = Math.max(
        VIEWPORT_MARGIN,
        window.innerWidth - VIEWPORT_MARGIN - popoverRect.width,
      );
      const top = fitsBelow
        ? triggerRect.bottom + TRIGGER_GAP
        : Math.max(VIEWPORT_MARGIN, triggerRect.top - TRIGGER_GAP - popoverRect.height);
      setPlacement({
        top,
        left: Math.min(Math.max(VIEWPORT_MARGIN, triggerRect.left), maxLeft),
      });
    };

    reposition();
    window.addEventListener("resize", reposition);
    window.addEventListener("scroll", reposition, true);
    return () => {
      window.removeEventListener("resize", reposition);
      window.removeEventListener("scroll", reposition, true);
    };
  }, [open]);

  return (
    <span
      ref={wrapperRef}
      className={`inline-flex align-middle ${className}`}
      onBlur={(event: React.FocusEvent<HTMLSpanElement>) => {
        if (!open) return;
        if (!event.currentTarget.contains(event.relatedTarget as Node | null)) {
          setOpen(false);
        }
      }}
    >
      <button
        ref={triggerRef}
        type="button"
        aria-label={`What does ${term} mean?`}
        aria-expanded={open}
        aria-describedby={open ? popoverId : undefined}
        onClick={() => setOpen((value) => !value)}
        className="ml-1 inline-flex h-4 w-4 shrink-0 items-center justify-center rounded-full border border-[var(--line-strong)] p-0 text-soft transition hover:border-[var(--accent)] hover:text-[var(--accent)] focus:outline-none focus-visible:ring-2 focus-visible:ring-[var(--accent)]"
      >
        <svg
          className="h-3 w-3"
          fill="none"
          viewBox="0 0 24 24"
          stroke="currentColor"
          aria-hidden="true"
        >
          <path
            strokeLinecap="round"
            strokeLinejoin="round"
            strokeWidth={2}
            d="M11.25 11.25l.041-.02a.75.75 0 011.063.852l-.708 2.836a.75.75 0 001.063.853l.041-.021M21 12a9 9 0 11-18 0 9 9 0 0118 0zm-9-3.75h.008v.008H12V8.25z"
          />
        </svg>
      </button>

      {open &&
        createPortal(
          <div
            ref={popoverRef}
            id={popoverId}
            role="tooltip"
            style={{ top: placement.top, left: placement.left }}
            className={`surface-panel fixed w-72 max-w-[92vw] rounded-lg p-3 text-xs leading-relaxed text-soft shadow-lg ${resolveZ("modalNested")}`}
          >
            {children}
          </div>,
          document.body,
        )}
    </span>
  );
}
