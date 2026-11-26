"""Insert-only persistence, duplicate marking and point-in-time reads for social posts."""

import json
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.analytics.social_post import SocialPostVersion as Row
from app.services.intelligence.social_evidence.schemas import Engagement, SocialPost


def _to_row(post: SocialPost) -> Row:
    return Row(
        provider=post.provider,
        post_id=post.post_id,
        version=post.version,
        canonical_url=post.canonical_url,
        url_aliases_json=json.dumps(post.url_aliases),
        author_id=post.author_id,
        author_handle=post.author_handle,
        provider_verified=post.provider_verified,
        text=post.text,
        text_hash=post.text_hash,
        posted_at=post.posted_at,
        first_seen_at=post.first_seen_at,
        observed_at=post.observed_at,
        engagement_json=post.engagement.model_dump_json() if post.engagement else None,
        engagement_as_of=post.engagement_as_of,
        status=post.status,
        duplicate_of=post.duplicate_of,
        provenance=post.provenance,
        injection_flagged=post.injection_flagged,
    )


def _from_row(row: Row) -> SocialPost:
    return SocialPost(
        provider=row.provider,
        post_id=row.post_id,
        version=row.version,
        canonical_url=row.canonical_url,
        url_aliases=json.loads(row.url_aliases_json or "[]"),
        author_id=row.author_id,
        author_handle=row.author_handle,
        provider_verified=row.provider_verified,
        text=row.text,
        text_hash=row.text_hash,
        posted_at=_aware(row.posted_at),
        first_seen_at=_aware(row.first_seen_at),
        observed_at=_aware(row.observed_at),
        engagement=Engagement.model_validate_json(row.engagement_json)
        if row.engagement_json
        else None,
        engagement_as_of=_aware(row.engagement_as_of),
        status=row.status,
        duplicate_of=row.duplicate_of,
        provenance=row.provenance,
        injection_flagged=bool(row.injection_flagged),
    )


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    from datetime import UTC

    return value if value.tzinfo else value.replace(tzinfo=UTC)


def latest(db: Session, provider: str, post_id: str) -> Row | None:
    return db.scalars(
        select(Row)
        .where(Row.provider == provider, Row.post_id == post_id)
        .order_by(Row.version.desc())
        .limit(1)
    ).first()


def record(db: Session, post: SocialPost) -> SocialPost:
    """Persist an observation. Unchanged posts add nothing; edits, deletions and a changed URL
    add a version while keeping the original ``first_seen_at`` and the old canonical URL as alias.
    """
    prior = latest(db, post.provider, post.post_id)
    if prior is None:
        owner = db.scalars(
            select(Row)
            .where(Row.text_hash == post.text_hash, Row.status == "active")
            .order_by(Row.first_seen_at, Row.id)
            .limit(1)
        ).first()
        if owner is not None and (owner.provider, owner.post_id) != (post.provider, post.post_id):
            post = post.model_copy(update={"duplicate_of": f"{owner.provider}:{owner.post_id}"})
        db.add(_to_row(post))
        db.commit()
        return post
    changed = (
        prior.text_hash != post.text_hash
        or prior.status != post.status
        or prior.canonical_url != post.canonical_url
    )
    if not changed:
        return _from_row(prior)
    aliases = set(json.loads(prior.url_aliases_json or "[]"))
    if prior.canonical_url != post.canonical_url:
        aliases.add(prior.canonical_url)
    new = post.model_copy(
        update={
            "version": prior.version + 1,
            "first_seen_at": _aware(prior.first_seen_at),
            "url_aliases": sorted(aliases),
            "duplicate_of": prior.duplicate_of,
        }
    )
    db.add(_to_row(new))
    db.commit()
    return new


def mark_deleted(db: Session, provider: str, post_id: str, now: datetime) -> SocialPost | None:
    prior = latest(db, provider, post_id)
    if prior is None or prior.status == "deleted":
        return _from_row(prior) if prior else None
    base = _from_row(prior)
    gone = base.model_copy(
        update={"status": "deleted", "text": "", "engagement": None, "observed_at": now}
    )
    return record(db, gone)


def visible_as_of(db: Session, as_of: datetime) -> tuple[list[SocialPost], int]:
    """Point-in-time view: the newest version first seen by ``as_of``, excluding any post whose
    own publication time is later. Returns (posts, number of posts rejected as future)."""
    rows = db.scalars(select(Row).order_by(Row.provider, Row.post_id, Row.version)).all()
    newest: dict[tuple[str, str], Row] = {}
    future: set[tuple[str, str]] = set()
    for row in rows:
        key = (row.provider, row.post_id)
        posted = _aware(row.posted_at)
        if _aware(row.first_seen_at) > as_of or (posted and posted > as_of):
            future.add(key)
            continue
        newest[key] = row
    rejected = len(future - set(newest))
    return [_from_row(r) for r in newest.values()], rejected
