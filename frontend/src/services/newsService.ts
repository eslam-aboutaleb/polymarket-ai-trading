import { apiClient } from "./apiClient";
import { API_BASE_URL } from "../config/api";

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
  return apiClient.get<NewsFeedResponse>(
    `/api/news/feed?limit=${limit}`,
  );
}

export async function getMarketNews(
  conditionId: string,
): Promise<MarketNewsResponse> {
  return apiClient.get<MarketNewsResponse>(
    `/api/news/market/${conditionId}`,
  );
}

export async function generateForMarket(
  payload: GenerateNewsPayload,
): Promise<{ article: NewsArticle; cached: boolean }> {
  return apiClient.post(`/api/news/generate`, payload);
}

export async function refreshFeed(
  maxMarkets = 5,
): Promise<NewsFeedResponse> {
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
    const resp = await fetch(`${API_BASE_URL}/api/news/generate/stream`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      credentials: "include",
      body: JSON.stringify(payload),
      signal: abort.signal,
    });

    if (!resp.ok || !resp.body) {
      callbacks.onError?.(`HTTP ${resp.status}`);
      return;
    }

    const reader = resp.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";

    // eslint-disable-next-line no-constant-condition
    while (true) {
      const { done: streamDone, value } = await reader.read();
      if (streamDone) break;

      buffer += decoder.decode(value, { stream: true });
      const lines = buffer.split("\n");
      buffer = lines.pop() || "";

      let eventType = "";
      for (const line of lines) {
        if (line.startsWith("event: ")) {
          eventType = line.slice(7).trim();
        } else if (line.startsWith("data: ") && eventType) {
          try {
            const data = JSON.parse(line.slice(6));
            if (eventType === "status") callbacks.onStatus?.(data.message);
            else if (eventType === "article") callbacks.onArticle?.(data);
            else if (eventType === "error") callbacks.onError?.(data.message);
            else if (eventType === "done") callbacks.onDone?.();
          } catch {
            /* skip unparseable lines */
          }
          eventType = "";
        }
      }
    }
  })();

  return { abort, done };
}
