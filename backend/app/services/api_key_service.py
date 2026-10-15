"""API key service — create, authenticate and revoke (plan 06 Phase B).

The security posture mirrors ``app.security.refresh_tokens`` exactly: a
secret is generated, hashed with HMAC-SHA256 under a dedicated server
key, and only the digest plus a short public prefix are persisted. The
raw secret crosses the wire once, in the creation response, and is never
stored, logged or recoverable — a lost key is revoked, not retrieved.

Invariants:

* **Hash-then-store.** ``key_hash`` is the only copy of the secret that
  hits the database, and it is the only value an authentication
  compares against, with ``hmac.compare_digest``.
* **Constant-time lookup.** A secret is resolved through its indexed
  public prefix and then compared as a digest, so timing does not leak
  whether a prefix exists.
* **Revocation is final.** ``revoked_at`` and ``expires_at`` are checked
  on every authentication; there is no un-revoke.
* **No secret in logs.** Every log line carries the prefix, never the
  secret or the digest.
* **Last-used telemetry is throttled.** ``last_used_at`` is written at
  most once a minute per key, so a busy key does not turn every read
  into a write.
* **No withdraw scope.** ``SCOPES`` is ``read`` and ``trade``; nothing
  in this module can be granted a capability that moves funds.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import secrets
from dataclasses import dataclass
from datetime import timedelta

from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models.api_key import ApiKey
from app.models.user import User
from app.utils.time import utc_now

logger = logging.getLogger(__name__)

#: Scopes a key can hold. ``withdraw`` is deliberately absent in v1.
SCOPE_READ = "read"
SCOPE_TRADE = "trade"
SCOPES = (SCOPE_READ, SCOPE_TRADE)

#: Secret layout: a short public label so a leaked prefix is recognisable
#: as one of ours, plus entropy ``secrets`` draws from the OS.
KEY_PREFIX_LABEL = "pk_"
KEY_RANDOM_BYTES = 32
#: How much of the secret is stored as the public lookup prefix.
KEY_PREFIX_LENGTH = 12
#: Upper bound on rows sharing a prefix (a digest comparison each). The
#: prefix is 12 characters of a 43-character secret, so in practice this
#: is one row; the bound exists so a pathological database cannot turn
#: authentication into a full-table scan.
MAX_PREFIX_CANDIDATES = 5

#: ``last_used_at`` is refreshed at most this often, per key.
LAST_USED_THROTTLE_SECONDS = 60

#: Default lifetime, in days, for a key with no explicit expiry.
DEFAULT_EXPIRY_DAYS = 90
MAX_EXPIRY_DAYS = 365

MAX_KEY_NAME_LENGTH = 100


@dataclass(frozen=True)
class AuthContext:
    """Who an authenticated API key acts as.

    Not a ``dict`` so a route cannot silently treat it as a user
    session: an external caller has no cookie, no bearer token and no
    admin claim, and this type makes that shape explicit.
    """

    user_id: int
    account_id: int | None
    wallet_address: str
    key_id: int
    key_prefix: str
    scopes: frozenset[str]

    def has_scope(self, scope: str) -> bool:
        return scope in self.scopes


def _hash_secret() -> str:
    settings = get_settings()
    # Same rollout order as refresh tokens: a dedicated key, then the
    # session secret, so an existing deployment keeps working. The
    # ``getattr`` keeps this importable before the setting lands.
    configured = str(getattr(settings, "api_key_hash_secret", "") or "").strip()
    secret = configured or str(getattr(settings, "jwt_secret_key", "") or "").strip()
    if not secret:
        raise ValueError("API key hash secret is not configured.")
    return secret


def hash_api_key(raw_key: str) -> str:
    """Return the deterministic HMAC-SHA256 digest of a raw API key."""
    token = (raw_key or "").strip()
    if not token:
        raise ValueError("API key cannot be empty.")
    return hmac.new(
        _hash_secret().encode("utf-8"),
        token.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def _api_key_prefix(raw_key: str) -> str:
    return raw_key[:KEY_PREFIX_LENGTH]


def normalize_scopes(scopes: list[str] | tuple[str, ...] | None) -> list[str]:
    """Validate and de-duplicate a requested scope list.

    An empty or missing list means "read only": a key is never created
    with more than was asked for, and an unknown scope is rejected
    rather than ignored so a typo cannot silently widen a key.
    """
    requested = [str(scope).strip().lower() for scope in (scopes or [])]
    unknown = [scope for scope in requested if scope not in SCOPES]
    if unknown:
        allowed = ", ".join(SCOPES)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Unknown scope(s): {', '.join(sorted(set(unknown)))}. Allowed: {allowed}",
        )
    return sorted(set(requested) or [SCOPE_READ])


def create_api_key(
    db: Session,
    *,
    user_id: int,
    name: str,
    account_id: int | None = None,
    scopes: list[str] | None = None,
    expires_in_days: int | None = DEFAULT_EXPIRY_DAYS,
) -> tuple[ApiKey, str]:
    """Create a key and return it with its raw secret.

    The secret exists only in the returned string. The caller must show
    it to the user immediately: nothing in the database can reproduce
    it afterwards.
    """
    clean_name = (name or "").strip()[:MAX_KEY_NAME_LENGTH] or f"key-{utc_now():%Y%m%d}"
    granted = normalize_scopes(scopes)

    if expires_in_days is not None:
        days = max(1, min(int(expires_in_days), MAX_EXPIRY_DAYS))
        expires_at = utc_now() + timedelta(days=days)
    else:
        expires_at = None

    raw_key = f"{KEY_PREFIX_LABEL}{secrets.token_urlsafe(KEY_RANDOM_BYTES)}"
    record = ApiKey(
        user_id=user_id,
        account_id=account_id,
        name=clean_name,
        key_prefix=_api_key_prefix(raw_key),
        key_hash=hash_api_key(raw_key),
        scopes=json.dumps(granted),
        expires_at=expires_at,
    )
    db.add(record)
    db.commit()
    db.refresh(record)
    logger.info(
        "Created API key %s (prefix=%s) for user_id=%s account_id=%s scopes=%s",
        record.id,
        record.key_prefix,
        user_id,
        account_id,
        granted,
    )
    return record, raw_key


def parse_scopes(raw: str | None) -> list[str]:
    """Decode the stored scope list, tolerating a malformed row."""
    if not raw:
        return []
    try:
        decoded = json.loads(raw)
    except (TypeError, ValueError):
        return []
    if not isinstance(decoded, list):
        return []
    return [str(scope) for scope in decoded if str(scope) in SCOPES]


def _is_active(record: ApiKey, now) -> bool:
    if record.revoked_at is not None:
        return False
    expires_at = record.expires_at
    if expires_at is None:
        return True
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=now.tzinfo)
    return expires_at > now


def _touch_last_used(db: Session, record: ApiKey, now) -> None:
    """Record usage at most once a minute (telemetry, not accounting)."""
    last_used = record.last_used_at
    if last_used is not None:
        if last_used.tzinfo is None:
            last_used = last_used.replace(tzinfo=now.tzinfo)
        if (now - last_used).total_seconds() < LAST_USED_THROTTLE_SECONDS:
            return
    record.last_used_at = now
    try:
        db.commit()
    except Exception as exc:  # pragma: no cover - telemetry must not fail auth
        logger.warning("Could not persist last_used_at for API key %s: %s", record.id, exc)
        db.rollback()


def authenticate_api_key(db: Session, raw_key: str | None) -> AuthContext | None:
    """Resolve a raw API key to an ``AuthContext``, or ``None``.

    ``None`` covers every rejection — absent, malformed, unknown,
    revoked and expired — so the caller cannot distinguish them and the
    endpoint is not an existence oracle. Scope enforcement is a separate
    step (``require_api_key``) so a wrong scope is a 403, not a 401.

    A ``ValueError`` propagates when the server has no hash secret
    configured: that is a deployment fault, not a bad credential, and
    the caller is expected to answer 503 rather than 401.
    """
    token = (raw_key or "").strip()
    if not token or len(token) <= KEY_PREFIX_LENGTH:
        return None

    digest = hash_api_key(token)

    # The prefix is indexed but not unique, so every candidate sharing
    # it is digest-compared rather than assuming the first is the match.
    candidates = (
        db.query(ApiKey)
        .filter(ApiKey.key_prefix == _api_key_prefix(token))
        .order_by(ApiKey.id.desc())
        .limit(MAX_PREFIX_CANDIDATES)
        .all()
    )
    record = None
    for candidate in candidates:
        if hmac.compare_digest(candidate.key_hash, digest):
            record = candidate
            break
    if record is None:
        return None

    now = utc_now()
    if not _is_active(record, now):
        logger.info("Rejected inactive API key %s (prefix=%s)", record.id, record.key_prefix)
        return None

    user = db.query(User).filter(User.id == record.user_id).first()
    if user is None:
        logger.warning("API key %s has no owning user (%s)", record.id, record.user_id)
        return None

    _touch_last_used(db, record, now)
    return AuthContext(
        user_id=record.user_id,
        account_id=record.account_id,
        wallet_address=user.wallet_address,
        key_id=record.id,
        key_prefix=record.key_prefix,
        scopes=frozenset(parse_scopes(record.scopes)),
    )


def list_api_keys(db: Session, user_id: int) -> list[ApiKey]:
    """The owner's keys, newest first. Never includes a digest."""
    return (
        db.query(ApiKey)
        .filter(ApiKey.user_id == user_id)
        .order_by(ApiKey.created_at.desc(), ApiKey.id.desc())
        .all()
    )


