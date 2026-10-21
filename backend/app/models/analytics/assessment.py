"""Assessment model for AI trade evaluation"""

from sqlalchemy import Column, DateTime, Float, ForeignKey, Integer, String, Text

from app.models.base import Base
from app.utils.time import utc_now

#: Vocabulary of a *verified* binary resolution. The resolver writes the
#: canonical "YES"/"NO"; rows written by the legacy resolver carry the raw
#: Gamma label ("Yes", "No", "Alice"), so the check is case-insensitive and
#: only these labels may become a calibration label. The scoring service
#: and every aggregate query share this one vocabulary, so a legacy
#: non-binary label is excluded everywhere instead of only in the summary.
BINARY_YES_LABELS = frozenset({"yes", "y", "true", "1"})
BINARY_NO_LABELS = frozenset({"no", "n", "false", "0"})
BINARY_OUTCOME_LABELS = BINARY_YES_LABELS | BINARY_NO_LABELS
VERIFIED_SCORING_VERSION = "brier-binary-v2"

#: Reason recorded on a row whose ``ai_probability`` could not be read.
#: A row with this reason is not a pre-calibration (legacy) row: it is a
#: verdict that was written without a usable probability.
NULL_PROBABILITY_REASON = "unusable_probability"


class Assessment(Base):
    """AI assessment of a trade opportunity"""

    __tablename__ = "assessments"

    id = Column(Integer, primary_key=True, index=True)
    # Nullable: opportunity-scan verdicts have no source trade row
    # (forecast calibration, plan 01) — only copy-trade evals do.
    trade_history_id = Column(Integer, ForeignKey("trade_history.id"), nullable=True, index=True)
    ai_score = Column(Float, nullable=False)  # 0-100 confidence score
    reasoning = Column(Text, nullable=False)
    recommendation = Column(String(50), nullable=False)  # copy, avoid, hold, etc.
    risk_level = Column(String(20), nullable=False)  # low, medium, high
    market_sentiment = Column(String(50), nullable=True)  # bullish, bearish, neutral
    confidence = Column(Float, nullable=False)  # 0-100
    created_at = Column(DateTime(timezone=True), default=utc_now, nullable=False)

    # ── Forecast calibration (plan 01) ──────────────────────────
    #: AI's calibrated probability the forecast resolves true (0.0-1.0).
    ai_probability = Column(Float, nullable=True)
    #: Market YES price when the verdict was produced.
    market_price_at_assessment = Column(Float, nullable=True)
    #: Winning outcome once the market resolved ("YES"/"NO"/…).
    resolved_outcome = Column(String(20), nullable=True)
    #: Brier score written by the nightly resolution job.
    brier_score = Column(Float, nullable=True)
    resolved_at = Column(DateTime(timezone=True), nullable=True)
    #: Scoring rule that produced ``brier_score`` (e.g. "brier-binary-v2").
    #: A change of scoring rule is then distinguishable from the rows
    #: written under the previous one.
    scoring_version = Column(String(32), nullable=True)
    #: Why the row was excluded from scoring (one of the
    #: ``forecast_resolution.ExclusionReason`` values). Null while the
    #: row is unresolved or once it has been scored.
    exclusion_reason = Column(String(40), nullable=True)
    #: Why ``ai_probability`` is NULL on this row. Null on every row that
    #: carries a probability, and on legacy rows written before the
    #: calibration columns existed (those are counted as legacy, not as
    #: a known-reason null).
    probability_null_reason = Column(String(40), nullable=True)
    #: Model attribution so accuracy can be aggregated per model.
    model_name = Column(String(100), nullable=True)
    provider = Column(String(50), nullable=True)
    task = Column(String(50), nullable=True)
    #: Market identity for resolution matching (independent of the
    #: trade_history link, which is absent for scan verdicts).
    condition_id = Column(String(100), nullable=True)

    # ── Model governance (plan 04) ──────────────────────────────
    #: Short content hash of the prompt store that produced this
    #: verdict. Every prompt change is a new version, so calibration
    #: impact is measurable without hand-numbered prompt releases.
    prompt_version = Column(String(16), nullable=True)
    #: A/B experiment this verdict belonged to, when any.
    experiment_id = Column(String(64), nullable=True)
    #: ``control`` or ``variant`` — the arm of that experiment.
    variant = Column(String(16), nullable=True)
    #: Model tier actually used (CHEAP/STANDARD/STRONG/WRITER). Null
    #: when tiering was disabled, which is why it must never be
    #: treated as a foreign key.
    llm_tier = Column(String(16), nullable=True)

    def __repr__(self):
        return f"<Assessment(trade_id={self.trade_history_id}, score={self.ai_score})>"
