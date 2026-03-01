import { useEffect, useRef, useState } from "react";
import { useSearchParams } from "react-router-dom";
import AnalysisDisplay from "../components/AnalysisDisplay";
import { analysisService } from "../services/analysisService";
import { sanitizeAIError } from "../utils/apiError";

export default function AnalysisPage() {
  const [searchParams] = useSearchParams();

  const [streamedText, setStreamedText] = useState("");
  const [streamDone, setStreamDone] = useState(false);
  const [streamError, setStreamError] = useState<string | null>(null);
  const abortRef = useRef<AbortController | null>(null);

  // Extract market params from URL
  const marketTitle = searchParams.get("market_title") || "AI Analysis";
  const marketDescription =
    searchParams.get("market_description") || marketTitle;
  const yesPrice = parseFloat(searchParams.get("yes_price") || "0.5");
  const noPrice = parseFloat(searchParams.get("no_price") || "0.5");
  const volume24h = parseFloat(searchParams.get("volume_24h") || "0");
  const endDate = searchParams.get("end_date") || new Date().toISOString();
  const includeResearch = searchParams.get("include_research") !== "false";

  // Start independent analysis on mount
  useEffect(() => {
    const controller = analysisService.streamMarketAnalysis(
      {
        market_title: marketTitle,
        market_description: marketDescription,
        yes_price: yesPrice,
        no_price: noPrice,
        volume_24h: volume24h,
        end_date: endDate,
        include_research: includeResearch,
      },
      {
        onChunk: (text) => setStreamedText((prev) => prev + text),
        onDone: () => setStreamDone(true),
        onError: (err) => {
          setStreamError(sanitizeAIError(err));
          setStreamDone(true);
        },
      },
    );

    abortRef.current = controller;

    return () => {
      controller.abort();
    };
    // Run once on mount only
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const yesCents = Math.round(yesPrice * 100);
  const noCents = Math.round(noPrice * 100);

  return (
    <div className="max-w-4xl mx-auto">
      {/* Market Header */}
      <div className="surface-panel p-6 mb-6">
        <h1
          className="text-xl font-bold mb-3"
          style={{ color: "var(--text-primary)" }}
        >
          {marketTitle}
        </h1>
        <div className="flex flex-wrap gap-3 items-center text-sm">
          <span className="chip chip-success">YES {yesCents}¢</span>
          <span className="chip chip-danger">NO {noCents}¢</span>
          {volume24h > 0 && (
            <span className="chip chip-accent">
              Vol ${volume24h.toLocaleString()}
            </span>
          )}
        </div>
      </div>

      {/* Analysis Content */}
      <div className="surface-panel p-6">
        {streamError && (
          <div
            className="mb-4 p-3 rounded-lg text-sm"
            style={{
              background: "rgba(255,99,99,.12)",
              color: "var(--danger)",
              border: "1px solid rgba(255,99,99,.25)",
            }}
          >
            Analysis error: {streamError}
          </div>
        )}

        {!streamedText && !streamDone && !streamError && (
          <div className="flex items-center gap-3 py-8 justify-center">
            <div className="analysis-spinner" />
            <span
              className="text-sm font-medium"
              style={{ color: "var(--text-secondary)" }}
            >
              Running independent analysis…
            </span>
          </div>
        )}

        {streamedText && (
          <AnalysisDisplay text={streamedText} streaming={!streamDone} />
        )}

        {streamDone && !streamError && streamedText && (
          <div
            className="mt-4 text-center text-xs"
            style={{ color: "var(--text-muted)" }}
          >
            Analysis complete
          </div>
        )}
      </div>
    </div>
  );
}
