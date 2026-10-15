/**
 * Toast state provider and fixed container.
 *
 * Holds a capped list (3 visible, oldest dropped), dedupes identical messages
 * within 3s, auto-dismisses each toast after 5s, and renders through a single
 * portal so toasts always stack above modals (`z-index.toast`).
 *
 * @module components/ui/ToastProvider
 */

import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from "react";
import { createPortal } from "react-dom";
import { resolveZ } from "../../lib/zIndex";
import Toast, { type ToastAction, type ToastItem } from "./Toast";

const MAX_VISIBLE = 3;
const AUTO_DISMISS_MS = 5000;
const DEDUPE_WINDOW_MS = 3000;

export interface ToastOptions {
  actionLabel?: string;
  onAction?: () => void;
}

export interface ToastApi {
  success: (message: string, options?: ToastOptions) => void;
  error: (message: string, options?: ToastOptions) => void;
  notice: (message: string, options?: ToastOptions) => void;
}

const ToastContext = createContext<ToastApi | null>(null);

let nextId = 1;

export function ToastProvider({ children }: { children: ReactNode }) {
  const [toasts, setToasts] = useState<ToastItem[]>([]);
  const recentRef = useRef<Array<{ message: string; at: number }>>([]);

  const dismiss = useCallback((id: number) => {
    setToasts((prev) => prev.filter((t) => t.id !== id));
  }, []);

  const push = useCallback(
    (message: string, variant: ToastItem["variant"], options?: ToastOptions) => {
      const now = Date.now();
      recentRef.current = recentRef.current.filter((entry) => now - entry.at < DEDUPE_WINDOW_MS);
      if (recentRef.current.some((entry) => entry.message === message)) return;
      recentRef.current.push({ message, at: now });

      const action: ToastAction | undefined =
        options?.actionLabel && options.onAction
          ? { actionLabel: options.actionLabel, onAction: options.onAction }
          : undefined;

      const id = nextId++;
      setToasts((prev) => {
        const next = [...prev, { id, message, variant, action }];
        return next.length > MAX_VISIBLE ? next.slice(next.length - MAX_VISIBLE) : next;
      });
    },
    [],
  );

  const api = useMemo<ToastApi>(
    () => ({
      success: (message, options) => push(message, "success", options),
      error: (message, options) => push(message, "error", options),
      notice: (message, options) => push(message, "notice", options),
    }),
    [push],
  );

  return (
    <ToastContext.Provider value={api}>
      {children}
      <ToastContainer toasts={toasts} onDismiss={dismiss} />
    </ToastContext.Provider>
  );
}

function ToastContainer({
  toasts,
  onDismiss,
}: {
  toasts: ToastItem[];
  onDismiss: (id: number) => void;
}) {
  const [mounted, setMounted] = useState(false);

  useEffect(() => {
    setMounted(true);
  }, []);

  if (!mounted) return null;

  return createPortal(
    <div
      className={`fixed top-4 right-4 w-80 max-w-[calc(100vw-2rem)] space-y-2 ${resolveZ("toast")}`}
    >
      {toasts.map((toast) => (
        <ToastCard key={toast.id} toast={toast} onDismiss={onDismiss} />
      ))}
    </div>,
    document.body,
  );
}

function ToastCard({ toast, onDismiss }: { toast: ToastItem; onDismiss: (id: number) => void }) {
  useEffect(() => {
    const timer = setTimeout(() => onDismiss(toast.id), AUTO_DISMISS_MS);
    return () => clearTimeout(timer);
  }, [toast.id, onDismiss]);

  return <Toast toast={toast} onDismiss={onDismiss} />;
}

/** Access the toast API. Must be used inside a {@link ToastProvider}. */
export function useToast(): ToastApi {
  const ctx = useContext(ToastContext);
  if (!ctx) {
    throw new Error("useToast must be used within a ToastProvider");
  }
  return ctx;
}
