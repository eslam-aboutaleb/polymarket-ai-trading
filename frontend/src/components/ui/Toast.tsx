/**
 * Presentational toast card used by {@link ToastProvider}'s container.
 *
 * @module components/ui/Toast
 */

export interface ToastAction {
  actionLabel: string;
  onAction: () => void;
}

export interface ToastItem {
  id: number;
  message: string;
  variant: "success" | "error" | "notice";
  action?: ToastAction;
}

export interface ToastProps {
  toast: ToastItem;
  onDismiss: (id: number) => void;
}

const VARIANT_CLASS = {
  success: "alert-success",
  error: "alert-error",
  notice: "border border-yellow-500/30 bg-yellow-500/10 text-yellow-300",
} as const;

export default function Toast({ toast, onDismiss }: ToastProps) {
  return (
    <div
      className={`p-3 rounded text-sm flex items-start justify-between gap-3 shadow-lg ${VARIANT_CLASS[toast.variant]}`}
      role="status"
      aria-live="polite"
    >
      <div className="flex-1 min-w-0">{toast.message}</div>
      <div className="flex items-center gap-2 shrink-0">
        {toast.action && (
          <button
            onClick={() => {
              toast.action?.onAction();
              onDismiss(toast.id);
            }}
            className="btn-muted text-xs"
          >
            {toast.action.actionLabel}
          </button>
        )}
        <button
          onClick={() => onDismiss(toast.id)}
          aria-label="Dismiss notification"
          className="text-current opacity-70 hover:opacity-100"
        >
          ✕
        </button>
      </div>
    </div>
  );
}
