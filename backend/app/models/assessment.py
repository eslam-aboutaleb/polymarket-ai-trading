"""Assessment model for AI trade evaluation"""

from sqlalchemy import Column, DateTime, Float, ForeignKey, Integer, String, Text

from app.models.base import Base
from app.utils.time import utc_now


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
    #: Model attribution so accuracy can be aggregated per model.
    model_name = Column(String(100), nullable=True)
    provider = Column(String(50), nullable=True)
    task = Column(String(50), nullable=True)
    #: Market identity for resolution matching (independent of the
    #: trade_history link, which is absent for scan verdicts).
    condition_id = Column(String(100), nullable=True)

    def __repr__(self):
        return f"<Assessment(trade_id={self.trade_history_id}, score={self.ai_score})>"
