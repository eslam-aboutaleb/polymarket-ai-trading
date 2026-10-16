"""On-chain whale event model — detected CTF position movements."""

from sqlalchemy import Column, DateTime, Float, Integer, String

from app.models.base import Base
from app.utils.time import utc_now


class WhaleEvent(Base):
    """A whale-sized position movement detected on-chain.

    Sourced from CTF Exchange / Conditional Tokens logs, so it lands
    seconds after the transaction confirms — ahead of the public
    positions API poll.
    """

    __tablename__ = "whale_events"

    id = Column(Integer, primary_key=True, index=True)
    wallet = Column(String(42), nullable=False, index=True)
    market_id = Column(String(200), nullable=False, default="")
    token_id = Column(String(200), nullable=False, default="")
    event_type = Column(
        String(40),
        nullable=False,
        default="transfer",  # transfer | position_split | position_merge | redemption
    )
    side = Column(String(20), nullable=False, default="")  # buy | sell | open | close | redeem
    size = Column(Float, nullable=False, default=0.0)
    price = Column(Float, nullable=False, default=0.0)
    notional = Column(Float, nullable=False, default=0.0)
    tx_hash = Column(String(100), nullable=False, default="", index=True)
    log_index = Column(Integer, nullable=True)
    block_number = Column(Integer, nullable=True)
    block_ts = Column(DateTime(timezone=True), nullable=True)
    detected_at = Column(DateTime(timezone=True), default=utc_now, nullable=False)
    created_at = Column(DateTime(timezone=True), default=utc_now, nullable=False)

    def __repr__(self):
        return (
            f"<WhaleEvent(wallet={self.wallet}, market={self.market_id}, "
            f"side={self.side}, notional={self.notional})>"
        )
