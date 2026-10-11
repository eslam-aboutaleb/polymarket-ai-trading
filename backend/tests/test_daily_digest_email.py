"""Coverage for ``send_daily_digest_email`` (plan 02).

The digest email is a plain pass-through of the subject and body the report
service assembles, so the tests focus on the SMTP path: the delivery uses the
shared ``_send_email_sync`` helper off the event loop, propagates transport
failures to the caller (which records them on the daily marker), and logs a
delivery line. Same pattern as ``test_email_service_coverage.py``.
"""

import unittest
from unittest.mock import patch

from app.services import email_service
from app.services.email_service import send_daily_digest_email


class SendDailyDigestEmailTests(unittest.IsolatedAsyncioTestCase):
    async def test_delegates_to_the_shared_smtp_helper(self):
        with patch.object(email_service, "_send_email_sync") as send_mock:
            await send_daily_digest_email(
                "user@example.com",
                "Daily Portfolio Digest - 2026-10-11",
                "Header\n\nArticle body",
            )

        send_mock.assert_called_once()
        to_email, subject, body = send_mock.call_args.args
        self.assertEqual(to_email, "user@example.com")
        self.assertEqual(subject, "Daily Portfolio Digest - 2026-10-11")
        self.assertEqual(body, "Header\n\nArticle body")

    async def test_transport_failure_propagates(self):
        with (
            patch.object(email_service, "_send_email_sync", side_effect=RuntimeError("smtp down")),
            self.assertRaises(RuntimeError),
        ):
            await send_daily_digest_email(
                "user@example.com", "Daily Portfolio Digest - 2026-10-11", "Article"
            )

    async def test_multiline_body_is_sent_unchanged(self):
        body = "\n".join(
            [
                "Daily Portfolio Digest - 2026-10-11 (Paper)",
                "",
                "Start value: $1,000.00",
                "Current value: $1,012.34",
                "Realized P&L today: +$12.34",
                "",
                "P&L summary: gains came from Market A.",
            ]
        )
        with patch.object(email_service, "_send_email_sync") as send_mock:
            await send_daily_digest_email(
                "user@example.com", "Paper Daily Portfolio Digest - 2026-10-11", body
            )

        self.assertEqual(send_mock.call_args.args[2], body)


if __name__ == "__main__":
    unittest.main()
