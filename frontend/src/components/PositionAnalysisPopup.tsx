/**
 * Click-anchored popup that streams an AI verdict for a single open position.
 *
 * Builds a JSON-only prompt from the position and calls analysisService.streamMarketAnalysis,
 * falling back to the non-streaming analyzeMarket when the stream errors. The parsed
 * result fills a fixed label/icon table; outside clicks and Escape close it.
 *
 * @module components/PositionAnalysisPopup
 */
import { useState, useEffect, useRef, useCallback } from "react";
import { analysisService } from "../services/analysisService";

export interface PositionForAnalysis {
  title?: string;
  market?: string;
  asset?: string;
  asset_id?: string;
  condition_id?: string;
  conditionId?: string;
  outcome?: string;
  size: number;
  avgPrice?: number;
  curPrice?: number;
  pnl?: number;
  marketSlug?: string;
}

interface AnalysisResult {
  verdict: string;
  confidence: string;
  volume_signal: string;
  sentiment: string;
  news_summary: string;
  risk_level: string;
  recommendation: string;
  key_factors: string;
  price_target: string;
  time_horizon: string;
}

interface Props {
  position: PositionForAnalysis;
  /**
   * Live-updating position from the dashboard's price stream (same asset_id).
   * When supplied, the summary bar's current price and P&L refresh on every
   * price tick while the popup stays open.
   */
  livePosition?: PositionForAnalysis | null;
  clickX: number;
  clickY: number;
  onClose: () => void;
}

/**
 * Scan text for the first balanced `{...}` span that parses as a JSON object.
 *
 * A greedy `/\{[\s\S]*\}/` match breaks when prose contains braces or when
 * several JSON objects arrive in one stream, so walk brace depth (honouring
 * string literals) and try `JSON.parse` on each balanced candidate until one
 * succeeds.
 */
export function extractFirstJsonObject(text: string): Record<string, unknown> | null {
  let depth = 0;
  let start = -1;
  let inString = false;
  let escaped = false;

  for (let i = 0; i < text.length; i += 1) {
    const ch = text[i];

    if (inString) {
      if (escaped) {
        escaped = false;
      } else if (ch === "\\") {
        escaped = true;
      } else if (ch === '"') {
        inString = false;
      }
      continue;
    }

    if (ch === '"') {
      inString = true;
      escaped = false;
    } else if (ch === "{") {
      if (depth === 0) start = i;
      depth += 1;
    } else if (ch === "}") {
      if (depth === 0) continue;
      depth -= 1;
      if (depth === 0 && start >= 0) {
        const candidate = text.slice(start, i + 1);
        try {
          const parsed: unknown = JSON.parse(candidate);
          if (typeof parsed === "object" && parsed !== null && !Array.isArray(parsed)) {
            return parsed as Record<string, unknown>;
          }
        } catch {
          // Not valid JSON — keep scanning for the next balanced span.
        }
        start = -1;
      }
    }
  }

  return null;
}

