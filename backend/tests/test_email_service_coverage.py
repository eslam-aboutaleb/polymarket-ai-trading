"""Coverage for :mod:`app.services.email_service`.

Sender resolution, SMTP configuration detection, the synchronous
SMTP send path (TLS / login branches), and the follow-event email
formatter. All SMTP I/O is mocked.
"""

import unittest
from unittest.mock import MagicMock, patch

from app.services import email_service


def _settings(
    smtp_host="smtp.example.com",
    smtp_port=587,
    smtp_user="user",
    smtp_password="pass",
    smtp_from_email="",
    smtp_use_tls=True,
):
    settings = MagicMock()
    settings.smtp_host = smtp_host
    settings.smtp_port = smtp_port
    settings.smtp_user = smtp_user
    settings.smtp_password = smtp_password
    settings.smtp_from_email = smtp_from_email
    settings.smtp_use_tls = smtp_use_tls
    return settings


class ResolveSenderTests(unittest.TestCase):
    """``_resolve_sender`` precedence chain."""

    def test_prefers_smtp_from_email(self):
        settings = _settings(smtp_from_email="noreply@example.com")
        with patch.object(email_service, "get_settings", return_value=settings):
            self.assertEqual(email_service._resolve_sender(), "noreply@example.com")

    def test_falls_back_to_smtp_user(self):
        settings = _settings(smtp_from_email="")
        with patch.object(email_service, "get_settings", return_value=settings):
            self.assertEqual(email_service._resolve_sender(), "user")

    def test_falls_back_to_default(self):
        settings = _settings(smtp_from_email="", smtp_user="")
        with patch.object(email_service, "get_settings", return_value=settings):
            self.assertEqual(email_service._resolve_sender(), "no-reply@polymarket.local")


class IsSmtpConfiguredTests(unittest.TestCase):
    """``is_smtp_configured`` truth table."""

    def test_configured_when_host_and_port_set(self):
        with patch.object(email_service, "get_settings", return_value=_settings()):
            self.assertTrue(email_service.is_smtp_configured())

    def test_not_configured_without_host(self):
        settings = _settings(smtp_host="")
        with patch.object(email_service, "get_settings", return_value=settings):
            self.assertFalse(email_service.is_smtp_configured())

    def test_not_configured_without_port(self):
        settings = _settings(smtp_port=0)
        with patch.object(email_service, "get_settings", return_value=settings):
            self.assertFalse(email_service.is_smtp_configured())


class SendEmailSyncTests(unittest.TestCase):
    """``_send_email_sync`` SMTP branches."""

    def test_sends_with_tls_and_login(self):
        settings = _settings()
        with (
            patch.object(email_service, "get_settings", return_value=settings),
            patch("app.services.email_service.smtplib.SMTP") as smtp_cls,
        ):
            smtp = smtp_cls.return_value.__enter__.return_value
            email_service._send_email_sync("to@example.com", "Subject", "Body")

        smtp_cls.assert_called_once_with("smtp.example.com", 587, timeout=20)
        smtp.starttls.assert_called_once_with()
        smtp.login.assert_called_once_with("user", "pass")
        smtp.send_message.assert_called_once()
        msg = smtp.send_message.call_args.args[0]
        self.assertEqual(msg["From"], "user")
        self.assertEqual(msg["To"], "to@example.com")
        self.assertEqual(msg["Subject"], "Subject")
        self.assertEqual(msg.get_content().strip(), "Body")

    def test_sends_without_tls_or_login(self):
        settings = _settings(smtp_use_tls=False, smtp_user="", smtp_password="")
        with (
            patch.object(email_service, "get_settings", return_value=settings),
            patch("app.services.email_service.smtplib.SMTP") as smtp_cls,
        ):
            smtp = smtp_cls.return_value.__enter__.return_value
            email_service._send_email_sync("to@example.com", "S", "B")

        smtp.starttls.assert_not_called()
        smtp.login.assert_not_called()
        smtp.send_message.assert_called_once()
        msg = smtp.send_message.call_args.args[0]
        self.assertEqual(msg["From"], "no-reply@polymarket.local")


class SendFollowEventEmailTests(unittest.IsolatedAsyncioTestCase):
    """``send_follow_event_email`` formatting and dispatch."""

    async def test_builds_subject_and_body(self):
        with patch.object(email_service, "_send_email_sync") as send_mock:
            await email_service.send_follow_event_email(
                to_email="to@example.com",
                trader_wallet="0xabcdef1234567890",
                event_type="opened",
                market_id="cond-1",
                side="BUY",
                size=1.5,
                price=0.42,
            )

        send_mock.assert_called_once()
        to_email, subject, body = send_mock.call_args.args
        self.assertEqual(to_email, "to@example.com")
        self.assertEqual(subject, "Trader Opened Position: 0xabcdef12...")
        self.assertIn("Trader wallet: 0xabcdef1234567890", body)
        self.assertIn("Event: opened", body)
        self.assertIn("Market: cond-1", body)
        self.assertIn("Side: BUY", body)
        self.assertIn("Size: 1.500000", body)
        self.assertIn("Price: 0.420000", body)

    async def test_market_id_none_renders_na(self):
        with patch.object(email_service, "_send_email_sync") as send_mock:
            await email_service.send_follow_event_email(
                to_email="to@example.com",
                trader_wallet="0xabcdef1234567890",
                event_type="closed",
                market_id=None,
                side="SELL",
                size=2.0,
                price=0.5,
            )

        body = send_mock.call_args.args[2]
        self.assertIn("Market: N/A", body)
        self.assertIn("Event: closed", body)


if __name__ == "__main__":
    unittest.main()
