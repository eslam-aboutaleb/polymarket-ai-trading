/**
 * Real-time price stream consumer (SSE) with polling fallback.
 *
 * Connects to GET /api/portfolio/prices/stream, reconnects with
 * exponential backoff when the stream drops, ignores stale ticks
 * (older than 30s), and falls back to the caller's poll function
 * on the previous 15s cadence while the stream is down.
 *
 * @module hooks/usePriceStream
 */

import { useEffect, useRef, useState } from "react";
import { API_BASE_URL } from "../config/api";
import { consumeSSEStream } from "../services/sseStream";

export type PriceStreamStatus = "connecting" | "live" | "delayed";

/** Reconnect backoff bounds (ms). */
const RECONNECT_BASE_DELAY_MS = 1000;
const RECONNECT_MAX_DELAY_MS = 30000;
/** Ticks older than this are ignored (ms). */
const STALE_TICK_MS = 30000;
/** Default cadence for the polling fallback (ms). */
const DEFAULT_FALLBACK_POLL_INTERVAL_MS = 15000;

export interface PriceStreamTick {
  token_id?: unknown;
  price?: unknown;
  ts?: unknown;
}

export interface UsePriceStreamOptions {
  /** Token ids to subscribe to (position asset ids / market token ids). */
  tokenIds: string[];
  /** Set false to suspend the stream entirely. */
  enabled?: boolean;
  /** Polling fallback invoked on the fallback cadence while SSE is down. */
  fallbackPoll?: () => Promise<Record<string, number>>;
  /** Polling fallback cadence in ms (default 15s). */
  fallbackPollIntervalMs?: number;
  /** Invoked with each batch of prices (stream ticks or poll snapshot). */
  onPrices?: (prices: Record<string, number>) => void;
}

/** True when a tick timestamp is older than the 30s staleness window. */
export function isStaleTick(ts: unknown, now: number = Date.now()): boolean {
  if (typeof ts !== "string" && typeof ts !== "number") return false;
  const tsMs = typeof ts === "number" ? ts : Date.parse(ts);
  if (!Number.isFinite(tsMs)) return false;
  return now - tsMs > STALE_TICK_MS;
}

export function usePriceStream(options: UsePriceStreamOptions): {
  prices: Record<string, number>;
  status: PriceStreamStatus;
} {
  const {
    tokenIds,
    enabled = true,
    fallbackPoll,
    fallbackPollIntervalMs = DEFAULT_FALLBACK_POLL_INTERVAL_MS,
    onPrices,
  } = options;

  const [prices, setPrices] = useState<Record<string, number>>({});
  const [status, setStatus] = useState<PriceStreamStatus>("connecting");

  // Refs keep the reconnect loop stable across re-renders: only the
  // joined token key and the cadence restart the stream.
  const fallbackPollRef = useRef(fallbackPoll);
  fallbackPollRef.current = fallbackPoll;
  const onPricesRef = useRef(onPrices);
  onPricesRef.current = onPrices;

  const tokenIdsKey = tokenIds.join(",");

  useEffect(() => {
    if (!enabled || tokenIdsKey === "") {
      setStatus("connecting");
      return;
    }

    const tokens = tokenIdsKey.split(",");
    let cancelled = false;
    let reconnectTimer: ReturnType<typeof setTimeout> | null = null;
    let fallbackTimer: ReturnType<typeof setInterval> | null = null;
    let reconnectDelay = RECONNECT_BASE_DELAY_MS;
    const controller = new AbortController();

    const clearFallbackTimer = () => {
      if (fallbackTimer !== null) {
        clearInterval(fallbackTimer);
        fallbackTimer = null;
      }
    };

    const applyPrices = (batch: Record<string, number>) => {
      if (cancelled || Object.keys(batch).length === 0) return;
      setPrices((prev) => ({ ...prev, ...batch }));
      onPricesRef.current?.(batch);
    };

    const runFallbackPoll = async () => {
      const poll = fallbackPollRef.current;
      if (!poll || cancelled) return;
      try {
        applyPrices(await poll());
      } catch {
        // Polling failures are retried on the next cadence tick.
      }
    };

    const startFallback = () => {
      clearFallbackTimer();
      void runFallbackPoll();
      fallbackTimer = setInterval(() => {
        void runFallbackPoll();
      }, fallbackPollIntervalMs);
    };

    const scheduleReconnect = () => {
      if (cancelled) return;
      reconnectTimer = setTimeout(() => {
        reconnectTimer = null;
        void connect();
      }, reconnectDelay);
      reconnectDelay = Math.min(reconnectDelay * 2, RECONNECT_MAX_DELAY_MS);
    };

    const connect = async () => {
      if (cancelled) return;
      const params = new URLSearchParams();
      params.set("token_ids", tokens.join(","));
      try {
        const res = await fetch(
          `${API_BASE_URL}/api/portfolio/prices/stream?${params.toString()}`,
          { credentials: "include", signal: controller.signal },
        );
        if (!res.ok) {
          throw new Error(`HTTP ${res.status}`);
        }
        setStatus("live");
        reconnectDelay = RECONNECT_BASE_DELAY_MS;
        clearFallbackTimer();
        // Blocks until the stream ends; the finally block below
        // schedules the reconnect and the polling fallback.
        await consumeSSEStream(res, {
          onEvent: (payload) => {
            const tick = payload as PriceStreamTick;
            if (typeof tick.token_id !== "string" || typeof tick.price !== "number") {
              return;
            }
            if (isStaleTick(tick.ts)) return;
            applyPrices({ [tick.token_id]: tick.price });
          },
        });
      } catch (err) {
        if ((err as Error)?.name === "AbortError") return;
      } finally {
        if (cancelled) return;
        setStatus("delayed");
        startFallback();
        scheduleReconnect();
      }
    };

    void connect();

    return () => {
      cancelled = true;
      controller.abort();
      if (reconnectTimer !== null) clearTimeout(reconnectTimer);
      clearFallbackTimer();
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [tokenIdsKey, enabled, fallbackPollIntervalMs]);

  return { prices, status };
}
