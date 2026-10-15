/**
 * Modal accessibility core — the single place focus trap, focus restore, Escape,
 * backdrop dismissal and the reference-counted scroll lock live.
 *
 * Extracted from `Modal` so non-centered surfaces (the event-group drawer, the
 * click-anchored position-analysis popover, the notification drawer) can adopt
 * the same a11y contract while keeping their own layout.
 *
 * @module components/ui/useModalA11y
 */

import { useEffect, useId, useRef } from "react";

export interface UseModalA11yOptions {
  /** Whether the surface is currently open. */
  open: boolean;
  /** Called on Escape and on backdrop dismissal. */
  onClose: () => void;
  /** When `true`, the Escape key does not close. */
  disableEscape?: boolean;
}

export interface UseModalA11yResult {
  /** Attach to the panel element that owns the dialog semantics. */
  panelRef: React.RefObject<HTMLDivElement>;
  /** Id for the title element; wire it via `aria-labelledby`. */
  titleId: string;
  /**
   * Attach to the overlay element's `onMouseDown`/`onClick` — it closes only
   * when the event target is the overlay itself, so clicks and text selection
   * inside the panel never dismiss.
   */
  onOverlayMouseDown: (event: React.MouseEvent<HTMLElement>) => void;
}

const FOCUSABLE_SELECTOR =
  'a[href],button:not([disabled]),textarea,input,select,[tabindex]:not([tabindex="-1"])';

let lockCount = 0;
let previousOverflow = "";

function lockScroll(): void {
  if (lockCount === 0) {
    previousOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
  }
  lockCount += 1;
}

function unlockScroll(): void {
  lockCount = Math.max(0, lockCount - 1);
  if (lockCount === 0) {
    document.body.style.overflow = previousOverflow;
    previousOverflow = "";
  }
}

export function useModalA11y({
  open,
  onClose,
  disableEscape = false,
}: UseModalA11yOptions): UseModalA11yResult {
  const titleId = useId();
  const panelRef = useRef<HTMLDivElement>(null);
  const onCloseRef = useRef(onClose);
  const disableEscapeRef = useRef(disableEscape);

  useEffect(() => {
    onCloseRef.current = onClose;
    disableEscapeRef.current = disableEscape;
  }, [onClose, disableEscape]);

  useEffect(() => {
    if (!open) return;
    const panel = panelRef.current;
    if (!panel) return;

    const previouslyFocused = document.activeElement as HTMLElement | null;
    const focusable = () => Array.from(panel.querySelectorAll<HTMLElement>(FOCUSABLE_SELECTOR));

    const first = focusable()[0];
    if (first) first.focus({ preventScroll: true });
    else panel.focus({ preventScroll: true });

    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        if (disableEscapeRef.current) return;
        event.preventDefault();
        onCloseRef.current();
        return;
      }
      if (event.key !== "Tab") return;

      const items = focusable();
      if (items.length === 0) {
        event.preventDefault();
        panel.focus({ preventScroll: true });
        return;
      }

      const index = items.indexOf(document.activeElement as HTMLElement);
      if (index === -1) {
        event.preventDefault();
        (event.shiftKey ? items[items.length - 1] : items[0]).focus({ preventScroll: true });
        return;
      }

      const next = event.shiftKey
        ? (index - 1 + items.length) % items.length
        : (index + 1) % items.length;
      event.preventDefault();
      items[next].focus({ preventScroll: true });
    };

    document.addEventListener("keydown", onKeyDown);
    lockScroll();

    return () => {
      document.removeEventListener("keydown", onKeyDown);
      unlockScroll();
      if (previouslyFocused && previouslyFocused.isConnected) {
        previouslyFocused.focus({ preventScroll: true });
      }
    };
  }, [open]);

  const onOverlayMouseDown = (event: React.MouseEvent<HTMLElement>) => {
    if (event.target !== event.currentTarget) return;
    onCloseRef.current();
  };

  return { panelRef, titleId, onOverlayMouseDown };
}
