/**
 * Notification center: bell icon with unread count and a slide-in drawer.
 *
 * Polls `/api/notifications` for the alert feed, shows the unread count
 * badge on the bell, and opens a right-side drawer (mobile: full-screen
 * modal) listing events newest-first. Selecting an event marks it read via
 * `PATCH /api/notifications/{id}/read`; "Mark all read" fires one PATCH
 * per unread event. Escape closes the drawer and body scroll is locked
 * while it is open. Mount inside the app shell navigation (see
 * AppLayout/Navigation — integration point for plan 02/07).
 *
 * @module components/NotificationCenter
 */
import React, { useCallback, useEffect, useState } from "react";
import {
  NotificationEvent,
  listNotifications,
  markNotificationRead,
} from "../services/notificationsService";
import { getApiErrorMessage } from "../utils/apiError";

const EVENT_LABELS: Record<string, string> = {
  fill: "Order Fill",
  stop_loss_hit: "Stop Loss Hit",
  take_profit_hit: "Take Profit Hit",
  followed_trader_activity: "Followed Trader Activity",
  arbitrage_detected: "Arbitrage Detected",
  latency_arb_opportunity: "Latency Arbitrage Opportunity",
  whale_alert: "Whale Alert",
  redeem_available: "Redemption Available",
  redeem_failed: "Redemption Failed",
  system_halt: "System Halt",
  dead_man_switch: "Dead Man Switch",
  drawdown_warning: "Drawdown Warning",
};

const POLL_INTERVAL_MS = 30_000;
const FETCH_LIMIT = 100;

function eventLabel(eventType: string): string {
  return EVENT_LABELS[eventType] || eventType.replace(/_/g, " ");
}

function eventSummary(event: NotificationEvent): string {
  const parts: string[] = [];
  if (event.trader_wallet) parts.push(event.trader_wallet);
  if (event.market_id) parts.push(event.market_id);
  if (event.side) parts.push(event.side);
  if (event.size) parts.push(`size ${event.size}`);
  if (event.price) parts.push(`@ ${event.price}`);
  return parts.join(" · ");
}

