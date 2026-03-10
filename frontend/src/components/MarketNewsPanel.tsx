import { useEffect, useState } from "react";
import {
  getMarketNews,
  generateForMarket,
  type NewsArticle,
} from "../services/newsService";

interface MarketNewsPanelProps {
  conditionId: string;
  question?: string;
}

export default function MarketNewsPanel({
  conditionId,
  question = "",
}: MarketNewsPanelProps) {
  const [article, setArticle] = useState<NewsArticle | null>(null);
  const [loading, setLoading] = useState(true);
  const [generating, setGenerating] = useState(false);
  const [expanded, setExpanded] = useState(false);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    getMarketNews(conditionId)
      .then((res) => {
        if (!cancelled) setArticle(res.article);
      })
      .catch(() => {})
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [conditionId]);

  const handleGenerate = async () => {
    setGenerating(true);
    try {
      const res = await generateForMarket({
        condition_id: conditionId,
        question,
        force: true,
      });
      setArticle(res.article);
    } catch (err) {
      console.error("News generation failed", err);
    } finally {
      setGenerating(false);
    }
  };

  if (loading) {
    return (
      <div className="surface-panel p-3 rounded-lg text-xs text-muted animate-pulse">
        Loading news…
      </div>
    );
  }

  if (!article) {
    return (
      <div className="surface-panel p-3 rounded-lg">
        <div className="flex items-center justify-between">
          <span className="text-xs text-muted">No news article yet</span>
          <button
            onClick={handleGenerate}
            disabled={generating || !question}
            className="btn-primary text-[11px] px-2.5 py-1 disabled:opacity-50"
          >
            {generating ? "Generating…" : "📰 Generate News"}
          </button>
        </div>
      </div>
    );
  }

  const sentimentColor: Record<string, string> = {
    bullish: "text-green-400",
    bearish: "text-red-400",
    neutral: "text-gray-400",
    mixed: "text-yellow-400",
  };

  return (
    <div className="surface-panel p-4 rounded-lg space-y-2">
      <div className="flex items-start justify-between gap-2">
        <h4 className="text-sm font-semibold text-heading leading-snug">
          {article.headline}
        </h4>
        <span
          className={`text-[10px] font-medium whitespace-nowrap ${sentimentColor[article.sentiment?.toLowerCase()] ?? "text-gray-400"}`}
        >
          {article.sentiment} • {Math.round(article.confidence * 100)}%
        </span>
      </div>

      <p className="text-xs text-soft">{article.summary}</p>

      {expanded && (
        <div className="text-xs text-soft whitespace-pre-line mt-2 space-y-3">
          <p>{article.body}</p>

          {article.key_insights?.length > 0 && (
            <div>
              <span className="font-semibold text-heading">Key Insights:</span>
              <ul className="list-disc list-inside mt-1 space-y-0.5">
                {article.key_insights.map((ins, i) => (
                  <li key={i}>{ins}</li>
                ))}
              </ul>
            </div>
          )}

          {article.trader_behavior_summary && (
            <div>
              <span className="font-semibold text-heading">Trader Behavior:</span>
              <p className="mt-0.5">{article.trader_behavior_summary}</p>
            </div>
          )}

          {article.market_outlook && (
            <div>
              <span className="font-semibold text-heading">Market Outlook:</span>
              <p className="mt-0.5">{article.market_outlook}</p>
            </div>
          )}
        </div>
      )}

      <div className="flex items-center justify-between pt-1">
        <button
          onClick={() => setExpanded(!expanded)}
          className="text-[11px] text-accent hover:underline"
        >
          {expanded ? "Collapse" : "Read more"}
        </button>
        <div className="flex items-center gap-2">
          <span className="text-[10px] text-muted">
            {new Date(article.generated_at).toLocaleTimeString()}
          </span>
          <button
            onClick={handleGenerate}
            disabled={generating}
            className="text-[10px] text-muted hover:text-accent disabled:opacity-50"
          >
            {generating ? "…" : "↻"}
          </button>
        </div>
      </div>
    </div>
  );
}
