"""Insert-only versions of observed social posts (#57).

A post is identified by (provider, post_id). An edit, deletion or later engagement
read adds a version; earlier rows are never rewritten, so a replay at time T can
reproduce exactly what had been observed by T. Deleted content is not retained,
only its hash and the fact of deletion.
"""

from sqlalchemy import Boolean, Column, DateTime, Index, Integer, String, Text, UniqueConstraint

from app.models.base import Base


class SocialPostVersion(Base):
    __tablename__ = "social_post_versions"
    __table_args__ = (
        UniqueConstraint("provider", "post_id", "version", name="uq_social_post_version"),
        Index("ix_social_post_seen", "first_seen_at"),
    )

    id = Column(Integer, primary_key=True)
    provider = Column(String(30), nullable=False)
    post_id = Column(String(30), nullable=False)
    version = Column(Integer, nullable=False)
    canonical_url = Column(String(300), nullable=False)
    url_aliases_json = Column(Text, nullable=False, default="[]")
    author_id = Column(String(40), nullable=True)
    author_handle = Column(String(40), nullable=True)
    provider_verified = Column(Boolean, nullable=True)
    text = Column(Text, nullable=False, default="")
    text_hash = Column(String(64), nullable=False, index=True)
    posted_at = Column(DateTime(timezone=True), nullable=True)
    first_seen_at = Column(DateTime(timezone=True), nullable=False)
    observed_at = Column(DateTime(timezone=True), nullable=False)
    engagement_json = Column(Text, nullable=True)
    engagement_as_of = Column(DateTime(timezone=True), nullable=True)
    status = Column(String(16), nullable=False, default="active")
    duplicate_of = Column(String(60), nullable=True)
    provenance = Column(String(20), nullable=False)
    injection_flagged = Column(Boolean, nullable=False, default=False)
