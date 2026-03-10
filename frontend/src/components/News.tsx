import { useEffect, useState, useCallback } from "react";
import {
  getNewsFeed,
  generateForMarket,
  refreshFeed,
  streamGenerate,
  type NewsArticle,
  type GenerateNewsPayload,
} from "../services/newsService";

// ── Helpers ────────────────────────────────────────────────

function SentimentBadge({ sentiment }: { sentiment: string }) {
  const colors: Record<string, string> = {
    bullish: "bg-green-500/20 text-green-400",
    bearish: "bg-red-500/20 text-red-400",
    neutral: "bg-gray-500/20 text-gray-400",
    mixed: "bg-yellow-500/20 text-yellow-400",
  };
  const cls = colors[sentiment?.toLowerCase()] ?? colors.neutral;
  return (
    <span className={`text-xs font-medium px-2 py-0.5 rounded-full ${cls}`}>
      {sentiment}
    </span>
  );
}

function ConfidenceMeter({ value }: { value: number }) {
  const pct = Math.round(value * 100);
  const color = pct >= 70 ? "bg-green-500" : pct >= 40 ? "bg-yellow-500" : "bg-red-500";
  return (
    <div className="flex items-center gap-2 text-xs text-soft">
      <div className="w-16 h-1.5 bg-white/10 rounded-full overflow-hidden">
        <div className={`h-full rounded-full ${color}`} style={{ width: `${pct}%` }} />
      </div>
      <span>{pct}%</span>
    </div>
  );
}

// ── News Card ──────────────────────────────────────────────

function NewsCard({
  article,
  onExpand,
}: {
  article: NewsArticle;
  onExpand: () => void;
}) {
  return (
    <div
      onClick={onExpand}
      className="surface-panel p-4 rounded-lg cursor-pointer hover:ring-1 hover:ring-accent/40 transition"
    >
      <div className="flex items-start justify-between gap-2 mb-2">
        <h3 className="text-sm font-semibold text-heading leading-snug line-clamp-2">
          {article.headline}
        </h3>
        <SentimentBadge sentiment={article.sentiment} />
      </div>

      <p className="text-xs text-soft line-clamp-3 mb-3">{article.summary}</p>

      <div className="flex items-center justify-between">
        <ConfidenceMeter value={article.confidence} />
        <span className="text-[10px] text-muted">
          {new Date(article.generated_at).toLocaleTimeString()}
        </span>
      </div>

      {article.tags?.length > 0 && (
        <div className="flex flex-wrap gap-1 mt-2">
          {article.tags.slice(0, 4).map((t) => (
            <span key={t} className="text-[10px] px-1.5 py-0.5 rounded bg-white/5 text-muted">
              {t}
            </span>
          ))}
        </div>
      )}
    </div>
  );
}

// ── Article Modal ──────────────────────────────────────────