export default function PositionAnalysisPopup({
  position,
  livePosition,
  clickX,
  clickY,
  onClose,
}: Props) {
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [result, setResult] = useState<AnalysisResult | null>(null);
  const [streamedText, setStreamedText] = useState("");
  const popupRef = useRef<HTMLDivElement>(null);
  const abortRef = useRef<AbortController | null>(null);
  // Accumulated stream text — the source of truth for onDone parsing, so
  // setResult never runs inside a setState updater.
  const accumulatedRef = useRef("");
  // Guards the non-streaming fallback so a failing stream triggers it once.
  const hasFallBackRef = useRef(false);
  // Set once a result exists — a late stream error must not overwrite it or
  // re-trigger the fallback.
  const hasResultRef = useRef(false);
  // Set by the unmount/dep-change cleanup so an aborted stream never
  // triggers the non-streaming fallback.
  const abortedRef = useRef(false);

  const title = position.title || position.market || position.asset || "Unknown Position";
  // Snapshot values captured at click time — they drive the analysis request
  // and stay stable so live price ticks never re-trigger the LLM call.
  const avgPrice = Number(position.avgPrice || 0);
  const curPrice = Number(position.curPrice || 0);
  const size = Number(position.size || 0);
  const pnl = Number(position.pnl || 0);
  // Live display values — refresh on every price tick while the popup is open.
  const displayPosition = livePosition ?? position;
  const displayAvgPrice =
    displayPosition.avgPrice != null ? Number(displayPosition.avgPrice) : avgPrice;
  const displayCurPrice =
    displayPosition.curPrice != null ? Number(displayPosition.curPrice) : curPrice;
  const displayPnl = displayPosition.pnl != null ? Number(displayPosition.pnl) : pnl;

  const parseAnalysis = useCallback((text: string): AnalysisResult | null => {
    const parsed = extractFirstJsonObject(text);
    if (parsed) {
      return {
        verdict: String(parsed.verdict || parsed.recommendation || parsed.decision || "—"),
        confidence: String(parsed.confidence || "—"),
        volume_signal: String(parsed.volume_signal || parsed.volume || "—"),
        sentiment: String(parsed.sentiment || parsed.market_sentiment || "—"),
        news_summary: String(parsed.news_summary || parsed.news || parsed.news_context || "—"),
        risk_level: String(parsed.risk_level || parsed.risk || "—"),
        recommendation: String(parsed.recommendation || parsed.action || "—"),
        key_factors: String(parsed.key_factors || parsed.factors || "—"),
        price_target: String(parsed.price_target || parsed.target || "—"),
        time_horizon: String(parsed.time_horizon || parsed.horizon || "—"),
      };
    }
    return null;
  }, []);

  const runAnalysis = useCallback(async () => {
    setLoading(true);
    setError(null);
    setResult(null);
    setStreamedText("");
    accumulatedRef.current = "";
    hasFallBackRef.current = false;
    hasResultRef.current = false;
    abortedRef.current = false;

    // Distinguish "unknown price" (assume a 50/50 market) from a known
    // zero price (collapsed market) — `curPrice || 0.5` misrepresented
    // legitimate zeros as 50/50.
    const yesPrice = position.curPrice != null ? Number(position.curPrice) : 0.5;
    const noPrice = position.curPrice != null ? 1 - Number(position.curPrice) : 0.5;

    const prompt = [
      "You are a senior prediction market analyst. Analyze this open position based on volume, sentiment, and news.",
      "Return only valid JSON with exactly this schema (no markdown, no bullet points):",
      '{"verdict":"", "confidence":"", "volume_signal":"", "sentiment":"", "news_summary":"", "risk_level":"", "recommendation":"", "key_factors":"", "price_target":"", "time_horizon":""}',
      "",
      `Market: ${title}`,
      `Held Outcome: ${position.outcome || "Unknown"}`,
      `Entry Price: ${(avgPrice * 100).toFixed(1)}¢`,
      `Current Price: ${(curPrice * 100).toFixed(1)}¢`,
      `Position Size: ${size} shares`,
      `Current P&L: $${pnl.toFixed(2)}`,
      "",
      "Analyze volume trends, market sentiment, and related news to provide actionable insight.",
      "verdict: your overall assessment (bullish / bearish / neutral)",
      "confidence: your confidence level (e.g. 75%)",
      "volume_signal: what trading volume suggests",
      "sentiment: overall market sentiment for this position",
      "news_summary: key recent news affecting this market",
      "risk_level: low / medium / high",
      "recommendation: hold / add / reduce / exit with brief reasoning",
      "key_factors: the 2-3 most important factors",
      "price_target: expected price direction",
      "time_horizon: short / medium / long term outlook",
    ].join("\n");

    const request = {
      market_title: title,
      market_description: prompt,
      yes_price: yesPrice,
      no_price: noPrice,
      volume_24h: 0,
      end_date: new Date(Date.now() + 30 * 86400_000).toISOString(),
      include_research: true,
    };

    const applyResult = (text: string) => {
      hasResultRef.current = true;
      const parsed = parseAnalysis(text);
      if (parsed) {
        setResult(parsed);
      } else {
        // Fallback: show raw text in a structured way
        setResult({
          verdict: "See analysis below",
          confidence: "—",
          volume_signal: "—",
          sentiment: "—",
          news_summary: text.slice(0, 300) || "No analysis returned",
          risk_level: "—",
          recommendation: "—",
          key_factors: "—",
          price_target: "—",
          time_horizon: "—",
        });
      }
    };

    // Non-streaming fallback. streamMarketAnalysis never throws
    // synchronously — it routes every transport/HTTP/backend failure to
    // onError — so this path is triggered from onError, not a catch block.
    const runNonStreamingFallback = async () => {
      try {
        const resp = await analysisService.analyzeMarket(request);
        const text = String(resp?.data?.analysis || JSON.stringify(resp?.data || {}));
        applyResult(text);
      } catch (fallbackErr: unknown) {
        setError(fallbackErr instanceof Error ? fallbackErr.message : "Analysis failed");
      }
      setLoading(false);
    };

    try {
      // Use streaming analysis for real-time feedback
      const controller = analysisService.streamMarketAnalysis(request, {
        onChunk: (chunk: string) => {
          accumulatedRef.current += chunk;
          setStreamedText(accumulatedRef.current);
        },
        onDone: () => {
          applyResult(accumulatedRef.current);
          setLoading(false);
        },
        onError: (err: string) => {
          if (abortedRef.current || hasResultRef.current) return;
          if (!hasFallBackRef.current) {
            hasFallBackRef.current = true;
            void runNonStreamingFallback();
            return;
          }
          setError(err || "Analysis failed");
          setLoading(false);
        },
      });
      abortRef.current = controller;
    } catch (err: unknown) {
      // Safety net — streamMarketAnalysis does not throw synchronously today.
      if (!hasFallBackRef.current) {
        hasFallBackRef.current = true;
        await runNonStreamingFallback();
      } else {
        setError(err instanceof Error ? err.message : "Analysis failed");
        setLoading(false);
      }
    }
  }, [title, position.outcome, position.curPrice, avgPrice, curPrice, size, pnl, parseAnalysis]);

  useEffect(() => {
    runAnalysis();
    return () => {
      abortedRef.current = true;
      abortRef.current?.abort();
    };
  }, [runAnalysis]);

  // Position the popup near the click, but keep it within viewport
  useEffect(() => {
    if (!popupRef.current) return;
    const popup = popupRef.current;
    const rect = popup.getBoundingClientRect();
    const vw = window.innerWidth;
    const vh = window.innerHeight;

    let left = clickX;
    let top = clickY + 8;

    // Keep within horizontal bounds
    if (left + rect.width > vw - 16) {
      left = vw - rect.width - 16;
    }
    if (left < 16) left = 16;

    // Keep within vertical bounds
    if (top + rect.height > vh - 16) {
      top = clickY - rect.height - 8;
    }
    if (top < 16) top = 16;

    popup.style.left = `${left}px`;
    popup.style.top = `${top}px`;
  }, [clickX, clickY, loading, result, error]);

  // Close on outside click
  useEffect(() => {
    const handleClick = (e: MouseEvent) => {
      if (popupRef.current && !popupRef.current.contains(e.target as Node)) {
        onClose();
      }
    };
    const handleEsc = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    // Delay adding listener so the triggering click doesn't immediately close
    const timer = setTimeout(() => {
      document.addEventListener("mousedown", handleClick);
      document.addEventListener("keydown", handleEsc);
    }, 100);
    return () => {
      clearTimeout(timer);
      document.removeEventListener("mousedown", handleClick);
      document.removeEventListener("keydown", handleEsc);
    };
  }, [onClose]);

  const labelMap: Record<keyof AnalysisResult, string> = {
    verdict: "Verdict",
    confidence: "Confidence",
    volume_signal: "Volume Signal",
    sentiment: "Sentiment",
    news_summary: "News Summary",
    risk_level: "Risk Level",
    recommendation: "Recommendation",
    key_factors: "Key Factors",
    price_target: "Price Target",
    time_horizon: "Time Horizon",
  };

  const iconMap: Record<keyof AnalysisResult, string> = {
    verdict: "📊",
    confidence: "🎯",
    volume_signal: "📈",
    sentiment: "💬",
    news_summary: "📰",
    risk_level: "⚠️",
    recommendation: "💡",
    key_factors: "🔑",
    price_target: "🎯",
    time_horizon: "⏱️",
  };

  const getValueColor = (key: string, value: string): string => {
    const lower = value.toLowerCase();
    if (key === "verdict" || key === "sentiment") {
      if (lower.includes("bullish") || lower.includes("positive")) return "text-emerald-400";
      if (lower.includes("bearish") || lower.includes("negative")) return "text-red-400";
    }
    if (key === "risk_level") {
      if (lower.includes("low")) return "text-emerald-400";
      if (lower.includes("high")) return "text-red-400";
      if (lower.includes("medium")) return "text-yellow-400";
    }
    if (key === "recommendation") {
      if (lower.includes("add") || lower.includes("buy")) return "text-emerald-400";
      if (lower.includes("exit") || lower.includes("sell")) return "text-red-400";
      if (lower.includes("reduce")) return "text-yellow-400";
    }
    return "text-soft";
  };

  return (
    <div
      ref={popupRef}
      className="fixed z-50 surface-panel shadow-2xl overflow-hidden"
      style={{
        left: clickX,
        top: clickY + 8,
        width: "min(480px, calc(100vw - 32px))",
        maxHeight: "min(600px, calc(100vh - 32px))",
      }}
    >
      {/* Header */}
      <div className="px-4 py-3 border-b border-[var(--line)] bg-[var(--bg-soft)] flex items-center justify-between gap-2">
        <div className="flex items-center gap-2 min-w-0">
          <span className="text-lg">🤖</span>
          <div className="min-w-0">
            <h3 className="text-sm font-bold text-white truncate">AI Position Analysis</h3>
            <p className="text-[11px] text-muted truncate">{title}</p>
          </div>
        </div>
        <button
          onClick={onClose}
          className="text-muted hover:text-white transition text-lg leading-none flex-shrink-0 w-6 h-6 flex items-center justify-center rounded hover:bg-[var(--bg)]"
        >
          ✕
        </button>
      </div>

      {/* Position Summary Bar — live values while the popup is open */}
      <div className="px-4 py-2 border-b border-[var(--line)] flex items-center gap-4 text-[11px]">
        {position.outcome && <span className="chip chip-accent">{position.outcome}</span>}
        <span className="text-muted">
          Entry: <span className="text-soft">{(displayAvgPrice * 100).toFixed(1)}¢</span>
        </span>
        <span className="text-muted">
          Now: <span className="text-soft">{(displayCurPrice * 100).toFixed(1)}¢</span>
        </span>
        <span className={displayPnl >= 0 ? "status-good" : "status-bad"}>
          {displayPnl >= 0 ? "+" : ""}${displayPnl.toFixed(2)}
        </span>
      </div>

      {/* Body */}
      <div
        className="overflow-y-auto scroll-soft"
        style={{ maxHeight: "min(480px, calc(100vh - 160px))" }}
      >
        {loading && (
          <div className="p-6 flex flex-col items-center gap-3">
            <div className="relative w-10 h-10">
              <span className="absolute inset-0 rounded-full border-2 border-[var(--accent)]/30" />
              <span className="absolute inset-0 rounded-full border-2 border-[var(--accent)] border-t-transparent animate-spin" />
            </div>
            <p className="text-sm text-soft">Analyzing position...</p>
            {streamedText && (
              <p className="text-[11px] text-muted text-center max-w-[90%] line-clamp-3 mt-1">
                {streamedText.slice(-200)}
              </p>
            )}
          </div>
        )}

        {error && !loading && (
          <div className="p-4">
            <div className="p-3 rounded-lg bg-red-500/10 border border-red-500/30 text-red-400 text-sm">
              {error}
            </div>
            <div className="mt-3 flex gap-2">
              <button onClick={runAnalysis} className="flex-1 btn-accent py-2 text-sm">
                Retry
              </button>
              <button onClick={onClose} className="flex-1 btn-muted py-2 text-sm">
                Close
              </button>
            </div>
          </div>
        )}

        {result && !loading && (
          <div className="p-1">
            <table className="w-full text-[12px]">
              <tbody>
                {(Object.keys(labelMap) as Array<keyof AnalysisResult>).map((key) => {
                  const value = result[key];
                  if (!value || value === "—") return null;
                  return (
                    <tr
                      key={key}
                      className="border-b border-[var(--line)]/50 last:border-0 hover:bg-[var(--bg-soft)]/50 transition"
                    >
                      <td className="px-3 py-2.5 text-muted whitespace-nowrap align-top w-[140px]">
                        <span className="mr-1.5">{iconMap[key]}</span>
                        {labelMap[key]}
                      </td>
                      <td className={`px-3 py-2.5 ${getValueColor(key, value)} leading-relaxed`}>
                        {value}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
      </div>

      {/* Footer */}
      {result && !loading && (
        <div className="px-4 py-2.5 border-t border-[var(--line)] bg-[var(--bg-soft)] flex items-center justify-between">
          <p className="text-[10px] text-muted">AI analysis — not financial advice</p>
          <button onClick={onClose} className="btn-muted text-xs px-3 py-1">
            Close
          </button>
        </div>
      )}
    </div>
  );
}