def revoke_api_key(db: Session, user_id: int, key_id: int) -> ApiKey | None:
    """Revoke one of the owner's keys; returns None when it is not theirs.

    A foreign id returns ``None`` rather than raising so the endpoint is
    not an existence oracle for other users' keys.
    """
    record = db.query(ApiKey).filter(ApiKey.id == key_id, ApiKey.user_id == user_id).first()
    if record is None:
        return None
    if record.revoked_at is None:
        record.revoked_at = utc_now()
        db.commit()
        db.refresh(record)
        logger.info("Revoked API key %s for user_id=%s", record.id, user_id)
    return record


def serialize_api_key(record: ApiKey, *, include_secret: str | None = None) -> dict:
    """Serialise a key for the management API.

    The digest is never part of this payload. ``include_secret`` is a
    keyword the create path passes to return the one-time secret; no
    other caller has any reason to.
    """
    payload = {
        "id": record.id,
        "name": record.name,
        "key_prefix": record.key_prefix,
        "account_id": record.account_id,
        "scopes": parse_scopes(record.scopes),
        "expires_at": record.expires_at.isoformat() if record.expires_at else None,
        "last_used_at": record.last_used_at.isoformat() if record.last_used_at else None,
        "revoked_at": record.revoked_at.isoformat() if record.revoked_at else None,
        "created_at": record.created_at.isoformat() if record.created_at else None,
        "revoked": record.revoked_at is not None,
    }
    if include_secret is not None:
        payload["secret"] = include_secret
    return payload
