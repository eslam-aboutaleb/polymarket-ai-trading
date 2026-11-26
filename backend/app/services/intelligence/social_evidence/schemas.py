"""Typed social post records. ``None`` always means unknown, never zero or now."""

from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict

Provenance = Literal["api_authorized", "indexed_snippet"]
PostStatus = Literal["active", "deleted", "unavailable"]
Mode = Literal["disabled", "api", "indexed_fallback"]


class Engagement(BaseModel):
    model_config = ConfigDict(extra="forbid")
    likes: int | None = None
    reposts: int | None = None
    replies: int | None = None
    quotes: int | None = None


class SocialPost(BaseModel):
    model_config = ConfigDict(extra="forbid")
    provider: str
    post_id: str
    version: int = 1
    canonical_url: str
    author_id: str | None = None  # only set when the provider returned it
    author_handle: str | None = None
    provider_verified: bool | None = None  # provider's own flag, never our inference
    text: str = ""
    text_hash: str
    posted_at: AwareDatetime | None = None  # None when the source did not supply it
    first_seen_at: AwareDatetime
    observed_at: AwareDatetime
    engagement: Engagement | None = None
    engagement_as_of: AwareDatetime | None = None
    status: PostStatus = "active"
    url_aliases: list[str] = []
    duplicate_of: str | None = None
    provenance: Provenance
    injection_flagged: bool = False


class SocialCoverage(BaseModel):
    """What the caller may and may not conclude from the social section."""

    model_config = ConfigDict(extra="forbid")
    mode: Mode
    fallback_reason: str | None = None
    provider: str | None = None
    posts_seen: int = 0
    posts_used: int = 0
    duplicates: int = 0
    deleted: int = 0
    rejected_future: int = 0
    comprehensive: bool = False  # never true: neither provider proves complete coverage
    notices: list[str] = []
