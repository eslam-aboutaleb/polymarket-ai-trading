"""RedemptionAttempt model for on-chain auto-redeem audit tracking."""

from sqlalchemy import Column, DateTime, Float, ForeignKey, Integer, String, Text

from app.models.base import Base
from app.utils.time import utc_now

# Attempt lifecycle statuses.
REDEEM_STATUS_PENDING = "pending"  # historical ambiguous status, never newly written
REDEEM_STATUS_PREVIEW = "preview"  # explicitly not broadcast
REDEEM_STATUS_LEGACY_UNKNOWN = "legacy_unknown"  # pre-migration pending row
REDEEM_STATUS_SUBMITTED = "submitted"
REDEEM_STATUS_CONFIRMED = "confirmed"
REDEEM_STATUS_FAILED = "failed"
REDEEM_STATUS_CLAIM_VIA_UI = "claim_via_ui"


class RedemptionAttempt(Base):
    """A single on-chain redemption attempt for a resolved-market position.

    Every redemption path — explicitly non-broadcast dry-run preview, live submission,
    on-chain confirmation, failure and the proxy claim-via-UI
    fallback — writes a row here so the full redemption history is
    auditable and the per-user daily redeem cap can be enforced.
    """

    __tablename__ = "redemption_attempts"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    account_id = Column(Integer, nullable=True, index=True)
    market_id = Column(String(200), nullable=False, index=True)  # conditionId
    collection_id = Column(String(66), nullable=False)  # bytes32 hex
    tx_hash = Column(String(66), nullable=True)  # bytes32 hex
    status = Column(
        String(20),
        nullable=False,
    )  # preview|legacy_unknown|submitted|confirmed|failed|claim_via_ui
    amount = Column(Float, nullable=False)  # winning share count (pays $1 each)
    error = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), default=utc_now, nullable=False)

    def __repr__(self):
        return (
            f"<RedemptionAttempt(user={self.user_id}, market={self.market_id}, "
            f"status={self.status})>"
        )