function ArticleModal({
  article,
  onClose,
}: {
  article: NewsArticle;
  onClose: () => void;
}) {
  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 backdrop-blur-sm px-4"
      onClick={onClose}
    >
      <div
        className="surface-panel max-w-2xl w-full max-h-[85vh] overflow-y-auto rounded-xl p-6 relative"
        onClick={(e) => e.stopPropagation()}
      >
        <button
          onClick={onClose}
          className="absolute top-3 right-3 text-muted hover:text-heading text-lg"
        >
          ×
        </button>

        <div className="flex items-center gap-2 mb-1">
          <SentimentBadge sentiment={article.sentiment} />
          <ConfidenceMeter value={article.confidence} />
        </div>

        <h2 className="text-lg font-bold text-heading mt-2 mb-1">
          {article.headline}
        </h2>
        <p className="text-xs text-muted mb-4">
          {article.question} • {new Date(article.generated_at).toLocaleString()}
        </p>

        <p className="text-sm text-soft mb-4 italic">{article.summary}</p>

        <div className="prose prose-invert prose-sm max-w-none text-soft whitespace-pre-line">
          {article.body}
        </div>

        {article.key_insights?.length > 0 && (
          <div className="mt-5">
            <h4 className="text-xs font-semibold text-heading mb-2">Key Insights</h4>
            <ul className="list-disc list-inside space-y-1 text-xs text-soft">
              {article.key_insights.map((ins, i) => (
                <li key={i}>{ins}</li>
              ))}
            </ul>
          </div>
        )}

        {article.trader_behavior_summary && (
          <div className="mt-4">
            <h4 className="text-xs font-semibold text-heading mb-1">Trader Behavior</h4>
            <p className="text-xs text-soft">{article.trader_behavior_summary}</p>
          </div>
        )}

        {article.market_outlook && (
          <div className="mt-4">
            <h4 className="text-xs font-semibold text-heading mb-1">Market Outlook</h4>
            <p className="text-xs text-soft">{article.market_outlook}</p>
          </div>
        )}

        {article.tags?.length > 0 && (
          <div className="flex flex-wrap gap-1 mt-4">
            {article.tags.map((t) => (
              <span key={t} className="text-[10px] px-2 py-0.5 rounded bg-white/5 text-muted">
                {t}
              </span>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}

// ── Main Component ─────────────────────────────────────────

export default function News() {
  const [feed, setFeed] = useState<NewsArticle[]>([]);
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [selectedArticle, setSelectedArticle] = useState<NewsArticle | null>(null);

  // On-demand generation state
  const [genConditionId, setGenConditionId] = useState("");
  const [genQuestion, setGenQuestion] = useState("");
  const [generating, setGenerating] = useState(false);
  const [genStatus, setGenStatus] = useState("");

  const loadFeed = useCallback(async () => {
    try {
      const res = await getNewsFeed(20);
      setFeed(res.articles);
    } catch (err) {
      console.error("Failed to load news feed", err);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    loadFeed();
  }, [loadFeed]);

  const handleRefresh = async () => {
    setRefreshing(true);
    try {
      const res = await refreshFeed(5);
      setFeed(res.articles);
    } catch (err) {
      console.error("Feed refresh failed", err);
    } finally {
      setRefreshing(false);
    }
  };

  const handleGenerate = () => {
    if (!genConditionId.trim() || !genQuestion.trim()) return;
    setGenerating(true);
    setGenStatus("Starting generation…");

    const payload: GenerateNewsPayload = {
      condition_id: genConditionId.trim(),
      question: genQuestion.trim(),
      force: true,
    };

    const { done } = streamGenerate(payload, {
      onStatus: (msg) => setGenStatus(msg),
      onArticle: (article) => {
        setFeed((prev) => [article, ...prev.filter((a) => a.condition_id !== article.condition_id)]);
        setGenStatus("Article generated!");
      },
      onError: (msg) => setGenStatus(`Error: ${msg}`),
      onDone: () => {
        setGenerating(false);
        setGenConditionId("");
        setGenQuestion("");
        setTimeout(() => setGenStatus(""), 3000);
      },
    });

    done.catch(() => setGenerating(false));
  };

  return (
    <div className="space-y-6">
      {/* Header */}
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-xl font-bold text-heading">AI Market News</h1>
          <p className="text-xs text-muted mt-0.5">
            AI-generated articles from market data, trader behavior &amp; social signals
          </p>
        </div>
        <button
          onClick={handleRefresh}
          disabled={refreshing}
          className="btn-primary text-xs px-3 py-1.5 disabled:opacity-50"
        >
          {refreshing ? "Refreshing…" : "↻ Refresh Feed"}
        </button>
      </div>

      {/* On-demand generator */}
      <div className="surface-panel p-4 rounded-lg">
        <h2 className="text-sm font-semibold text-heading mb-3">Generate Article</h2>
        <div className="flex flex-col sm:flex-row gap-2">
          <input
            type="text"
            placeholder="Condition ID"
            value={genConditionId}
            onChange={(e) => setGenConditionId(e.target.value)}
            className="input-field flex-1 text-xs"
          />
          <input
            type="text"
            placeholder="Market question"
            value={genQuestion}
            onChange={(e) => setGenQuestion(e.target.value)}
            className="input-field flex-[2] text-xs"
          />
          <button
            onClick={handleGenerate}
            disabled={generating || !genConditionId.trim() || !genQuestion.trim()}
            className="btn-primary text-xs px-4 py-1.5 whitespace-nowrap disabled:opacity-50"
          >
            {generating ? "Generating…" : "Generate"}
          </button>
        </div>
        {genStatus && (
          <p className="text-xs text-muted mt-2">{genStatus}</p>
        )}
      </div>

      {/* Feed grid */}
      {loading ? (
        <div className="text-center py-12 text-muted text-sm">Loading news feed…</div>
      ) : feed.length === 0 ? (
        <div className="text-center py-12">
          <p className="text-muted text-sm">No articles yet.</p>
          <p className="text-xs text-muted mt-1">
            Click &quot;Refresh Feed&quot; to generate articles for trending markets.
          </p>
        </div>
      ) : (
        <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
          {feed.map((article) => (
            <NewsCard
              key={`${article.condition_id}-${article.generated_at}`}
              article={article}
              onExpand={() => setSelectedArticle(article)}
            />
          ))}
        </div>
      )}

      {/* Modal */}
      {selectedArticle && (
        <ArticleModal
          article={selectedArticle}
          onClose={() => setSelectedArticle(null)}
        />
      )}
    </div>
  );
}
