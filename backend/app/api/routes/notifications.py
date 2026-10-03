"""Notification channels, in-app alert feed, and preference API routes."""

import json
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.api.routes.auth import get_current_user_from_token
from app.models.notification_channel import NotificationChannel
from app.models.notification_feed_event import NotificationFeedEvent
from app.models.user_notification_preference import UserNotificationPreference
from app.security.crypto import encrypt_text
from app.services.alert_service import (
    ALERT_EVENT_TYPES,
    get_alert_service,
)
from app.utils.database import get_db
from app.utils.time import utc_now

router = APIRouter(prefix="/api/notifications", tags=["notifications"])


# ── Request/Response schemas ────────────────────────────
class ChannelCreateRequest(BaseModel):
    """Request to create a notification channel."""

    channel_type: str = Field(..., pattern="^(telegram|discord|webhook)$")
    name: str = Field("", max_length=100)
    chat_id: str | None = Field(None, max_length=100)
    webhook_url: str | None = Field(None, max_length=2000)
    url: str | None = Field(None, max_length=2000)
    secret: str | None = Field(None, max_length=500)


class ChannelResponse(BaseModel):
    """Notification channel response (config is never returned)."""

    id: int
    channel_type: str
    name: str
    is_active: bool
    created_at: str | None = None


class NotificationEventResponse(BaseModel):
    """In-app notification feed event."""

    id: int
    event_type: str
    trader_wallet: str
    market_id: str
    token_id: str
    side: str
    size: float
    price: float
    read_at: str | None = None
    created_at: str
    unread: bool


class PreferenceResponse(BaseModel):
    """Notification preference for one event type."""

    event_type: str
    channel_ids: list[int]
    enabled: bool
    is_default: bool = False


class PreferenceItemRequest(BaseModel):
    """One preference upsert item."""

    event_type: str
    channel_ids: list[int] = Field(default_factory=list)
    enabled: bool = True


class PreferenceUpdateRequest(BaseModel):
    """Batch preference upsert request."""

    preferences: list[PreferenceItemRequest] = Field(default_factory=list)


class TestAlertRequest(BaseModel):
    """Request to send a test alert."""

    channel_id: int | None = None
    message: str = Field("Test alert from Polymarket", max_length=500)


def _channel_response(channel: NotificationChannel) -> ChannelResponse:
    return ChannelResponse(
        id=channel.id,
        channel_type=channel.channel_type,
        name=channel.name or "",
        is_active=bool(channel.is_active),
        created_at=channel.created_at.isoformat() if channel.created_at else None,
    )


def _event_response(event: NotificationFeedEvent) -> NotificationEventResponse:
    return NotificationEventResponse(
        id=event.id,
        event_type=event.event_type,
        trader_wallet=event.trader_wallet or "",
        market_id=event.market_id or "",
        token_id=event.token_id or "",
        side=event.side or "",
        size=event.size or 0.0,
        price=event.price or 0.0,
        read_at=event.read_at.isoformat() if event.read_at else None,
        created_at=event.created_at.isoformat() if event.created_at else "",
        unread=event.read_at is None,
    )


def _preference_response(
    preference: UserNotificationPreference,
) -> PreferenceResponse:
    try:
        channel_ids = [int(item) for item in json.loads(preference.channel_ids)]
    except (TypeError, ValueError):
        channel_ids = []
    return PreferenceResponse(
        event_type=preference.event_type,
        channel_ids=channel_ids,
        enabled=bool(preference.enabled),
    )


def _require_https(url: str) -> None:
    """Creation-time URL check; the full SSRF guard runs at send time."""
    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.hostname:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Webhook URL must be a valid https URL",
        )


def _build_channel_config(request: ChannelCreateRequest) -> dict:
    """Validate per-type config fields and return the plaintext config."""
    if request.channel_type == "telegram":
        chat_id = (request.chat_id or "").strip()
        if not chat_id:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="chat_id is required for telegram channels",
            )
        return {"chat_id": chat_id}
    if request.channel_type == "discord":
        webhook_url = (request.webhook_url or "").strip()
        if not webhook_url:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="webhook_url is required for discord channels",
            )
        _require_https(webhook_url)
        return {"webhook_url": webhook_url}
    webhook_url = (request.url or "").strip()
    if not webhook_url:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="url is required for webhook channels",
        )
    _require_https(webhook_url)
    return {"url": webhook_url, "secret": (request.secret or "").strip()}


# ── Channel management ──────────────────────────────────
@router.get("/channels", response_model=list[ChannelResponse])
async def list_channels(
    current_user: dict = Depends(get_current_user_from_token),
    db: Session = Depends(get_db),
):
    """List the current user's notification channels (config redacted)."""
    user_id = current_user.get("user_id")
    channels = (
        db.query(NotificationChannel)
        .filter(NotificationChannel.user_id == user_id)
        .order_by(NotificationChannel.created_at)
        .all()
    )
    return [_channel_response(channel) for channel in channels]


@router.post("/channels", response_model=ChannelResponse, status_code=status.HTTP_201_CREATED)
async def create_channel(
    request: ChannelCreateRequest,
    current_user: dict = Depends(get_current_user_from_token),
    db: Session = Depends(get_db),
):
    """Create a notification channel; config is Fernet-encrypted at rest."""
    user_id = current_user.get("user_id")
    config = _build_channel_config(request)
    encrypted_config, _key_id = encrypt_text(json.dumps(config))
    channel = NotificationChannel(
        user_id=user_id,
        channel_type=request.channel_type,
        name=request.name,
        config_json=encrypted_config,
    )
    db.add(channel)
    db.commit()
    db.refresh(channel)
    return _channel_response(channel)


