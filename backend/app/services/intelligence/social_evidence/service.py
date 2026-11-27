"""Collect social evidence: authorized API when enabled, otherwise a labelled index fallback."""

import logging
from datetime import datetime

import httpx
from sqlalchemy.orm import Session

from app.services.intelligence.social_evidence import store
from app.services.intelligence.social_evidence.providers import (
    ProviderUnavailable,
    XApiProvider,
    indexed_posts,
)
from app.services.intelligence.social_evidence.quota import QuotaExceeded, SocialQuota
from app.services.intelligence.social_evidence.schemas import SocialCoverage, SocialPost

logger = logging.getLogger(__name__)


async def collect_social(
    db: Session,
    *,
    mode: str,
    query: str,
    as_of: datetime,
    indexed_items: list[dict],
    user_id: int | None,
    bearer_token: str | None,
    quota: SocialQuota,
    client: httpx.AsyncClient | None,
) -> tuple[list[SocialPost], SocialCoverage]:
    """Never raises for provider problems: the coverage says what was actually used."""
    if mode == "off":
        return [], SocialCoverage(mode="disabled")
    notices: list[str] = []
    seen: list[SocialPost] = []
    coverage_mode = "indexed_fallback"
    reason: str | None = None
    provider_name = "indexed_search"
    if mode == "x_api":
        if not bearer_token or client is None:
            reason = "SOCIAL_PROVIDER_NOT_AUTHORIZED"
        else:
            try:
                quota.acquire(user_id)
                seen = await XApiProvider(client, bearer_token).search(query, as_of)
                coverage_mode, provider_name = "api", XApiProvider.name
            except QuotaExceeded as exc:
                reason = exc.code
            except ProviderUnavailable as exc:
                reason = exc.code
        if reason:
            notices.append(f"{reason}: used indexed search snippets instead of the social API.")
    else:
        reason = None
        coverage_mode = "indexed_fallback"
    if coverage_mode != "api":
        seen = indexed_posts(indexed_items, as_of)
        if mode == "indexed":
            reason = None
        notices.append(
            "Indexed snippets carry no author id, posting time or engagement; "
            "absence of posts is not evidence of no social activity."
        )
    stored = [store.record(db, post) for post in seen]
    visible, rejected_future = store.visible_as_of(db, as_of)
    keys = {(p.provider, p.post_id) for p in stored}
    current = [p for p in visible if (p.provider, p.post_id) in keys]
    used = [p for p in current if p.status == "active" and p.duplicate_of is None]
    return used, SocialCoverage(
        mode=coverage_mode,  # type: ignore[arg-type]
        fallback_reason=reason,
        provider=provider_name,
        posts_seen=len(seen),
        posts_used=len(used),
        duplicates=sum(1 for p in current if p.duplicate_of),
        deleted=sum(1 for p in current if p.status == "deleted"),
        rejected_future=rejected_future,
        comprehensive=False,
        notices=notices,
    )
