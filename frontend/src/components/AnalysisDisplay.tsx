/**
 * Renders LLM-produced market analysis markdown as a grid of styled section cards.
 *
 * `parseAnalysis` splits the raw text on markdown headings (falling back to numbered bold lines),
 * classifies each section by title, and picks a specialised card (probability, confidence, action,
 * position size, conclusion, copy-worthiness, or bullets). During streaming the component shows raw
 * text until enough content arrives to parse. `AnalysisDisplayInline` is a compact variant for
 * quick-analysis summaries.
 *
 * @module components/AnalysisDisplay
 */
import { useMemo } from "react";

/* ───────── Types ───────── */

interface ParsedSection {
  title: string;
  content: string;
  bullets: string[];
  type:
    | "findings"
    | "probability"
    | "confidence"
    | "factors"
    | "action"
    | "position"
    | "risks"
    | "conclusion"
    | "trading_pattern"
    | "strategy"
    | "copy_worthy"
    | "generic";
}

interface AnalysisDisplayProps {
  /** Raw markdown / plain-text analysis string */
  text: string;
  /** True while streaming is still in progress */
  streaming?: boolean;
}

/* ───────── Markdown → structured sections parser ───────── */

function classifySection(title: string): ParsedSection["type"] {
  const t = title.toLowerCase();
  if (/key\s*finding|finding/i.test(t)) return "findings";
  if (/probability|assessment/i.test(t)) return "probability";
  if (/confidence/i.test(t)) return "confidence";
  if (/factor|support|key factor/i.test(t)) return "factors";
  if (/recommend|action/i.test(t)) return "action";
  if (/position\s*size|allocation/i.test(t)) return "position";
  if (/risk/i.test(t)) return "risks";
  if (/trading\s*pattern|pattern\s*analysis/i.test(t)) return "trading_pattern";
  if (/strategy|strategy\s*assessment/i.test(t)) return "strategy";
  if (/copy[- ]?worth|rating/i.test(t)) return "copy_worthy";
  if (/conclusion|summary/i.test(t)) return "conclusion";
  return "generic";
}

function extractBullets(text: string): string[] {
  return text
    .split("\n")
    .map((l) => l.replace(/^[\s]*[-*•]\s*/, "").trim())
    .filter((l) => l.length > 0 && !l.startsWith("#"));
}

