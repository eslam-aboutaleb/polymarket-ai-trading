"""RefreshToken model for token management"""
from sqlalchemy import Column, String, DateTime, Integer, ForeignKey, Boolean
from datetime import datetime
from app.utils.time import utc_now
from app.models.base import Base


class RefreshToken(Base):
    """Refresh token storage for persistent user sessions"""
    __tablename__ = "refresh_tokens"
    
    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    token_hash = Column(String(64), unique=True, nullable=False, index=True)
    expires_at = Column(DateTime(timezone=True), nullable=False)
    is_revoked = Column(Boolean, default=False)
    created_at = Column(DateTime(timezone=True), default=utc_now, nullable=False)
    
    def __repr__(self):
        return f"<RefreshToken(user_id={self.user_id}, expires_at={self.expires_at})>"
