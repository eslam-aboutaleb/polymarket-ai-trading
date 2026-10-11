"""PortfolioSnapshot model — hourly bankroll snapshots for the equity curve.

One row per ``(user_id, bucket_hour)``: ``bucket_hour`` is the wall-clock
hour the row belongs to (the idempotency key), so a re-run or an
overlapping worker cannot insert a second row for the same hour. The
advisory lock that guards the writer fails open on database errors, so
that uniqueness is the only thing keeping the series clean.

``bankroll`` is the one canonical equity definition —
``kelly_service.compute_bankroll`` = USDC balance + unrealized PnL. The
service writes it; nothing recomputes it on read.
"""

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    UniqueConstraint,
)

from app.models.base import Base
from app.utils.time import utc_now


class PortfolioSnapshot(Base):
    """A single hourly portfolio-equity sample for one user.

    ``is_paper`` splits the series: paper accounts track
    ``UserSettings.paper_balance``, never the real wallet, so the two
    series are never mixed on one chart.
    """

    __tablename__ = "portfolio_snapshots"
    # Matches the migration's unique index — the writer's idempotency guarantee.
    __table_args__ = (
        UniqueConstraint(
            "user_id",
            "bucket_hour",
            name="uq_portfolio_snapshots_user_bucket",
        ),
    )

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    # Truncated to the hour — the idempotency key for the writer.
    bucket_hour = Column(DateTime(timezone=True), nullable=False, index=True)
    captured_at = Column(DateTime(timezone=True), default=utc_now, nullable=False)
    usdc_balance = Column(Float, nullable=False)
    unrealized_pnl = Column(Float, nullable=False)
    bankroll = Column(Float, nullable=False)
    total_invested = Column(Float, nullable=False, default=0.0)
    total_current_value = Column(Float, nullable=False, default=0.0)
    positions_count = Column(Integer, nullable=False, default=0)
    is_paper = Column(Boolean, nullable=False, default=False, server_default="0")

    def __repr__(self):
        return (
            f"<PortfolioSnapshot(user={self.user_id}, bucket={self.bucket_hour}, "
            f"bankroll={self.bankroll}, paper={self.is_paper})>"
        )