@router.delete("/channels/{channel_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_channel(
    channel_id: int,
    current_user: dict = Depends(get_current_user_from_token),
    db: Session = Depends(get_db),
):
    """Delete a notification channel owned by the current user."""
    user_id = current_user.get("user_id")
    channel = (
        db.query(NotificationChannel)
        .filter(
            NotificationChannel.id == channel_id,
            NotificationChannel.user_id == user_id,
        )
        .first()
    )
    if channel is None:
        raise HTTPException(status_code=404, detail="Notification channel not found")
    db.delete(channel)
    db.commit()


# ── In-app alert feed ───────────────────────────────────
@router.get("", response_model=list[NotificationEventResponse])
async def list_notifications(
    unread_only: bool = False,
    skip: int = 0,
    limit: int = Query(50, le=200),
    current_user: dict = Depends(get_current_user_from_token),
    db: Session = Depends(get_db),
):
    """List in-app notification feed events, newest first."""
    user_id = current_user.get("user_id")
    query = db.query(NotificationFeedEvent).filter(NotificationFeedEvent.user_id == user_id)
    if unread_only:
        query = query.filter(NotificationFeedEvent.read_at.is_(None))
    events = query.order_by(NotificationFeedEvent.created_at.desc()).offset(skip).limit(limit).all()
    return [_event_response(event) for event in events]


@router.patch("/{event_id}/read", response_model=NotificationEventResponse)
async def mark_notification_read(
    event_id: int,
    current_user: dict = Depends(get_current_user_from_token),
    db: Session = Depends(get_db),
):
    """Mark a feed event as read."""
    user_id = current_user.get("user_id")
    event = (
        db.query(NotificationFeedEvent)
        .filter(
            NotificationFeedEvent.id == event_id,
            NotificationFeedEvent.user_id == user_id,
        )
        .first()
    )
    if event is None:
        raise HTTPException(status_code=404, detail="Notification not found")
    event.read_at = utc_now()
    db.commit()
    db.refresh(event)
    return _event_response(event)


# ── Preferences ─────────────────────────────────────────
@router.get("/preferences", response_model=list[PreferenceResponse])
async def get_preferences(
    current_user: dict = Depends(get_current_user_from_token),
    db: Session = Depends(get_db),
):
    """Get notification preferences for every event type (defaults included)."""
    user_id = current_user.get("user_id")
    rows = (
        db.query(UserNotificationPreference)
        .filter(UserNotificationPreference.user_id == user_id)
        .all()
    )
    stored = {row.event_type: row for row in rows}
    preferences: list[PreferenceResponse] = []
    for event_type in sorted(ALERT_EVENT_TYPES):
        row = stored.get(event_type)
        if row is None:
            preferences.append(
                PreferenceResponse(
                    event_type=event_type, channel_ids=[], enabled=True, is_default=True
                )
            )
            continue
        preferences.append(_preference_response(row))
    return preferences


@router.put("/preferences", response_model=list[PreferenceResponse])
async def update_preferences(
    request: PreferenceUpdateRequest,
    current_user: dict = Depends(get_current_user_from_token),
    db: Session = Depends(get_db),
):
    """Upsert notification preferences for the current user."""
    user_id = current_user.get("user_id")
    valid_channel_ids = {
        row.id
        for row in db.query(NotificationChannel.id).filter(NotificationChannel.user_id == user_id)
    }
    for item in request.preferences:
        if item.event_type not in ALERT_EVENT_TYPES:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Unknown event type: {item.event_type}",
            )
        unknown = set(item.channel_ids) - valid_channel_ids
        if unknown:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Channel ids not owned by user: {sorted(unknown)}",
            )
    for item in request.preferences:
        row = (
            db.query(UserNotificationPreference)
            .filter(
                UserNotificationPreference.user_id == user_id,
                UserNotificationPreference.event_type == item.event_type,
            )
            .first()
        )
        if row is None:
            db.add(
                UserNotificationPreference(
                    user_id=user_id,
                    event_type=item.event_type,
                    channel_ids=json.dumps(item.channel_ids),
                    enabled=item.enabled,
                )
            )
        else:
            row.channel_ids = json.dumps(item.channel_ids)
            row.enabled = item.enabled
            row.updated_at = utc_now()
    db.commit()
    return await get_preferences(current_user, db)


# ── Test alert ──────────────────────────────────────────
@router.post("/test")
async def send_test_alert(
    request: TestAlertRequest,
    current_user: dict = Depends(get_current_user_from_token),
    db: Session = Depends(get_db),
):
    """Send a test alert through a channel (or all active channels)."""
    user_id = current_user.get("user_id")
    if request.channel_id is not None:
        channel = (
            db.query(NotificationChannel)
            .filter(
                NotificationChannel.id == request.channel_id,
                NotificationChannel.user_id == user_id,
            )
            .first()
        )
        if channel is None:
            raise HTTPException(status_code=404, detail="Notification channel not found")
    service = get_alert_service()
    return await service.send_test_alert(
        user_id, channel_id=request.channel_id, db=db, message=request.message
    )
