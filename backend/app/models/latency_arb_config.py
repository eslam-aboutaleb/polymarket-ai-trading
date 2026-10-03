"""Per-user latency-arbitrage engine configuration."""

from sqlalchemy import JSON, Boolean, Column, DateTime, Float, ForeignKey, Integer

from app.models.base import Base
from app.utils.time import utc_now


class LatencyArbConfig(Base):
    """Latency-arbitrage thresholds and market selection for a single user."""

    __tablename__ = "latency_arb_configs"

    user_id = Column(Integer, ForeignKey("users.id"), primary_key=True)
    enabled = Column(Boolean, nullable=False, default=False)
    edge_threshold = Column(Float, nullable=False, default=0.03)
    max_notional = Column(Float, nullable=False, default=50.0)
    symbols = Column(JSON, nullable=False, default=list)
    windows = Column(JSON, nullable=False, default=list)
    late_entry = Column(Boolean, nullable=False, default=False)
    daily_loss_limit = Column(Float, nullable=False, default=20.0)
    alert_on_opportunity = Column(Boolean, nullable=False, default=True)
    created_at = Column(DateTime(timezone=True), default=utc_now, nullable=False)
    updated_at = Column(
        DateTime(timezone=True),
        default=utc_now,
        onupdate=utc_now,
        nullable=False,
    )

    def __repr__(self):
        return (
            f"<LatencyArbConfig(user={self.user_id}, "
            f"enabled={self.enabled}, edge_threshold={self.edge_threshold})>"
        )
