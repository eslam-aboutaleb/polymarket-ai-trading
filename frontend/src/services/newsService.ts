/**
 * Typed client for the news generation API.
 *
 * Non-streaming calls go through the shared axios instance. `streamGenerate`
 * instead opens a raw `fetch` because the endpoint speaks the *named* SSE
 * dialect (`event: status|article|done|error` followed by a `data:` line), not
 * the JSON envelope the analysis endpoints use; it shares the line-buffering
 * primitive from `sseStream` and returns both an AbortController and a promise
 * so callers can cancel and can await completion.
 *
 * @module services/newsService
 */
import { apiClient } from "./apiClient";
import { API_BASE_URL } from "../config/api";
import { streamLines } from "./sseStream";

// ── Types ──────────────────────────────────────────────────

export interface NewsArticle {
  condition_id: string;
  question: string;
  headline: string;
  summary: string;
  body: string;
  sentiment: string;
  confidence: number;
  key_insights: string[];
  trader_behavior_summary: string;
  market_outlook: string;
  tags: string[];
  generated_at: string;
  provider?: string;
  model?: string;
}

export interface NewsFeedResponse {
  articles: NewsArticle[];
  count: number;
}

export interface MarketNewsResponse {
  article: NewsArticle | null;
  cached: boolean;
}

export interface GenerateNewsPayload {
  condition_id: string;
  question: string;
  force?: boolean;
  provider?: string;
  model?: string;
}

// ── API Methods ────────────────────────────────────────────

export async function getNewsFeed(limit = 20): Promise<NewsFeedResponse> {
  return apiClient.get<NewsFeedResponse>(`/api/news/feed?limit=${limit}`);
}

export async function getMarketNews(conditionId: string): Promise<MarketNewsResponse> {
  return apiClient.get<MarketNewsResponse>(`/api/news/market/${conditionId}`);
}

export async function generateForMarket(
  payload: GenerateNewsPayload,
): Promise<{ article: NewsArticle; cached: boolean }> {
  return apiClient.post(`/api/news/generate`, payload);
}

export async function refreshFeed(maxMarkets = 5): Promise<NewsFeedResponse> {
  return apiClient.post(`/api/news/refresh-feed`, {
    max_markets: maxMarkets,
  });
}

/**
 * SSE streaming generation – returns an AbortController so the caller can
 * cancel the stream and a Promise that resolves when the stream completes.
 */
export function streamGenerate(
  payload: GenerateNewsPayload,
  callbacks: {
    onStatus?: (msg: string) => void;
    onArticle?: (article: NewsArticle) => void;
    onError?: (msg: string) => void;
    onDone?: () => void;
  },
): { abort: AbortController; done: Promise<void> } {
  const abort = new AbortController();

  const done = (async () => {
    let resp: Response;
    try {
      resp = await fetch(`${API_BASE_URL}/api/news/generate/stream`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        credentials: "include",
        body: JSON.stringify(payload),
        signal: abort.signal,
      });
    } catch (err) {
      // A network failure must surface through onError, not an
      // unhandled rejection of `done`.
      if ((err as Error)?.name !== "AbortError") {
        callbacks.onError?.(err instanceof Error ? err.message : String(err));
      }
      // onDone must still fire so callers can clear their busy state.
      callbacks.onDone?.();
      return;
    }

    if (!resp.ok || !resp.body) {
      callbacks.onError?.(`HTTP ${resp.status}`);
      // onDone must still fire so callers can clear their busy state.
      callbacks.onDone?.();
      return;
    }

    // This endpoint uses the named SSE dialect (`event: <name>` followed by a
    // `data:` line) rather than the JSON envelope the analysis endpoints use,
    // so payloads are dispatched here while line buffering is shared.
    let eventType = "";
    try {
      await streamLines(resp, (lines) => {
        for (const line of lines) {
          if (line.startsWith("event: ")) {
            eventType = line.slice(7).trim();
          } else if (line.startsWith("data: ") && eventType) {
            let data: ({ message?: string } & Partial<NewsArticle>) | null = null;
            try {
              data = JSON.parse(line.slice(6));
            } catch {
              eventType = "";
              continue;
            }

            if (eventType === "status") callbacks.onStatus?.(data?.message ?? "");
            else if (eventType === "article") callbacks.onArticle?.(data as NewsArticle);
            else if (eventType === "error") callbacks.onError?.(data?.message ?? "");
            else if (eventType === "done") {
              callbacks.onDone?.();
              return true;
            }
            eventType = "";
          }
        }
        return false;
      });
    } catch (err) {
      if ((err as Error)?.name !== "AbortError") {
        callbacks.onError?.(err instanceof Error ? err.message : String(err));
      }
      callbacks.onDone?.();
      return;
    }

    callbacks.onDone?.();
  })();

  return { abort, done };
}
