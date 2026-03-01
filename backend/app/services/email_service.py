"""SMTP email delivery utilities."""
import asyncio
import logging
import smtplib
from email.message import EmailMessage

from app.config import get_settings

logger = logging.getLogger(__name__)


def _resolve_sender() -> str:
    settings = get_settings()
    return (
        settings.smtp_from_email
        or settings.smtp_user
        or "no-reply@polymarket.local"
    )


def is_smtp_configured() -> bool:
    """Whether SMTP credentials/config are available."""
    settings = get_settings()
    return bool(settings.smtp_host and settings.smtp_port)


def _send_email_sync(to_email: str, subject: str, body: str) -> None:
    settings = get_settings()
    msg = EmailMessage()
    msg["From"] = _resolve_sender()
    msg["To"] = to_email
    msg["Subject"] = subject
    msg.set_content(body)

    with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=20) as smtp:
        if settings.smtp_use_tls:
            smtp.starttls()
        if settings.smtp_user:
            smtp.login(settings.smtp_user, settings.smtp_password)
        smtp.send_message(msg)


async def send_follow_event_email(
    to_email: str,
    trader_wallet: str,
    event_type: str,
    market_id: str,
    side: str,
    size: float,
    price: float,
) -> None:
    """Send immediate notification email for followed-trader open/close events."""
    subject = f"Trader {event_type.title()} Position: {trader_wallet[:10]}..."
    body = (
        f"Trader wallet: {trader_wallet}\n"
        f"Event: {event_type}\n"
        f"Market: {market_id or 'N/A'}\n"
        f"Side: {side}\n"
        f"Size: {size:.6f}\n"
        f"Price: {price:.6f}\n"
    )
    await asyncio.to_thread(_send_email_sync, to_email, subject, body)
    logger.info(
        "Follow-event email sent to %s for trader %s (%s)",
        to_email,
        trader_wallet[:10],
        event_type,
    )
