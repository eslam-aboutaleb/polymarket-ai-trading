/**
 * Renders streamed AI trader-analysis JSON as a styled vertical label/value table.
 *
 * Strips code fences and extracts the JSON object from the raw text, mapping known keys to icons
 * and labels, rendering "x/10" ratings as bars and array fields as bullet lists. While streaming
 * it shows the trailing text with a cursor, and unparseable output falls back to raw text.
 *
 * @module components/TraderAnalysisTable
 */
import { useMemo } from "react";

/* ───────── Types ───────── */

interface TraderAnalysisJSON {
  trader_overview?: string;
  trading_patterns?: string;
  market_preferences?: string;
  strategy_classification?: string;
  position_sizing?: string;
  risk_management?: string;
  win_rate_analysis?: string;
  consistency_assessment?: string;
  copy_worthiness_rating?: string;
  copy_worthiness_justification?: string;
  key_strengths?: string[];
  specific_risks?: string[];
  recommendation?: string;
  [key: string]: unknown;
}

interface TraderAnalysisTableProps {
  /** Raw text from the streaming AI response */
  text: string;
  /** True while streaming is still in progress */
  streaming?: boolean;
}

/* ───────── Label + icon mapping for each JSON field ───────── */

const FIELD_CONFIG: Record<
  string,
  { label: string; icon: string; isArray?: boolean; isRating?: boolean }
> = {
  trader_overview: { label: "Trader Overview", icon: "👤" },
  trading_patterns: { label: "Trading Patterns", icon: "📈" },
  market_preferences: { label: "Market Preferences", icon: "🏷️" },
  strategy_classification: { label: "Strategy", icon: "🧩" },
  position_sizing: { label: "Position Sizing", icon: "💰" },
  risk_management: { label: "Risk Management", icon: "🛡️" },
  win_rate_analysis: { label: "Win Rate Analysis", icon: "🎯" },
  consistency_assessment: { label: "Consistency", icon: "📊" },
  copy_worthiness_rating: {
    label: "Copy-Worthiness Rating",
    icon: "⭐",
    isRating: true,
  },
  copy_worthiness_justification: { label: "Rating Justification", icon: "📋" },
  key_strengths: { label: "Key Strengths", icon: "✅", isArray: true },
  specific_risks: { label: "Specific Risks", icon: "⚠️", isArray: true },
  recommendation: { label: "Recommendation", icon: "🧠" },
};

/** Ordered keys for display */
const FIELD_ORDER = [
  "trader_overview",
  "trading_patterns",
  "market_preferences",
  "strategy_classification",
  "position_sizing",
  "risk_management",
  "win_rate_analysis",
  "consistency_assessment",
  "copy_worthiness_rating",
  "copy_worthiness_justification",
  "key_strengths",
  "specific_risks",
  "recommendation",
];

/* ───────── Parse helpers ───────── */

function tryParseJSON(raw: string): TraderAnalysisJSON | null {
  // Strip markdown code fences if present
  let cleaned = raw.trim();
  if (cleaned.startsWith("```")) {
    cleaned = cleaned.replace(/^```(?:json)?\s*\n?/, "").replace(/\n?```\s*$/, "");
  }
  // Find the first { and last }
  const start = cleaned.indexOf("{");
  const end = cleaned.lastIndexOf("}");
  if (start === -1 || end === -1 || end <= start) return null;
  try {
    return JSON.parse(cleaned.slice(start, end + 1));
  } catch {
    return null;
  }
}

function extractRatingNumber(rating: string): number | null {
  const m = rating.match(/(\d{1,2})\s*\/\s*10/);
  return m ? parseInt(m[1], 10) : null;
}

/* ───────── Sub-components ───────── */

function RatingBar({ rating }: { rating: number }) {
  const color = rating >= 7 ? "bg-green-500" : rating >= 4 ? "bg-[var(--accent)]" : "bg-red-500";
  return (
    <div className="flex items-center gap-3 w-full">
      <span className="text-2xl font-extrabold text-[var(--accent)]">{rating}/10</span>
      <div className="flex-1 bg-[var(--bg-soft)] rounded-full h-2.5">
        <div
          className={`h-2.5 rounded-full transition-all duration-500 ${color}`}
          style={{ width: `${rating * 10}%` }}
        />
      </div>
    </div>
  );
}

