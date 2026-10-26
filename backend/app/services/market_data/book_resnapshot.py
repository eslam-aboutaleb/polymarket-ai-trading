"""Bounded public resnapshot jobs, attached to the existing WS manager lifecycle."""

import asyncio
import logging
import re
import time

import httpx

from app.config import get_settings
from app.services.market_data.book_research import capture_market_books
from app.services.market_data.replay_capture import PublicVenueCapture
from app.utils.database import SessionLocal

logger = logging.getLogger(__name__)


def schedule_resnapshot(manager, event):
    settings = get_settings()
    if not (
        settings.binary_book_research_enabled
        and settings.native_capture_enabled
        and settings.observation_archive_enabled
    ):
        return
    kind = event.get("event_type")
    if kind not in {"book", "price_change", "tick_size_change", "source_status"}:
        return
    tokens = {str(event.get("asset_id") or "")}
    tokens.update(
        str(v.get("asset_id") or "") for v in event.get("price_changes", []) if isinstance(v, dict)
    )
    if not tokens.intersection(manager._subscriptions):
        return
    condition = str(event.get("market") or "").lower()
    if not re.fullmatch(r"0x[0-9a-f]{64}", condition):
        return  # A token alone cannot authorize a guessed condition join.
    now = time.monotonic()
    if (
        len(manager._resnapshot_tasks) >= 2
        or now - manager._resnapshot_attempts.get(condition, -60) < 60
    ):
        return
    manager._resnapshot_attempts = {
        key: at for key, at in manager._resnapshot_attempts.items() if now - at < 60
    }
    manager._resnapshot_attempts[condition] = now
    task = asyncio.create_task(_resnapshot(condition))
    manager._resnapshot_tasks.add(task)
    task.add_done_callback(manager._resnapshot_tasks.discard)


async def _resnapshot(condition):
    with SessionLocal() as db:
        try:
            async with asyncio.timeout(25), httpx.AsyncClient() as client:
                result = await capture_market_books(db, condition, PublicVenueCapture(client))
            db.commit()
            logger.info(
                "Read-only book resnapshot condition=%s status=%s", condition, result["status"]
            )
        except asyncio.CancelledError:
            db.rollback()
            raise
        except Exception:
            db.rollback()
            from app.services.market_data.book_research import record_capture_failure

            try:
                record_capture_failure(db, condition)
                db.commit()
            except Exception:
                db.rollback()
            logger.exception("Read-only book resnapshot unavailable condition=%s", condition)
