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
  clickX: number;
  clickY: number;
  onClose: () => void;
}

export default function PositionAnalysisPopup({
  position,
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

  const title =
    position.title || position.market || position.asset || "Unknown Position";
  const curPrice = Number(position.curPrice || 0);
  const avgPrice = Number(position.avgPrice || 0);
  const size = Number(position.size || 0);
  const pnl = Number(position.pnl || 0);

  const parseAnalysis = useCallback((text: string): AnalysisResult | null => {
    // Try to extract JSON from the response
    const jsonMatch = text.match(/\{[\s\S]*\}/);
    if (jsonMatch) {
      try {
        const parsed = JSON.parse(jsonMatch[0]);
        if (typeof parsed === "object" && parsed !== null) {
          return {
            verdict: String(
              parsed.verdict || parsed.recommendation || parsed.decision || "—",
            ),
            confidence: String(parsed.confidence || "—"),
            volume_signal: String(parsed.volume_signal || parsed.volume || "—"),
            sentiment: String(
              parsed.sentiment || parsed.market_sentiment || "—",
            ),
            news_summary: String(
              parsed.news_summary || parsed.news || parsed.news_context || "—",
            ),
            risk_level: String(parsed.risk_level || parsed.risk || "—"),
            recommendation: String(
              parsed.recommendation || parsed.action || "—",
            ),
            key_factors: String(parsed.key_factors || parsed.factors || "—"),
            price_target: String(parsed.price_target || parsed.target || "—"),
            time_horizon: String(parsed.time_horizon || parsed.horizon || "—"),
          };
        }
      } catch {
        // not valid JSON
      }
    }
    return null;
  }, []);

  useEffect(() => {
    const run = async () => {
      setLoading(true);
      setError(null);
      setResult(null);
      setStreamedText("");

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

      try {
        // Use streaming analysis for real-time feedback
        const controller = analysisService.streamMarketAnalysis(
          {
            market_title: title,
            market_description: prompt,
            yes_price: curPrice || 0.5,
            no_price: 1 - (curPrice || 0.5),
            volume_24h: 0,
            end_date: new Date(Date.now() + 30 * 86400_000).toISOString(),
            include_research: true,
          },
          {
            onChunk: (chunk: string) => {
              setStreamedText((prev) => {
                const updated = prev + chunk;
                return updated;
              });
            },
            onDone: () => {
              setStreamedText((prev) => {
                const parsed = parseAnalysis(prev);
                if (parsed) {
                  setResult(parsed);
                } else {
                  // Fallback: show raw text in a structured way
                  setResult({
                    verdict: "See analysis below",
                    confidence: "—",
                    volume_signal: "—",
                    sentiment: "—",
                    news_summary: prev.slice(0, 300) || "No analysis returned",
                    risk_level: "—",
                    recommendation: "—",
                    key_factors: "—",
                    price_target: "—",
                    time_horizon: "—",
                  });
                }
                return prev;
              });
              setLoading(false);
            },
            onError: (err: string) => {
              setError(err || "Analysis failed");
              setLoading(false);
            },
          },
        );
        abortRef.current = controller;
      } catch (err: unknown) {
        // Fallback to non-streaming
        try {
          const resp = await analysisService.analyzeMarket({
            market_title: title,
            market_description: prompt,
            yes_price: curPrice || 0.5,
            no_price: 1 - (curPrice || 0.5),
            volume_24h: 0,
            end_date: new Date(Date.now() + 30 * 86400_000).toISOString(),
            include_research: true,
          });

          const text = String(
            resp?.data?.analysis || JSON.stringify(resp?.data || {}),
          );
          const parsed = parseAnalysis(text);
          if (parsed) {
            setResult(parsed);
          } else {
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
        } catch (fallbackErr: unknown) {
          setError(
            fallbackErr instanceof Error
              ? fallbackErr.message
              : "Analysis failed",
          );
        }
        setLoading(false);
      }
    };

    run();

    return () => {
      abortRef.current?.abort();
    };
  }, [title, position.outcome, avgPrice, curPrice, size, pnl, parseAnalysis]);

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
      if (lower.includes("bullish") || lower.includes("positive"))
        return "text-emerald-400";
      if (lower.includes("bearish") || lower.includes("negative"))
        return "text-red-400";
    }
    if (key === "risk_level") {
      if (lower.includes("low")) return "text-emerald-400";
      if (lower.includes("high")) return "text-red-400";
      if (lower.includes("medium")) return "text-yellow-400";
    }
    if (key === "recommendation") {
      if (lower.includes("add") || lower.includes("buy"))
        return "text-emerald-400";
      if (lower.includes("exit") || lower.includes("sell"))
        return "text-red-400";
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
            <h3 className="text-sm font-bold text-white truncate">
              AI Position Analysis
            </h3>
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

      {/* Position Summary Bar */}
      <div className="px-4 py-2 border-b border-[var(--line)] flex items-center gap-4 text-[11px]">
        {position.outcome && (
          <span className="chip chip-accent">{position.outcome}</span>
        )}
        <span className="text-muted">
          Entry:{" "}
          <span className="text-soft">{(avgPrice * 100).toFixed(1)}¢</span>
        </span>
        <span className="text-muted">
          Now: <span className="text-soft">{(curPrice * 100).toFixed(1)}¢</span>
        </span>
        <span className={pnl >= 0 ? "status-good" : "status-bad"}>
          {pnl >= 0 ? "+" : ""}${pnl.toFixed(2)}
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
            <button
              onClick={onClose}
              className="mt-3 w-full btn-muted py-2 text-sm"
            >
              Close
            </button>
          </div>
        )}

        {result && !loading && (
          <div className="p-1">
            <table className="w-full text-[12px]">
              <tbody>
                {(Object.keys(labelMap) as Array<keyof AnalysisResult>).map(
                  (key) => {
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
                        <td
                          className={`px-3 py-2.5 ${getValueColor(key, value)} leading-relaxed`}
                        >
                          {value}
                        </td>
                      </tr>
                    );
                  },
                )}
              </tbody>
            </table>
          </div>
        )}
      </div>

      {/* Footer */}
      {result && !loading && (
        <div className="px-4 py-2.5 border-t border-[var(--line)] bg-[var(--bg-soft)] flex items-center justify-between">
          <p className="text-[10px] text-muted">
            AI analysis — not financial advice
          </p>
          <button onClick={onClose} className="btn-muted text-xs px-3 py-1">
            Close
          </button>
        </div>
      )}
    </div>
  );
}