function ArrayValue({ items, isRisk }: { items: string[]; isRisk?: boolean }) {
  return (
    <ul className="space-y-1.5">
      {items.map((item, i) => (
        <li key={i} className="flex items-start gap-2 text-sm text-soft">
          <span
            className={`mt-1.5 w-1.5 h-1.5 rounded-full flex-shrink-0 ${
              isRisk ? "bg-red-400" : "bg-[var(--accent)]"
            }`}
          />
          <span>{item}</span>
        </li>
      ))}
    </ul>
  );
}

/* ───────── Main component ───────── */

export default function TraderAnalysisTable({ text, streaming }: TraderAnalysisTableProps) {
  const parsed = useMemo(() => tryParseJSON(text), [text]);

  // While streaming, show raw text with a cursor
  if (streaming) {
    return (
      <div className="rounded-xl border border-[var(--line)] bg-[var(--bg-soft)] p-4 min-h-[120px]">
        <div className="flex items-center gap-2 mb-3">
          <span className="animate-spin h-4 w-4 border-2 border-[var(--accent)] border-t-transparent rounded-full" />
          <span className="text-sm text-muted font-medium">Analyzing trader profile…</span>
        </div>
        <pre className="text-xs text-soft whitespace-pre-wrap leading-relaxed max-h-48 overflow-y-auto scroll-soft opacity-50">
          {text.slice(-500)}
          <span className="animate-pulse">▌</span>
        </pre>
      </div>
    );
  }

  // If parsing failed, show fallback raw text
  if (!parsed) {
    return (
      <div className="rounded-xl border border-[var(--line)] bg-[var(--bg-soft)] p-4">
        <p className="text-sm text-soft whitespace-pre-wrap leading-relaxed">{text}</p>
      </div>
    );
  }

  // Build rows from the parsed JSON
  const rows: {
    key: string;
    label: string;
    icon: string;
    value: unknown;
    isArray?: boolean;
    isRating?: boolean;
  }[] = [];

  for (const key of FIELD_ORDER) {
    if (parsed[key] !== undefined && parsed[key] !== null && parsed[key] !== "") {
      const config = FIELD_CONFIG[key] || {
        label: key.replace(/_/g, " ").replace(/\b\w/g, (c) => c.toUpperCase()),
        icon: "📄",
      };
      rows.push({
        key,
        label: config.label,
        icon: config.icon,
        value: parsed[key],
        isArray: config.isArray,
        isRating: config.isRating,
      });
    }
  }

  // Also add any unknown keys not in FIELD_ORDER
  for (const key of Object.keys(parsed)) {
    if (
      !FIELD_ORDER.includes(key) &&
      parsed[key] !== undefined &&
      parsed[key] !== null &&
      parsed[key] !== ""
    ) {
      rows.push({
        key,
        label: key.replace(/_/g, " ").replace(/\b\w/g, (c) => c.toUpperCase()),
        icon: "📄",
        value: parsed[key],
        isArray: Array.isArray(parsed[key]),
      });
    }
  }

  return (
    <div className="rounded-xl border border-[var(--line)] bg-[var(--bg-soft)] overflow-hidden">
      {/* Header */}
      <div className="px-4 py-3 border-b border-[var(--line)] bg-gradient-to-r from-[var(--accent)]/8 to-transparent">
        <h4 className="text-sm font-semibold text-[var(--accent)] flex items-center gap-2">
          <span>🧠</span> AI Trader Analysis
        </h4>
      </div>

      {/* Vertical table */}
      <div className="divide-y divide-[var(--line)]">
        {rows.map((row) => {
          const ratingNum =
            row.isRating && typeof row.value === "string" ? extractRatingNumber(row.value) : null;

          return (
            <div
              key={row.key}
              className="flex flex-col sm:flex-row gap-1 sm:gap-4 px-4 py-3 hover:bg-[var(--bg-panel)] transition-colors"
            >
              {/* Label column */}
              <div className="flex items-center gap-2 sm:w-48 flex-shrink-0">
                <span className="text-base">{row.icon}</span>
                <span className="text-xs font-semibold text-muted uppercase tracking-wider whitespace-nowrap">
                  {row.label}
                </span>
              </div>

              {/* Value column */}
              <div className="flex-1 min-w-0">
                {ratingNum !== null ? (
                  <RatingBar rating={ratingNum} />
                ) : row.isArray && Array.isArray(row.value) ? (
                  <ArrayValue items={row.value as string[]} isRisk={row.key === "specific_risks"} />
                ) : (
                  <p className="text-sm text-soft leading-relaxed">{String(row.value)}</p>
                )}
              </div>
            </div>
          );
        })}
      </div>
    </div>
  );
}
