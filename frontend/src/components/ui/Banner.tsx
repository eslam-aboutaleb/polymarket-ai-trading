/**
 * Inline status banner — the single error/notice surface for the app.
 *
 * Wraps the existing `alert-error` / `alert-success` visual language so migrated
 * call sites look identical. Error banners announce via `role="alert"`, success
 * and notice banners via `role="status"`; both carry `aria-live="polite"`.
 *
 * @module components/ui/Banner
 */

export type BannerVariant = "error" | "success" | "notice";

export interface BannerProps {
  /** Visual + semantic treatment of the banner. */
  variant: BannerVariant;
  /** Banner content (usually the message text). */
  children?: React.ReactNode;
  /** When provided, renders a dismiss (✕) button. */
  onDismiss?: () => void;
  /** When provided, renders a retry button that calls this. */
  onRetry?: () => void;
  /** Accessible label for the dismiss button. */
  dismissLabel?: string;
  /** Label for the retry button. */
  retryLabel?: string;
  /** Extra classes for layout tweaks at the call site. */
  className?: string;
}

const VARIANT_CLASS: Record<BannerVariant, string> = {
  error: "alert-error",
  success: "alert-success",
  notice: "border border-yellow-500/30 bg-yellow-500/10 text-yellow-300",
};

const ROLE: Record<BannerVariant, "alert" | "status"> = {
  error: "alert",
  success: "status",
  notice: "status",
};

export default function Banner({
  variant,
  children,
  onDismiss,
  onRetry,
  dismissLabel = "Dismiss",
  retryLabel = "Retry",
  className = "",
}: BannerProps) {
  return (
    <div
      className={`p-3 rounded text-sm flex items-start justify-between gap-3 ${VARIANT_CLASS[variant]} ${className}`}
      role={ROLE[variant]}
      aria-live="polite"
    >
      <div className="flex-1 min-w-0">{children}</div>
      {(onDismiss || onRetry) && (
        <div className="flex items-center gap-2 shrink-0">
          {onRetry && (
            <button onClick={onRetry} className="btn-muted text-xs">
              {retryLabel}
            </button>
          )}
          {onDismiss && (
            <button
              onClick={onDismiss}
              aria-label={dismissLabel}
              className="text-current opacity-70 hover:opacity-100"
            >
              ✕
            </button>
          )}
        </div>
      )}
    </div>
  );
}