function parseAnalysis(raw: string): {
  title: string;
  sections: ParsedSection[];
} {
  // Normalise windows line-endings
  const text = raw.replace(/\r\n/g, "\n");

  // Extract top header if present (###, ##, or first bold line)
  let title = "";
  const headerMatch = text.match(/^#{1,4}\s+(.+)/m);
  if (headerMatch) title = headerMatch[1].replace(/\*\*/g, "").trim();

  // Split on markdown headings (#### or ###)
  const sectionRegex = /^#{2,5}\s+\d*\.?\s*(.+)/gm;
  const starts: { idx: number; heading: string }[] = [];
  let m: RegExpExecArray | null;
  while ((m = sectionRegex.exec(text)) !== null) {
    starts.push({ idx: m.index, heading: m[1].replace(/\*\*/g, "").trim() });
  }

  if (starts.length === 0) {
    // No markdown headings — try numbered bold lines  "**1. Key Findings**"
    const boldNumRegex = /^\*{0,2}\d+\.\s*\*{0,2}(.+?)\*{0,2}\s*$/gm;
    while ((m = boldNumRegex.exec(text)) !== null) {
      starts.push({ idx: m.index, heading: m[1].trim() });
    }
  }

  if (starts.length === 0) {
    // Still nothing — return one giant section
    return {
      title: title || "Analysis",
      sections: [
        {
          title: "Analysis",
          content: text,
          bullets: [],
          type: "generic",
        },
      ],
    };
  }

  const sections: ParsedSection[] = starts.map((s, i) => {
    const nextIdx = i + 1 < starts.length ? starts[i + 1].idx : text.length;
    const body = text
      .slice(s.idx, nextIdx)
      // Remove the heading line itself
      .replace(/^.*\n/, "")
      .trim();
    const bullets = extractBullets(body);
    return {
      title: s.heading,
      content: body,
      bullets,
      type: classifySection(s.heading),
    };
  });

  return { title: title || "AI Analysis", sections };
}

/* ───────── Tiny reusable sub-components ───────── */

function SectionIcon({ type }: { type: ParsedSection["type"] }) {
  const map: Record<ParsedSection["type"], string> = {
    findings: "🔍",
    probability: "📊",
    confidence: "🎯",
    factors: "📌",
    action: "⚡",
    position: "💰",
    risks: "⚠️",
    conclusion: "📝",
    trading_pattern: "📈",
    strategy: "🧩",
    copy_worthy: "⭐",
    generic: "📄",
  };
  return <span className="text-base mr-2 flex-shrink-0">{map[type]}</span>;
}

export function badgeColor(type: ParsedSection["type"]): string {
  switch (type) {
    case "action":
      return "chip-accent";
    case "probability":
      return "chip-success";
    case "confidence":
      return "chip-warning";
    case "risks":
      return "chip-danger";
    default:
      return "chip-accent";
  }
}

/** Pull a bold-value from text, e.g. "**60%**" → "60%" */
function extractBoldValue(text: string): string | null {
  const m = text.match(/\*\*([^*]+)\*\*/);
  return m ? m[1].trim() : null;
}

/** Extract recommendation keyword from action section */
function extractRecommendation(text: string): { label: string; color: string } | null {
  const lower = text.toLowerCase();
  if (/buy\s*no/i.test(lower)) return { label: "BUY NO", color: "chip-danger" };
  if (/buy\s*yes/i.test(lower)) return { label: "BUY YES", color: "chip-success" };
  if (/hold/i.test(lower)) return { label: "HOLD", color: "chip-warning" };
  if (/sell/i.test(lower)) return { label: "SELL", color: "chip-danger" };
  return null;
}

/** Extract a percentage from text */
function extractPercentage(text: string): string | null {
  const m = text.match(/(\d{1,3})\s*%/);
  return m ? `${m[1]}%` : null;
}

/* ───────── Section renderers ───────── */

function ProbabilityCard({ section }: { section: ParsedSection }) {
  const pct = extractPercentage(section.content);
  const boldVal = extractBoldValue(section.content);
  const displayPct = boldVal || pct;
  const cleanText = section.content
    .replace(/\*\*/g, "")
    .replace(/^[-*•]\s*/gm, "")
    .trim();

  return (
    <div className="analysis-card analysis-card--highlight analysis-card--half">
      <div className="flex items-center gap-3 mb-3">
        <SectionIcon type="probability" />
        <h4 className="analysis-card__title">{section.title}</h4>
      </div>
      {displayPct && (
        <div className="flex items-center gap-3 mb-3">
          <span className="text-3xl font-extrabold text-[var(--accent)]">{displayPct}</span>
          <span className="text-xs text-muted uppercase tracking-wider">Assessed probability</span>
        </div>
      )}
      <p className="text-sm text-soft leading-relaxed">{cleanText}</p>
    </div>
  );
}

function ConfidenceCard({ section }: { section: ParsedSection }) {
  const boldVal = extractBoldValue(section.content);
  const level = boldVal || section.content.split(".")[0]?.trim();
  const levelLower = (level || "").toLowerCase();
  const color = levelLower.includes("high")
    ? "chip-success"
    : levelLower.includes("low")
      ? "chip-danger"
      : "chip-warning";
  const cleanText = section.content.replace(/\*\*/g, "").trim();

  return (
    <div className="analysis-card analysis-card--half">
      <div className="flex items-center gap-3 mb-3">
        <SectionIcon type="confidence" />
        <h4 className="analysis-card__title">{section.title}</h4>
        {level && <span className={`chip ${color} ml-auto text-xs`}>{level}</span>}
      </div>
      <p className="text-sm text-soft leading-relaxed">{cleanText}</p>
    </div>
  );
}

function ActionCard({ section }: { section: ParsedSection }) {
  const rec = extractRecommendation(section.content);
  const cleanText = section.content.replace(/\*\*/g, "").trim();

  return (
    <div className="analysis-card analysis-card--action">
      <div className="flex items-center gap-3 mb-3">
        <SectionIcon type="action" />
        <h4 className="analysis-card__title">{section.title}</h4>
        {rec && <span className={`chip ${rec.color} ml-auto text-sm font-bold`}>{rec.label}</span>}
      </div>
      <p className="text-sm text-soft leading-relaxed">{cleanText}</p>
    </div>
  );
}

function PositionCard({ section }: { section: ParsedSection }) {
  const pct = extractPercentage(section.content);
  const boldVal = extractBoldValue(section.content);
  const cleanText = section.content.replace(/\*\*/g, "").trim();

  return (
    <div className="analysis-card">
      <div className="flex items-center gap-3 mb-3">
        <SectionIcon type="position" />
        <h4 className="analysis-card__title">{section.title}</h4>
        {(boldVal || pct) && (
          <span className="chip chip-accent ml-auto text-sm font-bold">{boldVal || pct}</span>
        )}
      </div>
      <p className="text-sm text-soft leading-relaxed">{cleanText}</p>
    </div>
  );
}

function BulletCard({ section }: { section: ParsedSection }) {
  const isRisk = section.type === "risks";
  const isStrategy = section.type === "strategy" || section.type === "trading_pattern";

  const variantClass = isRisk ? "analysis-card--risk" : isStrategy ? "analysis-card--strategy" : "";

  return (
    <div className={`analysis-card ${variantClass}`}>
      <div className="flex items-center gap-3 mb-3">
        <SectionIcon type={section.type} />
        <h4 className="analysis-card__title">{section.title}</h4>
      </div>
      {section.bullets.length > 0 ? (
        <ul className="space-y-2">
          {section.bullets.map((b, i) => {
            // Strip sub-bold markers
            const clean = b.replace(/\*\*/g, "");
            // Split on first colon for label:detail
            const colonIdx = clean.indexOf(":");
            const label = colonIdx > 0 ? clean.slice(0, colonIdx) : null;
            const detail = colonIdx > 0 ? clean.slice(colonIdx + 1).trim() : clean;

            return (
              <li key={i} className="flex items-start gap-2 text-sm text-soft">
                <span
                  className={`mt-1.5 w-1.5 h-1.5 rounded-full flex-shrink-0 ${isRisk ? "bg-[var(--danger)]" : "bg-[var(--accent)]"}`}
                />
                <span>
                  {label && (
                    <span className="font-semibold text-[var(--text-primary)]">{label}:</span>
                  )}{" "}
                  {detail}
                </span>
              </li>
            );
          })}
        </ul>
      ) : (
        <p className="text-sm text-soft leading-relaxed">{section.content.replace(/\*\*/g, "")}</p>
      )}
    </div>
  );
}

function ConclusionCard({ section }: { section: ParsedSection }) {
  const rec = extractRecommendation(section.content);
  const cleanText = section.content.replace(/\*\*/g, "").trim();

  return (
    <div className="analysis-card analysis-card--conclusion">
      <div className="flex items-center gap-3 mb-3">
        <SectionIcon type="conclusion" />
        <h4 className="analysis-card__title">{section.title}</h4>
        {rec && <span className={`chip ${rec.color} ml-auto text-sm font-bold`}>{rec.label}</span>}
      </div>
      <p className="text-sm text-soft leading-relaxed">{cleanText}</p>
    </div>
  );
}

function CopyWorthyCard({ section }: { section: ParsedSection }) {
  // Try to extract a rating like "3/10" or "7/10"
  const ratingMatch = section.content.match(/(\d{1,2})\s*\/\s*10/);
  const rating = ratingMatch ? parseInt(ratingMatch[1], 10) : null;
  const ratingColor =
    rating !== null
      ? rating >= 7
        ? "chip-success"
        : rating >= 4
          ? "chip-warning"
          : "chip-danger"
      : "chip-accent";
  const cleanText = section.content.replace(/\*\*/g, "").trim();

  return (
    <div className="analysis-card analysis-card--highlight">
      <div className="flex items-center gap-3 mb-3">
        <SectionIcon type="copy_worthy" />
        <h4 className="analysis-card__title">{section.title}</h4>
        {rating !== null && (
          <span className={`chip ${ratingColor} ml-auto text-sm font-bold`}>{rating}/10</span>
        )}
      </div>
      {rating !== null && (
        <div className="w-full bg-[var(--bg-soft)] rounded-full h-2 mb-3">
          <div
            className={`h-2 rounded-full transition-all ${
              rating >= 7 ? "bg-green-500" : rating >= 4 ? "bg-yellow-500" : "bg-red-500"
            }`}
            style={{ width: `${rating * 10}%` }}
          />
        </div>
      )}
      <p className="text-sm text-soft leading-relaxed">{cleanText}</p>
    </div>
  );
}

function SectionCard({ section }: { section: ParsedSection }) {
  switch (section.type) {
    case "probability":
      return <ProbabilityCard section={section} />;
    case "confidence":
      return <ConfidenceCard section={section} />;
    case "action":
      return <ActionCard section={section} />;
    case "position":
      return <PositionCard section={section} />;
    case "conclusion":
      return <ConclusionCard section={section} />;
    case "copy_worthy":
      return <CopyWorthyCard section={section} />;
    case "findings":
    case "factors":
    case "risks":
    case "trading_pattern":
    case "strategy":
      return <BulletCard section={section} />;
    default:
      return <BulletCard section={section} />;
  }
}

/* ───────── Main export ───────── */

export default function AnalysisDisplay({ text, streaming }: AnalysisDisplayProps) {
  const { sections } = useMemo(() => parseAnalysis(text), [text]);

  // While streaming and we haven't accumulated enough for a real parse,
  // just show the raw text with a cursor
  if (streaming && sections.length <= 1 && text.length < 200) {
    return (
      <pre className="text-sm text-soft whitespace-pre-wrap leading-relaxed bg-[var(--bg-elevated)] border border-[var(--line)] rounded-lg p-4 min-h-[120px]">
        {text}
        <span className="animate-pulse">▌</span>
      </pre>
    );
  }

  return (
    <div className="analysis-grid">
      {sections.map((s, i) => (
        <SectionCard key={i} section={s} />
      ))}
      {streaming && (
        <div className="flex items-center gap-2 text-sm text-muted px-1 pt-1">
          <span className="animate-spin h-3.5 w-3.5 border-2 border-current border-t-transparent rounded-full inline-block" />
          Receiving more data…
        </div>
      )}
    </div>
  );
}

/* Also export a lightweight inline version for quick-analysis cards */
export function AnalysisDisplayInline({ text }: { text: string }) {
  const rec = extractRecommendation(text);
  const pct = extractPercentage(text);

  // For inline, show the first meaningful paragraph and summary chips
  const preview = text
    .replace(/\*\*/g, "")
    .replace(/^#+\s+.*/gm, "")
    .replace(/^[-*•]\s*/gm, "")
    .trim()
    .split("\n")
    .filter((l) => l.trim().length > 10)
    .slice(0, 3)
    .join(" ")
    .slice(0, 220);

  return (
    <div className="space-y-2">
      {(rec || pct) && (
        <div className="flex gap-2 items-center flex-wrap">
          {rec && <span className={`chip ${rec.color} text-xs font-bold`}>{rec.label}</span>}
          {pct && <span className="chip chip-accent text-xs">Probability: {pct}</span>}
        </div>
      )}
      <p className="text-xs text-soft leading-relaxed">
        {preview}
        {preview.length >= 220 && "…"}
      </p>
    </div>
  );
}
