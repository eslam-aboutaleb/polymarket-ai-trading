/**
 * Accessible modal dialog — the single modal primitive for the app.
 *
 * Controlled via `open`, rendered through one portal into `document.body`, so
 * callers never wire `createPortal` themselves. Wraps the existing
 * `modal-overlay` / `surface-panel` visual language; the focus trap, focus
 * restore, Escape handling and scroll lock live in `useModalA11y`.
 *
 * @module components/ui/Modal
 */

import { createPortal } from "react-dom";
import { resolveZ, type ZLayer } from "../../lib/zIndex";
import { useModalA11y } from "./useModalA11y";

export type ModalSize = "sm" | "md" | "lg";

export interface ModalProps {
  /** Whether the dialog is rendered. When `false` the component renders nothing. */
  open: boolean;
  /** Called on Escape, backdrop click, and the close button. */
  onClose: () => void;
  /** Rendered in the header row and wired up via `aria-labelledby`. */
  title?: string;
  /** Panel width variant. Defaults to `"md"`. */
  size?: ModalSize;
  /** Dialog body content. */
  children: React.ReactNode;
  /** Rendered below the body in a bordered footer row. */
  footer?: React.ReactNode;
  /** Backdrop clicks close the dialog. Defaults to `true`. */
  dismissOnBackdrop?: boolean;
  /** Render the header close (✕) button. Defaults to `true`. */
  showClose?: boolean;
  /** When `true`, the Escape key does not close the dialog. */
  disableEscape?: boolean;
  /** Stacking layer for the overlay; defaults to `"modal"`. */
  zLevel?: ZLayer;
  /** Accessible label for the header close button. Defaults to `"Close"`. */
  closeLabel?: string;
  /** Extra classes appended to the panel. */
  className?: string;
}

const MAX_WIDTH: Record<ModalSize, string> = {
  sm: "max-w-sm",
  md: "max-w-lg",
  lg: "max-w-2xl",
};

export default function Modal({
  open,
  onClose,
  title,
  size = "md",
  children,
  footer,
  dismissOnBackdrop = true,
  showClose = true,
  disableEscape = false,
  zLevel = "modal",
  closeLabel = "Close",
  className = "",
}: ModalProps) {
  const { panelRef, titleId, onOverlayMouseDown } = useModalA11y({ open, onClose, disableEscape });

  if (!open) return null;

  return createPortal(
    <div
      className={`fixed inset-0 modal-overlay flex items-center justify-center p-4 ${resolveZ(zLevel)}`}
      onClick={(event: React.MouseEvent<HTMLDivElement>) => {
        if (dismissOnBackdrop === false) return;
        onOverlayMouseDown(event);
      }}
    >
      <div
        ref={panelRef}
        role="dialog"
        aria-modal="true"
        aria-labelledby={title ? titleId : undefined}
        tabIndex={-1}
        className={`surface-panel relative z-10 flex w-full flex-col ${MAX_WIDTH[size]} max-h-[90vh] overflow-hidden rounded-xl ${className}`}
      >
        {(title || showClose) && (
          <div className="flex items-start justify-between gap-3 border-b border-[var(--line)] px-5 pb-3 pt-5">
            {title && (
              <h2 id={titleId} className="text-base font-semibold text-white">
                {title}
              </h2>
            )}
            {showClose && (
              <button
                type="button"
                onClick={onClose}
                aria-label={closeLabel}
                className="ml-auto shrink-0 rounded-lg p-1.5 text-soft transition hover:bg-[var(--bg-soft)]"
              >
                ✕
              </button>
            )}
          </div>
        )}

        <div className="min-h-0 flex-1 overflow-y-auto px-5 py-4 text-sm text-soft">{children}</div>

        {footer && (
          <div className="flex items-center justify-end gap-2 border-t border-[var(--line)] px-5 py-4">
            {footer}
          </div>
        )}
      </div>
    </div>,
    document.body,
  );
}
