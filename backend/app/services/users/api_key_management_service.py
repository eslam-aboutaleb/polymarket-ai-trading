"""API-key lifecycle operations with their actor audit events."""

from sqlalchemy.orm import Session

from app.services.platform import audit_service
from app.services.users.api_key_service import create_api_key, revoke_api_key


def create_api_key_with_audit(
    db: Session,
    *,
    user_id: int,
    name: str,
    account_id: int | None,
    scopes: list[str] | None,
    expires_in_days: int | None,
):
    record, raw_secret = create_api_key(
        db,
        user_id=user_id,
        name=name,
        account_id=account_id,
        scopes=scopes,
        expires_in_days=expires_in_days,
    )
    audit_service.record_api_key_created(db, record=record, user_id=user_id)
    db.commit()
    return record, raw_secret


def revoke_api_key_with_audit(db: Session, *, user_id: int, key_id: int):
    record = revoke_api_key(db, user_id, key_id)
    if record is None:
        return None
    audit_service.record_api_key_revoked(db, record=record, user_id=user_id)
    db.commit()
    return record
