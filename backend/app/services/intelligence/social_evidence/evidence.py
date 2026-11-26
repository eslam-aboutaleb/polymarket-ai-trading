"""Map social posts to deep-dive evidence. All social posts share one publisher on purpose:
many posts are one family of evidence and never count as several independent sources."""

from datetime import datetime

from app.services.intelligence.market_deep_dive.evidence_adapter import content_hash, evidence_id
from app.services.intelligence.market_deep_dive.schemas import EvidenceItem
from app.services.intelligence.social_evidence.schemas import SocialPost

SOCIAL_PUBLISHER = "social:x.com"


def posts_to_evidence(posts: list[SocialPost], retrieved_at: datetime) -> list[EvidenceItem]:
    items = []
    for post in posts:
        if post.status != "active" or not post.text:
            continue
        limitations = ["Untrusted social text; shown as data, never as instructions."]
        if post.posted_at is None:
            limitations.append("Posting time was not supplied by the source.")
        if post.provenance == "indexed_snippet":
            limitations.append("Search-index snippet; author identity and engagement unknown.")
        if post.engagement is None:
            limitations.append("Engagement unknown.")
        if post.injection_flagged:
            limitations.append("Contains instruction-like text; ignored.")
        digest = content_hash(post.text)
        items.append(
            EvidenceItem(
                evidence_id=evidence_id(post.canonical_url, digest),
                url=post.canonical_url,
                canonical_url=post.canonical_url,
                publisher=SOCIAL_PUBLISHER,
                published_at=post.posted_at,
                retrieved_at=retrieved_at,
                available_at=post.first_seen_at,
                source_family="social_post",
                content_hash=digest,
                excerpt=post.text,
                verification="excerpt_matched",
                limitations=limitations,
            )
        )
    return items
