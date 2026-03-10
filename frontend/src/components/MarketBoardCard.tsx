import { MarketBoardCardVM } from "../types/marketBoard";
import { TradeTicketOutcome } from "../types/trading";

interface MarketBoardCardProps {
  card: MarketBoardCardVM;
  onTrade: (outcome?: TradeTicketOutcome) => void;
  onRowTrade?: (rowId: string, outcome: TradeTicketOutcome) => void;
  onOpenDetail?: () => void;
  onToggleFavorite?: () => void;
}

function ProbabilityRing({
  probability,
  label,
}: {
  probability: number;
  label?: string;
}) {
  const pct = Math.max(0, Math.min(100, Math.round(probability * 100)));
  const radius = 18;
  const circumference = 2 * Math.PI * radius;
  const progress = (pct / 100) * circumference;

  return (
    <div className="pm-card-ring" aria-label={`${pct}% ${label || "chance"}`}>
      <svg viewBox="0 0 44 44" className="pm-card-ring-svg" aria-hidden="true">
        <circle cx="22" cy="22" r={radius} className="pm-card-ring-track" />
        <circle
          cx="22"
          cy="22"
          r={radius}
          className="pm-card-ring-progress"
          style={{
            strokeDasharray: `${progress} ${circumference - progress}`,
          }}
        />
      </svg>
      <div className="pm-card-ring-center">
        <span className="pm-card-ring-value">{pct}%</span>
        <span className="pm-card-ring-label">{label || "chance"}</span>
      </div>
    </div>
  );
}

function IconBookmark() {
  return (
    <svg viewBox="0 0 24 24" className="pm-card-icon" aria-hidden="true">
      <path
        d="M7 4.75A1.75 1.75 0 0 1 8.75 3h6.5A1.75 1.75 0 0 1 17 4.75v15.5a.75.75 0 0 1-1.206.595L12 17.9l-3.794 2.946A.75.75 0 0 1 7 20.25V4.75Z"
        fill="none"
        stroke="currentColor"
        strokeWidth="1.7"
        strokeLinejoin="round"
      />
    </svg>
  );
}

function IconDetails() {
  return (
    <svg viewBox="0 0 24 24" className="pm-card-icon" aria-hidden="true">
      <path
        d="M4 12h16M12 4v16"
        fill="none"
        stroke="currentColor"
        strokeWidth="1.8"
        strokeLinecap="round"
      />
    </svg>
  );
}

export default function MarketBoardCard({
  card,
  onTrade,
  onRowTrade,
  onOpenDetail,
  onToggleFavorite,
}: MarketBoardCardProps) {
  return (
    <article
      className={`pm-card ${card.variant === "multi_option" ? "pm-card-multi-option" : ""}`}
      aria-label={card.title}
    >
      <header className="pm-card-header">
        <div className="pm-card-title-wrap">
          {card.image ? (
            <img src={card.image} alt="" className="pm-card-image" />
          ) : (
            <span className="pm-card-image pm-card-image-fallback" aria-hidden="true">
              M
            </span>
          )}
          <div className="pm-card-title-col">
            <h3 className="pm-card-title" title={card.title}>
              {card.href ? (
                <a
                  href={card.href}
                  target="_blank"
                  rel="noopener noreferrer"
                  className="hover:underline"
                >
                  {card.title}
                </a>
              ) : (
                card.title
              )}
            </h3>
            {card.aiSummary && (
              <div className="pm-card-ai-row">
                <span className="pm-chip pm-chip-ai">AI {Math.round(card.aiSummary.score)}</span>
                <span className="pm-chip pm-chip-rec">{card.aiSummary.recommendation}</span>
                <span className="pm-chip pm-chip-risk">{card.aiSummary.risk}</span>
              </div>
            )}
          </div>
        </div>

        {typeof card.probability === "number" ? (
          <ProbabilityRing probability={card.probability} label={card.probabilityLabel} />
        ) : (
          <div className="pm-card-ring-placeholder" />
        )}
      </header>

      <div className="pm-card-body">
        {(card.variant === "binary_single" || card.variant === "price_direction") && (
          <div className="pm-card-binary-actions">
            <button
              type="button"
              className="pm-binary pm-binary-positive"
              onClick={() => onTrade(card.positiveOutcome || "Yes")}
            >
              <span>{card.positiveLabel || "Yes"}</span>
              {card.positiveMeta && <small>{card.positiveMeta}</small>}
            </button>
            <button
              type="button"
              className="pm-binary pm-binary-negative"
              onClick={() => onTrade(card.negativeOutcome || "No")}
            >
              <span>{card.negativeLabel || "No"}</span>
              {card.negativeMeta && <small>{card.negativeMeta}</small>}
            </button>
          </div>
        )}

        {card.variant === "multi_option" && (
          <div className="pm-card-rows">
            {(card.rows || []).map((row) => (
              <div key={row.id} className="pm-row">
                <div className="pm-row-label" title={row.label}>
                  {row.label}
                </div>
                <div className="pm-row-right">
                  {row.probabilityText && (
                    <span className="pm-row-prob">{row.probabilityText}</span>
                  )}
                  <button
                    type="button"
                    className="pm-mini-pill pm-mini-pill-yes pm-mini-pill-btn"
                    onClick={() => onRowTrade?.(row.id, "Yes")}
                    aria-label={`Trade ${row.label} Yes`}
                    disabled={!onRowTrade}
                  >
                    {row.yesLabel || "Yes"}
                  </button>
                  <button
                    type="button"
                    className="pm-mini-pill pm-mini-pill-no pm-mini-pill-btn"
                    onClick={() => onRowTrade?.(row.id, "No")}
                    aria-label={`Trade ${row.label} No`}
                    disabled={!onRowTrade}
                  >
                    {row.noLabel || "No"}
                  </button>
                </div>
              </div>
            ))}
          </div>
        )}
      </div>

      <footer className="pm-card-footer">
        <div className="pm-card-meta">
          <span>{card.footerMeta}</span>
          {card.footerSubMeta && <span className="pm-card-meta-sep">·</span>}
          {card.footerSubMeta && <span>{card.footerSubMeta}</span>}
          {card.isLive && <span className="pm-live-dot">LIVE</span>}
        </div>

        <div className="pm-card-footer-actions">
          {onOpenDetail && (
            <button
              type="button"
              className="pm-icon-btn"
              aria-label="Open market details"
              onClick={onOpenDetail}
            >
              <IconDetails />
            </button>
          )}
          {onToggleFavorite && (
            <button
              type="button"
              className={`pm-icon-btn ${card.isFavorite ? "is-active" : ""}`}
              aria-label={
                card.isFavorite ? "Remove market from watchlist" : "Add market to watchlist"
              }
              onClick={onToggleFavorite}
            >
              <IconBookmark />
            </button>
          )}
        </div>
      </footer>
    </article>
  );
}
