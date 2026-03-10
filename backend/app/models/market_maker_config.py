"""Market maker configuration per market per user."""
from sqlalchemy import (
    Column,
    String,
    Float,
    DateTime,
    Integer,
    ForeignKey,
    Boolean,
    Text,
)

from app.models.base import Base
from app.utils.time import utc_now


class MarketMakerConfig(Base):
    """Persistent configuration for the market making strategy on a single market."""

    __tablename__ = "market_maker_configs"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)

    # Market identification
    condition_id = Column(String(200), nullable=False, index=True)
    token_id_yes = Column(String(200), nullable=False)
    token_id_no = Column(String(200), nullable=False)
    market_title = Column(String(500), default="")

    enabled = Column(Boolean, nullable=False, default=False, index=True)

    # Strategy: "bands" or "amm"
    strategy = Column(String(20), nullable=False, default="bands")

    # ── Bands strategy parameters ──
    # Number of bands on each side of midpoint
    num_bands = Column(Integer, nullable=False, default=3)
    # Minimum spread from midpoint (percentage, e.g. 0.02 = 2%)
    min_spread = Column(Float, nullable=False, default=0.02)
    # Maximum spread from midpoint
    max_spread = Column(Float, nullable=False, default=0.10)
    # Order size per band in USDC
    band_order_size = Column(Float, nullable=False, default=10.0)

    # ── AMM strategy parameters ──
    # Liquidity depth (virtual USDC in the AMM curve)
    amm_liquidity = Column(Float, nullable=False, default=1000.0)

    # ── Shared parameters ──
    # Maximum total collateral (USDC) this maker can deploy on this market
    max_collateral = Column(Float, nullable=False, default=500.0)
    # Sync interval in seconds (how often to refresh orders)
    sync_interval_seconds = Column(Integer, nullable=False, default=30)
    # Minimum order size (below this, skip band)
    min_order_size = Column(Float, nullable=False, default=1.0)
    # Price bounds — don't place orders outside [min_price, max_price]
    min_price = Column(Float, nullable=False, default=0.01)
    max_price = Column(Float, nullable=False, default=0.99)

    # ── Runtime state ──
    # "idle" | "running" | "paused" | "error"
    status = Column(String(30), nullable=False, default="idle")
    last_sync_at = Column(DateTime(timezone=True), nullable=True)
    last_error = Column(Text, nullable=True)
    total_orders_placed = Column(Integer, nullable=False, default=0)
    total_orders_cancelled = Column(Integer, nullable=False, default=0)
    total_volume_usdc = Column(Float, nullable=False, default=0.0)
    current_open_orders = Column(Integer, nullable=False, default=0)

    created_at = Column(DateTime(timezone=True), default=utc_now, nullable=False)
    updated_at = Column(DateTime(timezone=True), default=utc_now, onupdate=utc_now)

    def __repr__(self):
        return (
            f"<MarketMakerConfig(user_id={self.user_id}, "
            f"condition_id={self.condition_id[:16]}..., "
            f"strategy={self.strategy}, enabled={self.enabled})>"
        )