export default function NotificationCenter() {
  const [open, setOpen] = useState(false);
  const [events, setEvents] = useState<NotificationEvent[]>([]);
  const [unreadCount, setUnreadCount] = useState(0);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [markingIds, setMarkingIds] = useState<Set<number>>(new Set());

  const refresh = useCallback(async () => {
    try {
      const list = await listNotifications(false, FETCH_LIMIT);
      setEvents(list);
      setUnreadCount(list.filter((e) => e.unread).length);
      setError(null);
    } catch (err: unknown) {
      setError(getApiErrorMessage(err, "Failed to load notifications"));
    }
  }, []);

  useEffect(() => {
    refresh();
    const timer = setInterval(refresh, POLL_INTERVAL_MS);
    return () => clearInterval(timer);
  }, [refresh]);

  // Escape key closes the drawer; lock body scroll while open.
  useEffect(() => {
    if (!open) return;
    const handleKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") setOpen(false);
    };
    document.addEventListener("keydown", handleKey);
    const prev = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    return () => {
      document.removeEventListener("keydown", handleKey);
      document.body.style.overflow = prev;
    };
  }, [open]);

  const handleMarkRead = async (event: NotificationEvent) => {
    if (!event.unread || markingIds.has(event.id)) return;
    setMarkingIds((prev) => new Set(prev).add(event.id));
    try {
      const updated = await markNotificationRead(event.id);
      setEvents((prev) => prev.map((e) => (e.id === updated.id ? { ...e, ...updated } : e)));
      setUnreadCount((prev) => Math.max(0, prev - 1));
    } catch (err: unknown) {
      setError(getApiErrorMessage(err, "Failed to mark notification read"));
    } finally {
      setMarkingIds((prev) => {
        const next = new Set(prev);
        next.delete(event.id);
        return next;
      });
    }
  };

  const handleMarkAllRead = async () => {
    const unread = events.filter((e) => e.unread);
    if (unread.length === 0) return;
    setLoading(true);
    try {
      const results = await Promise.allSettled(unread.map((e) => markNotificationRead(e.id)));
      const readIds = new Set(
        results
          .map((r, i) => (r.status === "fulfilled" ? unread[i].id : null))
          .filter((id): id is number => id !== null),
      );
      setEvents((prev) =>
        prev.map((e) =>
          readIds.has(e.id) ? { ...e, unread: false, read_at: new Date().toISOString() } : e,
        ),
      );
      setUnreadCount((prev) => Math.max(0, prev - readIds.size));
    } catch (err: unknown) {
      setError(getApiErrorMessage(err, "Failed to mark notifications read"));
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className="relative">
      <button
        type="button"
        onClick={() => {
          setOpen(true);
          refresh();
        }}
        className="relative p-2 rounded-md text-muted hover:text-white hover:bg-[var(--bg-soft)] transition-colors"
        title="Notifications"
        aria-label={`Notifications (${unreadCount} unread)`}
      >
        <svg className="w-5 h-5" fill="none" viewBox="0 0 24 24" stroke="currentColor">
          <path
            strokeLinecap="round"
            strokeLinejoin="round"
            strokeWidth={2}
            d="M15 17h5l-1.405-1.405A2.032 2.032 0 0118 14.158V11a6.002 6.002 0 00-4-5.659V5a2 2 0 10-4 0v.341C7.67 6.165 6 8.388 6 11v3.159c0 .538-.214 1.055-.595 1.436L4 17h5m6 0v1a3 3 0 11-6 0v-1m6 0H9"
          />
        </svg>
        {unreadCount > 0 && (
          <span className="absolute -top-0.5 -right-0.5 min-w-[18px] h-[18px] px-1 rounded-full bg-red-500 text-white text-[10px] font-bold flex items-center justify-center">
            {unreadCount > 99 ? "99+" : unreadCount}
          </span>
        )}
      </button>

      {open && (
        <>
          <div
            className="fixed inset-0 bg-black/50 z-40"
            onClick={() => setOpen(false)}
            aria-hidden="true"
          />
          <aside className="fixed right-0 top-0 h-full w-full sm:w-96 bg-[var(--bg)] border-l border-[var(--line)] z-50 flex flex-col">
            <div className="flex items-center justify-between p-4 border-b border-[var(--line)]">
              <h2 className="text-lg font-bold">Notifications</h2>
              <div className="flex items-center gap-2">
                <button
                  type="button"
                  onClick={handleMarkAllRead}
                  disabled={loading || unreadCount === 0}
                  className="text-xs text-[var(--accent)] hover:underline disabled:opacity-40 disabled:no-underline"
                >
                  Mark all read
                </button>
                <button
                  type="button"
                  onClick={() => setOpen(false)}
                  className="p-1 text-muted hover:text-white"
                  aria-label="Close notifications"
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
            </div>

            {error && (
              <div className="alert-error mx-4 mt-3 px-3 py-2 rounded text-sm">{error}</div>
            )}

            <div className="flex-1 overflow-y-auto">
              {events.length === 0 && !loading ? (
                <p className="text-muted text-sm p-6 text-center">No notifications yet</p>
              ) : (
                <ul className="divide-y divide-[var(--line)]">
                  {events.map((event) => (
                    <li key={event.id}>
                      <button
                        type="button"
                        onClick={() => handleMarkRead(event)}
                        disabled={markingIds.has(event.id)}
                        className={`w-full text-left px-4 py-3 hover:bg-[var(--bg-soft)] transition-colors ${
                          event.unread ? "bg-[var(--accent-soft)]/40" : ""
                        }`}
                      >
                        <div className="flex items-start justify-between gap-2">
                          <span className="text-sm font-medium text-white">
                            {eventLabel(event.event_type)}
                          </span>
                          <span className="text-[11px] text-muted flex-shrink-0">
                            {new Date(event.created_at).toLocaleString()}
                          </span>
                        </div>
                        {eventSummary(event) && (
                          <p className="text-xs text-soft mt-0.5 truncate">{eventSummary(event)}</p>
                        )}
                        {event.unread && (
                          <span className="inline-block mt-1 w-2 h-2 rounded-full bg-[var(--accent)]" />
                        )}
                      </button>
                    </li>
                  ))}
                </ul>
              )}
            </div>
          </aside>
        </>
      )}
    </div>
  );
}
